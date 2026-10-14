"""🧩 数据师：从线索池取线索 → 硬性条件筛选 → 去重 → 读官网补全 → AI 打分 A/B/C/D + 客户画像 → 线索库。

“合格线索”严格按 PRD 5.1，必须同时满足：
1. 符合模板硬性条件（国家、客户类型；不在排除名单）
2. 评分在模板的合格等级线内（默认 A、B）
3. 不重复（只按 TG 用户名 / WhatsApp 号码判重：同一个 TG 或 WhatsApp = 同一个客户）
4. 至少一个可用联系方式：TG 用户名 或 明确的 WhatsApp（只有邮箱 / 普通电话 → 备用，不计数）
5. 有依据（附来源链接 / 原文）
C 进线索库备用（不计数）；D 丢弃（只留在线索池记录原因，不进线索库）。
"""
import json
import re
import urllib.parse

from .. import contacts as contacts_mod
from .. import countries, db, edition, leadpool, leads, llm, prompts, quality, settings
from ..llm import decision
from ..sources import crawler as crawler_mod
from . import learning, memory, schemas
from .researcher import icp_brief

AGENT = "analyst"

# 打分提示词在提示词库 analyst.score；打分标准（grading_criteria）来自客户的模板（AI 根据客户资料生成），
# 程序里不写死“什么算好线索”（PRD 2.4.2）。A–D 等级本身和程序硬性检查属于功能，留在这里。


class Analyst:
    def __init__(self, run, icp: dict):
        self.run = run
        self.icp = icp
        self.icp_id = run.icp_id
        self.cfg = llm.get_config(AGENT)
        self.mock = self.cfg.provider == "mock"
        self.crawler = crawler_mod.Crawler(mock=self.mock, should_stop=lambda: run.cancel_flag.is_set(),
                                           max_pages=10_000)
        self.enrich = settings.get("analyst")["enrich_website"]
        self.lang = settings.get("ui_lang")
        self.qualified_grades = set(icp.get("qualified_grades") or ["A", "B"])
        icp = {**icp, "countries": countries.profile_countries(icp)}   # 客户自己说的目标国家（空 = 不按国家筛）
        # 学到的知识（按模板、带版本）：打分权重 + 额外排除项。任务开始时读取一次 —— 干净上下文
        self.knowledge = learning.effective(run.icp_id)
        self.icp = icp = {**icp,
                          "exclude_words": list(dict.fromkeys((icp.get("exclude_words") or [])
                                                              + self.knowledge["exclude_keywords"])),
                          "exclude_companies": list(dict.fromkeys((icp.get("exclude_companies") or [])
                                                                  + self.knowledge["exclude_companies"]))}
        from . import icp as icp_mod
        if self.icp_id and icp_mod.needs_rules(icp):
            if icp_mod.ensure_rules(self.icp_id, task_id=run.id):      # 旧模板：先让主管生成打分标准
                icp = self.icp = {**icp, **{k: icp_mod.get(self.icp_id)["data"].get(k) for k in
                                            ("buy_words", "product_words", "grading_criteria")}}
                self.knowledge = learning.effective(run.icp_id)
        self.criteria = icp_mod.criteria_text(icp, self.knowledge["weights"])
        # 同一任务内不变的部分放进 system（Claude 提示缓存）
        self.system = prompts.render("analyst.score", lang="Simplified Chinese" if self.lang == "zh" else "English",
                                     icp=icp_brief(icp), criteria=self.criteria, memos=memory.memos_block(AGENT))

    def emit(self, _key, level="info", **p):
        self.run.emit(AGENT, _key, level=level, state="working", **p)

    def _finish_raw(self, rid, status, reason="", lead_id=None):
        db.execute("UPDATE raw_leads SET status=?, reason=?, lead_id=?, processed_at=? WHERE id=?",
                   (status, reason, lead_id, db.now(), rid))

    # ------------------------------------------------------------ 硬性条件（规则，不花钱）
    def rule_filter(self, cand: dict, domain: str) -> str:
        text = " ".join([cand.get("company_name", ""), cand.get("description", ""), cand.get("customer_type", ""),
                         cand.get("website", "")]).lower()
        from . import icp as icp_mod
        w = {"buy": [], "product": [], "exclude": icp_mod.lead_words({"exclude_words": self.icp.get("exclude_words")})["exclude"]}
        if icp_mod.match_words(text, w) == "excluded":       # 排除词（PRD §3）
            k = next((x for x in w["exclude"] if icp_mod._rx([x]).search(text)), "")
            return f"excluded_keyword:{k}"
        for ex in self.icp.get("exclude_companies") or []:
            exl = ex.lower().strip()
            if not exl:
                continue
            exd = leads.domain_of(exl) if "." in exl else ""
            if (exd and domain and exd == domain) or leads.company_key(exl) == leads.company_key(cand["company_name"]):
                return "excluded_company"
        c = cand.get("country") or ""
        if not edition.served_market(self.icp) and c and self.icp.get("countries") and c not in self.icp["countries"]:
            return "wrong_country"
        return ""

    # ------------------------------------------------------------ 主流程
    def process(self, rid: int):
        raw = db.query_one("SELECT * FROM raw_leads WHERE id=?", (rid,))
        if not raw or raw["status"] != "new":
            return
        db.execute("UPDATE raw_leads SET status='processing' WHERE id=?", (rid,))
        cand = db.loads(raw["data"], {})
        name = cand.get("company_name") or raw["company_name"]
        cand["company_name"] = name
        cand["country"] = countries.normalize_country(cand.get("country", ""))
        domain = leads.domain_of(cand.get("website", ""))
        self.emit("ev.analysing", name=name)

        # 1. 硬性条件（规则）
        why = self.rule_filter(cand, domain)
        if why:
            self._finish_raw(rid, "filtered", why)
            self.run.bump(processed=1, filtered=1)
            self.emit("ev.filtered", name=name, reason=f"@reason.{why.split(':')[0]}", detail=why.partition(":")[2])
            return

        # 1.5 黑名单（同行 / 已合作客户 / 永不联系，PRD 5.2.2）：程序直接排除，任何来源都一样
        emails = [e.lower() for e in cand.get("emails") or []]
        tgs = [t for t in cand.get("telegram") or []]
        nums = leads.numbers_of([], list(cand.get("whatsapp") or []) + list(cand.get("phones") or []))
        bl = leadpool.blacklisted(name, domain, emails, tgs, nums)
        if bl:
            self._finish_raw(rid, "filtered", f"blacklist:{bl}")
            self.run.bump(processed=1, filtered=1)
            self.emit("ev.filtered", name=name, reason="@reason.blacklist", detail=bl)
            return

        # 2. 去重（线索库已有 / 已联系过）
        src = [{"url": raw["source_url"], "title": raw["source_title"], "quote": raw["evidence"][:300]}] \
            if raw["source_url"] else []
        contacts = self._contacts(cand, emails, tgs, raw["source"], raw["source_url"], verified=cand.get(
            "verified_text", True))
        with leads.write_lock:
            dup = leads.find_duplicate(tgs, nums)
            if dup:
                leads.merge_into(dup, contacts, src)
                self._finish_raw(rid, "duplicate", "duplicate", dup)
                self.run.bump(processed=1, duplicate=1)
                self.emit("ev.duplicate", name=name, lead_id=dup)
                return

        # 2.5 判断模型预筛（PRD 2.3，可选）：只在“很有把握不符合”时筛掉，省掉读官网和大模型打分的费用；
        #     拿不准（最高概率 < 阈值）或判断为符合 → 交给大模型。判断模型不能单独让线索合格。
        if decision.enabled() and not edition.signal_mode(self.icp):
            q, opts = prompts.options("decision.lead_match")
            d = decision.decide(self._material(raw, cand, [])[:12000], opts, q + "\n" + icp_brief(self.icp),
                                agent=AGENT, task_id=self.run.id, context=self._mock_hint(cand))
            if d and d.choice == "no_match" and not d.escalate:
                self._finish_raw(rid, "discarded", "prescreen_no_match")
                self.run.bump(processed=1, discarded=1)
                self.emit("ev.prescreen_out", name=name, p=f"{d.confidence:.0%}")
                return
            if d and d.escalate:
                self.emit("ev.prescreen_unsure", name=name, p=f"{d.confidence:.0%}")

        # 3. 读官网补全（联系页 / 关于页），遵守 robots.txt 和限速
        pages = []
        if self.enrich and cand.get("website") and not leads.domain_of(cand["website"]) == "":
            pages = self._read_site(cand["website"])
        site_text = " ".join(p["markdown"] for p in pages).lower()
        for p in pages:
            for e in p.get("emails") or []:
                if e not in emails and (not domain or e.endswith("@" + domain) or e.split("@")[1].endswith(domain)
                                        or self.mock):
                    emails.append(e)
                    contacts.append({"name": "", "title": "", "email": e, "telegram": "", "source_url": p["url"],
                                     "verified": True})
        # Claude 联网路径的邮箱：若在官网出现，或与公司域名一致，视为可信
        for c in contacts:
            if c.get("email") and not c.get("verified"):
                if c["email"] in site_text or (domain and c["email"].endswith("@" + domain)):
                    c["verified"] = True

        # 4. AI 打分 + 画像（带上客户教过的正/负样本作参照）
        material = self._material(raw, cand, pages)
        samples = learning.relevant_samples(self.icp_id, material)
        # 干净上下文：每条线索单独一次调用，只带 岗位说明书 + 模板（system，可缓存）+ 相关样本 + 本线索材料
        user = prompts.render("analyst.score_task", examples=learning.few_shot_block(samples).lstrip(),
                              material=material).lstrip()
        self.emit("ev.scoring", name=name)
        data, _ = llm.call_json(AGENT, "score_lead", self.system, user, schemas.SCORE,
            task_id=self.run.id,
            context={"candidate": cand, "icp": self.icp, "pages": pages, "lang": self.lang})

        grade = data.get("grade") if data.get("grade") in ("A", "B", "C", "D") else "C"
        hm = data.get("hard_match") or {}
        country = countries.normalize_country(data.get("country", "")) or cand["country"]
        # 国家（硬条件：客户说了目标国家时才检查）：域名后缀、电话区号是强依据；地名是弱依据（AI 没给国家时才用）
        phones = [c.get("whatsapp") for c in contacts if c.get("whatsapp")] + \
            [ph for p in pages for ph in (p.get("phones") or [])] + list(cand.get("phones") or [])
        inferred, strength = countries.infer_country(material + " " + site_text, domain, phones)
        if not edition.served_market(self.icp) and inferred and (not country or strength == "strong"):
            country = inferred
        validation = None
        if edition.served_market(self.icp):
            validation = self._market_validation(self.icp, raw, cand, pages, country)
            cok = {"matched": True, "mismatch": False, "unverified": None}[validation["market_validation"]["status"]]
            b2b = validation["b2b_validation"]["status"]
            hm["customer_type_ok"] = b2b == "matched"
        else:
            cok = countries.country_ok(country, self.icp)
        if cok is False:
            hm["country_ok"] = False
        else:
            hm["country_ok"] = True          # 客户没说国家 / 国家不明：不算硬条件不符（不明的单独记为“国家不明”进备用池）
        hard_ok = bool(hm.get("country_ok")) and bool(hm.get("customer_type_ok")) and not hm.get("excluded")

        # 来源：只接受本次真实读过的网址（有依据）
        allowed = {raw["source_url"]} | {p["url"] for p in pages}
        sources = []
        for s in data.get("sources") or []:
            u = str(s.get("url") or "")
            if edition.served_market(self.icp):
                docs = self._source_documents(raw, cand, pages)
                quote = str(s.get("quote") or "")
                if not quote or not any(d["url"] == u and quote in d["text"] for d in docs):
                    continue
            if u in allowed and all(x["url"] != u for x in sources):
                sources.append({"url": u, "title": s.get("title", ""), "quote": str(s.get("quote") or "")[:300]})
        for s in src:
            if edition.served_market(self.icp) and not any(d["url"] == s["url"] and s["quote"] in d["text"] for d in self._source_documents(raw, cand, pages)):
                continue
            if all(x["url"] != s["url"] for x in sources):
                sources.insert(0, s)

        # AI 补充的联系方式也必须在原文里出现过
        all_text = (material + " " + site_text).lower()
        for e in data.get("emails") or []:
            e = e.strip().lower()
            if e and e not in emails and e in all_text:
                emails.append(e)
                contacts.append({"name": "", "title": "", "email": e, "telegram": "", "source_url": "",
                                 "verified": True})
        for t in data.get("telegram") or []:
            if t and t.lower().lstrip("@") in all_text and all(c.get("telegram") != t for c in contacts):
                contacts.append({"name": "", "title": "", "email": "", "telegram": t, "source_url": "",
                                 "verified": True})
        for dm in data.get("decision_makers") or []:
            nm = str(dm.get("name") or "").strip()
            if nm and nm.lower() in all_text and all(c.get("name") != nm for c in contacts):
                em = str(dm.get("email") or "").lower()
                contacts.append({"name": nm, "title": dm.get("title", ""), "email": em if em in all_text else "",
                                 "telegram": "", "source_url": dm.get("source_url", ""), "verified": True})

        # 官网上找到的 TG / WhatsApp（PRD 5.1：合格线索必须有 TG 用户名或明确的 WhatsApp）
        for p in pages:
            for w in p.get("whatsapp") or []:
                if all(c.get("whatsapp") != w for c in contacts):
                    contacts.append({"name": "", "title": "", "email": "", "telegram": "", "whatsapp": w,
                                     "wa_explicit": True, "source_url": p["url"], "verified": True})
            for t in p.get("telegram_links") or []:
                if all((c.get("telegram") or "").lower() != t.lower() for c in contacts):
                    contacts.append({"name": "", "title": "", "email": "", "telegram": t, "source_url": p["url"],
                                     "verified": True})
            for ph in (p.get("phones") or [])[:2]:
                if all(c.get("whatsapp") != ph for c in contacts):
                    contacts.append({"name": "", "title": "", "email": "", "telegram": "", "whatsapp": ph,
                                     "wa_explicit": False, "source_url": p["url"], "verified": False})
        bl = leadpool.blacklisted("", "", [], [c["telegram"] for c in contacts if c.get("telegram")],
                                  leads.numbers_of(contacts))
        if bl:      # 官网上新找到的 TG / WhatsApp 在黑名单里（同行 / 已合作客户 / 永不联系）
            self._finish_raw(rid, "filtered", f"blacklist:{bl}")
            self.run.bump(processed=1, filtered=1)
            self.emit("ev.filtered", name=name, reason="@reason.blacklist", detail=bl)
            return
        has_contact = contacts_mod.qualifying(contacts)
        profile = dict(data.get("profile") or {})
        profile["grade_reason"] = data.get("grade_reason", "")
        profile["hard_match"] = hm
        signal_accepted = bool(edition.signal_mode(self.icp) and validation
                               and validation["b2b_validation"]["status"] == "matched")
        if signal_accepted:
            validation["b2b_validation"]["status"] = "suspected"
        if validation:
            profile.update(validation)
            for evidence in validation["market_validation"]["evidence"] + validation["b2b_validation"]["evidence"]:
                if not any(x["url"] == evidence["url"] and x["quote"] == evidence["quote"] for x in sources):
                    sources.append({"url": evidence["url"], "title": "Verified business / served-market evidence", "quote": evidence["quote"]})

        # 5. 判定（国家明确不在目标内才丢弃；国家不明的按“不完全符合硬性条件”进备用）
        country_wrong = cok is False and not signal_accepted
        b2b_wrong = validation and (validation["b2b_validation"]["status"] == "mismatch" or
                                    edition.signal_mode(self.icp) and validation["b2b_validation"]["status"] == "unverified")
        if ((grade == "D" or hm.get("excluded")) and not signal_accepted) or country_wrong or b2b_wrong:
            reason = ("b2b_unverified" if validation["b2b_validation"]["status"] == "unverified" else "b2b_mismatch") if b2b_wrong else "market_mismatch" if country_wrong and validation else "grade_d" if grade == "D" else ("wrong_country" if country_wrong else "excluded")
            db.execute("UPDATE raw_leads SET data=? WHERE id=?",
                       (db.dumps({**cand, "analysis": {"grade": grade, "reason": data.get("grade_reason", ""), "validation": validation}}), rid))
            self._finish_raw(rid, "discarded", reason)
            self.run.bump(processed=1, discarded=1)
            self.emit("ev.discarded", name=name, grade=grade, reason=data.get("grade_reason", "")[:200])
            return

        nq = ""
        if validation and validation["market_validation"]["status"] == "unverified":
            nq = "market_unverified"
        elif validation and validation["b2b_validation"]["status"] == "unverified":
            nq = "b2b_unverified"
        elif grade not in self.qualified_grades:
            nq = "grade"
        elif not hard_ok:
            nq = "hard_conditions"
        elif cok is None:
            nq = "country_unknown"
        elif not has_contact:
            nq = "no_tg_wa"
        elif not sources:
            nq = "no_source"
        qualified = signal_accepted or not nq
        if signal_accepted:
            nq = ""
        # v1 没有人工审核：按比例抽查数据师的判断（裁判模型），不同意或拿不准 → 备用池（PRD §1）
        if qualified and not signal_accepted and not leads.review_on() and quality.should_check():
            j = quality.judge({"company_name": name, "grade": grade, "score": data.get("score")},
                              icp_brief(self.icp) + "\n\nGrading criteria:\n" + self.criteria, material,
                              task_id=self.run.id)
            self.emit("ev.spot_check", name=name, result="@quality.pass" if j["passed"] else "@quality.fail",
                      p=f"{j['confidence']:.0%}")
            if not j["passed"]:
                qualified, nq = False, "spot_check"
        lead = {
            "company_name": name, "domain": domain, "website": cand.get("website", ""), "country": country,
            "customer_type": data.get("customer_type") or cand.get("customer_type", ""),
            "industry": data.get("industry") or cand.get("industry", ""), "grade": grade,
            "score": max(0, min(100, int(data.get("score") or 0))), "qualified": qualified,
            "status": leads.qualified_status() if qualified else "backup", "not_qualified_reason": nq,
            "profile": profile, "contacts": contacts, "sources": sources, "first_source": raw["source"],
            "icp_id": self.icp_id, "task_id": self.run.id,
            "message_type": cand.get("message_type", ""), "intent": cand.get("intent", ""),
            "found_by_account": cand.get("found_by_account"), "posted_at": cand.get("posted_at"),
            "channel_id": cand.get("channel_id"),
        }
        with leads.write_lock:
            dup = leads.find_duplicate([c["telegram"] for c in contacts if c.get("telegram")],
                                       leads.numbers_of(contacts))
            if dup:   # 并行处理时同一公司刚被另一个 worker 写入
                leads.merge_into(dup, contacts, sources)
                self._finish_raw(rid, "duplicate", "duplicate", dup)
                self.run.bump(processed=1, duplicate=1)
                self.emit("ev.duplicate", name=name, lead_id=dup)
                return
            if qualified and self.run.target_reached.is_set():
                lead["qualified"], lead["status"], lead["not_qualified_reason"] = False, "backup", "target_reached"
                qualified = False
            lead_id = leads.insert(lead)
        self._finish_raw(rid, "qualified" if qualified else "backup", nq, lead_id)
        if qualified:
            snap = self.run.bump(processed=1, qualified=1)
            self.emit("ev.qualified", level="success", name=name, grade=grade, lead_id=lead_id,
                      n=snap["qualified"], target=self.run.target)
        else:
            self.run.bump(processed=1, backup=1)
            self.emit("ev.backup", name=name, grade=grade, reason=f"@nq.{nq}", lead_id=lead_id)

    # ------------------------------------------------------------ 辅助
    def _mock_hint(self, cand):
        """仅 mock 判断模型使用：按规则给出“答案”，用于测试预筛与“拿不准就升级”。"""
        ct = (cand.get("customer_type") or "").lower()
        if any(ct and (ct in t.lower() or t.lower() in ct) for t in self.icp.get("customer_types") or []):
            return {"mock_hint": "match"}
        return {"mock_hint": "no_match", "mock_unsure": ct != "brand_owner"}

    @staticmethod
    def _contacts(cand, emails, tgs, source, url, verified=True):
        out = []
        people = cand.get("people") or []
        for e in emails:
            p = next((x for x in people if (x.get("email") or "").lower() == e), {})
            out.append({"name": p.get("name", ""), "title": p.get("title", ""), "email": e, "telegram": "",
                        "source_url": url, "verified": bool(verified)})
        for t in tgs:
            out.append({"name": "", "title": "", "email": "", "telegram": t, "source_url": url,
                        "verified": bool(verified)})
        for w in cand.get("whatsapp") or []:
            out.append({"name": "", "title": "", "email": "", "telegram": "", "whatsapp": w, "wa_explicit": True,
                        "source_url": url, "verified": bool(verified)})
        for ph in (cand.get("phones") or [])[:2]:
            from .. import contacts as cm
            n = cm.norm_phone(ph)
            if n and all(c.get("whatsapp") != n for c in out):
                out.append({"name": "", "title": "", "email": "", "telegram": "", "whatsapp": n, "wa_explicit": False,
                            "source_url": url, "verified": False})
        for p in people:
            if p.get("name") and all(c.get("name") != p["name"] for c in out):
                out.append({"name": p["name"], "title": p.get("title", ""), "email": "", "telegram": "",
                            "source_url": url, "verified": bool(verified)})
        return out

    def _read_site(self, website: str) -> list[dict]:
        url = crawler_mod.normalize_url(website)
        self.emit("ev.reading_site", url=url)
        pages = []
        try:
            home = self.crawler.fetch(url)
            pages.append(home)
            for sub in crawler_mod.key_pages(home, limit=2):
                try:
                    pages.append(self.crawler.fetch(sub))
                except crawler_mod.Skip:
                    continue
        except crawler_mod.Skip as e:
            if e.key != "ev.cancelled":
                self.emit(e.key, level="warn", **e.params)
        for p in pages:
            p["markdown"] = p["markdown"][:8000]
        return pages

    @staticmethod
    def _source_documents(raw, cand, pages):
        # A directory may describe several companies. Bind each quote to this
        # candidate, or to its own fetched website; never use another entry.
        documents = []
        name = str(cand.get("company_name") or raw.get("company_name") or "").strip()
        own_domain = leads.domain_of(cand.get("website", ""))
        for p in pages:
            if own_domain and leads.domain_of(p["url"]) == own_domain:
                documents.append({"url": p["url"], "text": p.get("markdown", "")})
        verified = cand.get("verified_evidence") or {}
        quote = str(cand.get("evidence_quote") or raw.get("evidence") or "")
        excerpt = str(cand.get("page_excerpt") or "") if cand.get("verified_text") else ""
        quote_verified = (verified.get("url") == raw["source_url"] and verified.get("quote") == quote) or (quote and quote in excerpt)
        # Exact candidate quotation must identify the candidate. Anonymous group
        # context or a named neighbouring company cannot qualify it.
        entity_pattern = re.escape(name) if re.search(r"[\u3400-\u9fff]", name) else r"(?<!\w)" + re.escape(name) + r"(?!\w)"
        bound = bool(name and len(name) >= 2 and re.search(entity_pattern, quote, re.I))
        source_path = urllib.parse.urlparse(raw["source_url"])
        handle = str(cand.get("telegram", [""])[0] or "").lstrip("@") if cand.get("telegram") else ""
        author_bound = bool(handle and source_path.hostname in ("t.me", "telegram.me")
                            and source_path.path.strip("/").split("/")[0].lower() == handle.lower())
        tg = cand.get("tg") or {}
        message_bound = bool(raw.get("source") == "tg" and verified.get("origin") == "tg_message"
                             and verified.get("author") in (str(tg.get("username") or ""), str(tg.get("sender_id") or ""))
                             and verified.get("author") and str(tg.get("msg_id")) == source_path.path.rstrip("/").split("/")[-1])
        post = cand.get("post") or {}
        post_author = str(post.get("author") or "")
        post_bound = bool(verified.get("origin") == "public_post" and post_author
                          and verified.get("author") == post_author and post.get("url") == raw["source_url"]
                          and name.lstrip("@").casefold() == post_author.lstrip("@").casefold())
        author_bound = author_bound or message_bound or post_bound
        if quote_verified and (bound or author_bound):
            # A multi-entry quotation still must not transfer the neighbouring
            # company's market assertion to this entity.
            entity_sentences = [sentence for sentence in edition._sentences(quote)
                                if author_bound or re.search(entity_pattern, sentence, re.I)]
            if entity_sentences:
                documents.append({"url": raw["source_url"], "text": "\n".join(entity_sentences)})
        if own_domain and leads.domain_of(raw["source_url"]) == own_domain and excerpt:
            documents.append({"url": raw["source_url"], "text": excerpt})
        return documents

    @staticmethod
    def _market_validation(icp, raw, cand, pages, country):
        documents = Analyst._source_documents(raw, cand, pages)
        return {"geo_mode": "served_market", "target_markets": icp.get("target_markets") or [],
                "market_validation": edition.validate_market(icp, documents, country),
                "b2b_validation": edition.validate_b2b(icp, documents)}

    @staticmethod
    def _material(raw, cand, pages) -> str:
        c = {k: cand.get(k) for k in ("company_name", "website", "country", "customer_type", "industry", "emails",
                                      "telegram", "people", "description", "buying_signal")}
        parts = [f"Source: {raw['source']} — {raw['source_url']}",
                 f"Extracted by researcher: {json.dumps(c, ensure_ascii=False)}",
                 f"Evidence quote from source: {raw['evidence']}"]
        if cand.get("page_excerpt"):
            parts.append(f"Source page excerpt:\n{cand['page_excerpt'][:2500]}")
        for p in pages:
            host = urllib.parse.urlparse(p["url"]).netloc
            parts.append(f"### Company website page ({host}): {p['url']}\n{p['markdown']}")
        return "\n\n".join(parts)


class Passthrough:
    """数据师关闭时（PRD 2.1.2）：不筛选打分，程序只做去重 + 排除名单核对，线索直接进入审核台（不计入任务数）。"""

    def __init__(self, run, icp: dict):
        self.run = run
        self.icp = icp
        self.icp_id = run.icp_id

    def process(self, rid: int):
        raw = db.query_one("SELECT * FROM raw_leads WHERE id=?", (rid,))
        if not raw or raw["status"] != "new":
            return
        cand = db.loads(raw["data"], {})
        name = cand.get("company_name") or raw["company_name"]
        domain = leads.domain_of(cand.get("website", ""))
        emails = [e.lower() for e in cand.get("emails") or []]
        tgs = list(cand.get("telegram") or [])
        a = Analyst.__new__(Analyst)
        a.icp = self.icp
        why = Analyst.rule_filter(a, {**cand, "company_name": name,
                                      "country": countries.normalize_country(cand.get("country", ""))}, domain)
        nums = leads.numbers_of([], list(cand.get("whatsapp") or []) + list(cand.get("phones") or []))
        bl = leadpool.blacklisted(name, domain, emails, tgs, nums)
        if bl:
            why = f"blacklist:{bl}"
        if why:
            db.execute("UPDATE raw_leads SET status='filtered', reason=?, processed_at=? WHERE id=?",
                       (why, db.now(), rid))
            self.run.bump(processed=1, filtered=1)
            return
        contacts = Analyst._contacts(cand, emails, tgs, raw["source"], raw["source_url"],
                                     verified=cand.get("verified_text", True))
        src = [{"url": raw["source_url"], "title": raw["source_title"], "quote": raw["evidence"][:300]}] \
            if raw["source_url"] else []
        with leads.write_lock:
            dup = leads.find_duplicate(tgs, nums)
            if dup:
                leads.merge_into(dup, contacts, src)
                db.execute("UPDATE raw_leads SET status='duplicate', reason='duplicate', lead_id=?, processed_at=? "
                           "WHERE id=?", (dup, db.now(), rid))
                self.run.bump(processed=1, duplicate=1)
                return
            # 数据师关闭：没有打分，只有程序硬检查。有审核台时交给人看；v1 没有审核环节 —— 硬检查都通过才算合格
            # （国家符合客户说的目标国家、TG 或明确 WhatsApp、有来源），否则进备用池
            country = countries.normalize_country(cand.get("country", "")) or countries.infer_country(
                " ".join([cand.get("description", ""), raw["evidence"] or ""]), domain,
                [c.get("whatsapp") for c in contacts if c.get("whatsapp")])[0]
            validation = Analyst._market_validation(self.icp, raw, cand, [], country) if edition.served_market(self.icp) else None
            signal_accepted = bool(edition.signal_mode(self.icp) and validation
                                   and validation["b2b_validation"]["status"] == "matched")
            b2b_wrong = validation and (validation["b2b_validation"]["status"] == "mismatch" or
                                        edition.signal_mode(self.icp) and validation["b2b_validation"]["status"] == "unverified")
            if validation and (b2b_wrong or validation["market_validation"]["status"] == "mismatch" and not signal_accepted):
                db.execute("UPDATE raw_leads SET status='filtered',reason=?,data=?,processed_at=? WHERE id=?",
                           (("b2b_unverified" if validation["b2b_validation"]["status"] == "unverified" else "b2b_mismatch") if b2b_wrong else "market_mismatch",
                            db.dumps({**cand, "validation": validation}), db.now(), rid))
                self.run.bump(processed=1, filtered=1)
                return
            geo_ok = (validation["market_validation"]["status"] == "matched" and validation["b2b_validation"]["status"] == "matched") if validation else countries.country_ok(country, self.icp) is True
            if validation:
                src = [{"url": e["url"], "title": "Verified business / served-market evidence", "quote": e["quote"]} for e in
                       validation["market_validation"]["evidence"] + validation["b2b_validation"]["evidence"]]
            if signal_accepted:
                validation["b2b_validation"]["status"] = "suspected"
            hard = signal_accepted or bool(src) and contacts_mod.qualifying(contacts) and geo_ok
            if leads.review_on():
                status, qual, nq = "pending_review", False, "analyst_off"
            else:
                status, qual, nq = (leads.qualified_status(), True, "") if hard else ("backup", False, "analyst_off")
            lead_id = leads.insert({
                "company_name": name, "domain": domain, "website": cand.get("website", ""),
                "country": country,
                "customer_type": cand.get("customer_type", ""), "industry": cand.get("industry", ""), "grade": "-",
                "score": 0, "qualified": qual, "status": status, "not_qualified_reason": nq,
                "profile": {"summary": cand.get("description", ""), "grade_reason": "", **(validation or {})}, "contacts": contacts,
                "sources": src, "first_source": raw["source"], "icp_id": self.icp_id, "task_id": self.run.id,
                "message_type": cand.get("message_type", ""), "intent": cand.get("intent", ""),
                "found_by_account": cand.get("found_by_account"), "posted_at": cand.get("posted_at"),
            "channel_id": cand.get("channel_id")})
        db.execute("UPDATE raw_leads SET status=?, reason='analyst_off', lead_id=?, processed_at=? WHERE id=?",
                   ("qualified" if qual else "backup", lead_id, db.now(), rid))
        self.run.bump(processed=1, **({"qualified": 1} if qual else {"backup": 1}))
        self.run.emit("analyst", "ev.analyst_off_passed", name=name, lead_id=lead_id)

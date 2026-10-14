"""线索池管理（PRD 5.2.2）。数据师兼任“线索池管理员”；红线由程序把关。

线索池分区（状态）：
- 待处理 pending：raw_leads.status = new / processing（还没交给数据师）
- 合格 qualified：A/B + 国家符合客户说的目标国家（没说就不限） + 有 TG 用户名或明确的 WhatsApp + 有来源（leads.qualified=1，status=qualified；
  v1 没有审核环节，当晚推送给客户的销售后变成 pushed；有审核台时是 pending_review → approved）
- 备用 backup：C 级，或缺 TG / WhatsApp 等（leads.status=backup）
- 丢弃 discarded：D 级、重复、同行 / 黑名单、广告 / 骗子（raw_leads 的 discarded/filtered/duplicate，或 leads.status=discarded）
- 之后：已审核(approved) → 已联系(contacted) → 已回复(replied) → 成交(deal=1) / 过期(expired：求购消息太旧，公司信息保留)

数据师作为池管理员：
- 跨来源合并重复（leads.merge_into，保留所有来源和联系人）；合并带来新联系方式时立刻重新判定（可能升级为合格）
- 备用池补全：重读官网（联系页 / 关于页）+ 官网上链接的 Facebook / 领英公开页（只读公开页面，遵守 robots，不登录）
- 定期复查（默认每 7 天，主管可调）：有了 TG / 明确 WhatsApp，或出现新的求购信号 → 升级为合格
- 过期：TG 等求购消息超过 expire_days（默认 30 天，主管可调）还没处理 → expired（公司信息保留）
- 清理：黑名单（同行、已合作客户、永不联系）、广告 → discarded

程序红线：去重和合格判定在代码里；永不联系标记（do_not_contact）永久有效；黑名单自动排除；
同一客户不重复开发（contact_history + can_contact()，第 3 步发送层调用）。
"""
import time

from . import contacts as contacts_mod, db, i18n, leads, settings

DEFAULTS = {"recheck_days": 7, "expire_days": 30, "batch": 30, "run_every_h": 24}
UPGRADABLE = {"no_tg_wa", "no_contact", "grade", "country_unknown"}       # 这些“备用原因”在条件满足后可以自动升级


def cfg() -> dict:
    c = dict(DEFAULTS)
    c.update(settings.get("lead_pool", {}) or {})
    return c


def event(lead_id, kind, reason="", detail=""):
    db.execute("INSERT INTO pool_events(lead_id, kind, reason, detail, ts) VALUES(?,?,?,?,?)",
               (lead_id, kind, reason, detail[:300], db.now()))


# ------------------------------------------------------------------ 黑名单 / 永不联系

def _norm(kind, value):
    v = (value or "").strip().lower()
    if kind == "tg":
        v = v.lstrip("@")
    elif kind == "wa":
        v = contacts_mod.norm_phone(v)
    elif kind == "company":
        v = leads.company_key(value or "")
    return v


def identities(row, keys_only=False) -> list[tuple[str, str]]:
    """一条线索的所有身份：域名、规范化公司名、邮箱、TG、WhatsApp。
    keys_only=True：只取判重 / 不重复开发用的两个键 —— TG 用户名、WhatsApp 号码（PRD 5.1 / 5.2.2）。"""
    cs = db.loads(row["contacts"], []) if isinstance(row["contacts"], str) else (row["contacts"] or [])
    out = []
    if row["domain"]:
        out.append(("domain", row["domain"].lower()))
    if row["company_name"]:
        out.append(("company", leads.company_key(row["company_name"])))
    for c in cs:
        if c.get("email"):
            out.append(("email", c["email"].lower()))
        if c.get("telegram"):
            out.append(("tg", c["telegram"].lower().lstrip("@")))
        if c.get("whatsapp"):
            w = contacts_mod.norm_phone(c["whatsapp"])
            if w:
                out.append(("wa", w))
    out = [x for x in dict.fromkeys(out) if x[1]]
    return [x for x in out if x[0] in ("tg", "wa")] if keys_only else out


def blacklist_add(kind, value, reason, note="", lead_id=None) -> bool:
    v = _norm(kind, value)
    if not v:
        return False
    n = db.execute("INSERT OR IGNORE INTO contact_blacklist(kind, value, reason, note, lead_id, created_at) "
                   "VALUES(?,?,?,?,?,?)", (kind, v, reason, note[:200], lead_id, db.now()))
    return bool(n)


def blacklist_from_lead(lead_id: int, reason: str) -> list[list[str]]:
    """把一条线索的身份（域名、公司名、邮箱、TG、WhatsApp）加入黑名单；返回新加入的条目（撤销用）。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row:
        return []
    added = []
    for kind, v in identities(row):
        if not db.query_one("SELECT 1 FROM contact_blacklist WHERE kind=? AND value=?", (kind, v)):
            blacklist_add(kind, v, reason, lead_id=lead_id)
            added.append([kind, v])
    return added


def blacklist_remove(items):
    for kind, v in items or []:
        db.execute("DELETE FROM contact_blacklist WHERE kind=? AND value=?", (kind, v))


def blacklisted(name="", domain="", emails=(), tgs=(), was=()) -> str:
    """命中黑名单返回原因（competitor / existing_customer / do_not_contact …），否则空字符串。"""
    checks = [("domain", domain), ("company", name)] + [("email", e) for e in emails] + \
             [("tg", t) for t in tgs] + [("wa", w) for w in was]
    for kind, v in checks:
        v = _norm(kind, v)
        if not v:
            continue
        r = db.query_one("SELECT reason FROM contact_blacklist WHERE kind=? AND value=?", (kind, v))
        if r:
            return r["reason"]
    return ""


def lead_blacklisted(row) -> str:
    for kind, v in identities(row):
        r = db.query_one("SELECT reason FROM contact_blacklist WHERE kind=? AND value=?", (kind, v))
        if r:
            return r["reason"]
    return ""


def set_do_not_contact(lead_id: int, on: bool, reason="customer") -> dict:
    """永不联系（永久）：线索打标记 + 所有身份进黑名单；以后任何来源再出现都会被自动排除。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row:
        from .errors import UserError
        raise UserError("lead_not_found", 404)
    if on:
        db.execute("UPDATE leads SET do_not_contact=1, dnc_reason=?, dnc_at=?, updated_at=? WHERE id=?",
                   (reason, db.now(), db.now(), lead_id))
        blacklist_from_lead(lead_id, "do_not_contact")
        event(lead_id, "do_not_contact", reason)
    else:
        db.execute("UPDATE leads SET do_not_contact=0, dnc_reason='', dnc_at=NULL, updated_at=? WHERE id=?",
                   (db.now(), lead_id))
        db.execute("DELETE FROM contact_blacklist WHERE reason='do_not_contact' AND lead_id=?", (lead_id,))
        event(lead_id, "do_not_contact_off", reason)
    return {"lead_id": lead_id, "do_not_contact": on}


def can_contact(lead_id: int) -> tuple[bool, str]:
    """发送层（第 3 步）每次联系前必须调用：永不联系 / 已退订 / 黑名单 / 已经联系过同一客户 → 不允许。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row:
        return False, "lead_not_found"
    if row["do_not_contact"]:
        return False, "do_not_contact"
    if row["unsubscribed"]:
        return False, "unsubscribed"
    # 审核台打开时只联系审核通过的；v1 没有审核环节：合格 / 已推送给销售的
    if row["status"] not in (("approved",) if leads.review_on() else ("qualified", "pushed", "approved")):
        return False, "not_approved"
    bl = lead_blacklisted(row)
    if bl:
        return False, "blacklist:" + bl
    ids = identities(row, keys_only=True)        # 不重复开发：按 TG / WhatsApp 判断是不是同一个客户
    for kind, v in ids:
        r = db.query_one("SELECT lead_id FROM contact_history WHERE ident_kind=? AND ident_value=? LIMIT 1", (kind, v))
        if r:
            return False, "already_contacted"
    return True, ""


def record_contact(lead_id: int, channel: str, account: str = ""):
    """第 3 步发送成功后调用：按每个身份记一笔，保证同一客户（换了来源 / 换了线索 id）也不会被重复开发。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row:
        return
    now = db.now()
    for kind, v in identities(row, keys_only=True):
        db.execute("INSERT INTO contact_history(lead_id, ident_kind, ident_value, channel, account, ts) "
                   "VALUES(?,?,?,?,?,?)", (lead_id, kind, v, channel, account, now))
    db.execute("UPDATE leads SET contacted=1, status='contacted', first_contact_channel=COALESCE(NULLIF("
               "first_contact_channel,''), ?), first_contact_account=COALESCE(NULLIF(first_contact_account,''), ?), "
               "updated_at=? WHERE id=?", (channel, account, now, lead_id))


# ------------------------------------------------------------------ 重新判定 / 升级

def _template(icp_id) -> dict:
    r = db.query_one("SELECT data FROM icp_templates WHERE id=?", (icp_id,)) if icp_id else None
    return db.loads(r["data"], {}) if r else {}


def _qualified_grades(icp_id) -> set:
    if icp_id:
        r = db.query_one("SELECT data FROM icp_templates WHERE id=?", (icp_id,))
        if r:
            return set(db.loads(r["data"], {}).get("qualified_grades") or ["A", "B"])
    return {"A", "B"}


def requalify(lead_id: int, why: str = "recheck") -> bool:
    """备用线索在条件满足后升级为合格（程序判定，与数据师同一套规则）。返回是否升级。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row or row["status"] != "backup" or row["not_qualified_reason"] not in UPGRADABLE:
        return False
    if row["do_not_contact"] or lead_blacklisted(row):
        return False
    contacts = db.loads(row["contacts"], [])
    profile = db.loads(row["profile"], {})
    hm = profile.get("hard_match") or {}
    hard_ok = not hm or (hm.get("country_ok", True) and hm.get("customer_type_ok", True) and not hm.get("excluded"))
    sources = db.loads(row["sources"], [])
    if row["grade"] not in _qualified_grades(row["icp_id"]) or not hard_ok or not sources:
        return False
    if not contacts_mod.qualifying(contacts):
        return False
    from . import countries
    if countries.country_ok(row["country"], _template(row["icp_id"])) is not True:
        return False
    now = db.now()
    db.execute("UPDATE leads SET qualified=1, status=?, not_qualified_reason='', upgraded_at=?, "
               "updated_at=? WHERE id=?", (leads.qualified_status(), now, now, lead_id))
    event(lead_id, "upgrade", why)
    return True


def _new_signal(row, since: float) -> bool:
    """上次复查以来，同一公司又出现了新的求购信号（例如 TG 群里的求购消息被合并进来）。"""
    r = db.query_one("SELECT COUNT(*) n FROM raw_leads WHERE lead_id=? AND created_at>? AND status='duplicate' "
                     "AND (data LIKE '%\"message_type\": \"buy_request\"%' OR source='tg')", (row["id"], since or 0))
    return bool(r and r["n"])


# ------------------------------------------------------------------ 定期作业：清理 / 过期 / 复查备用池

def cleanup(log=None) -> int:
    """黑名单（同行、已合作客户、永不联系）和广告 → 丢弃。只动还没审核的（待审核 / 备用）。"""
    n = 0
    for row in db.query("SELECT * FROM leads WHERE status IN ('qualified','pending_review','backup')"):
        reason = ""
        bl = lead_blacklisted(row)
        if bl:
            reason = "blacklist_" + bl
        elif (row["message_type"] or "") == "advertisement":
            reason = "advertisement"
        if reason:
            db.execute("UPDATE leads SET status='discarded', qualified=0, not_qualified_reason=?, updated_at=? "
                       "WHERE id=?", (reason, db.now(), row["id"]))
            event(row["id"], "discard", reason)
            n += 1
            if log:
                log("ev.pool_discarded", name=row["company_name"], reason=f"@nq.{reason}")
    return n


def expire_stale(log=None) -> int:
    """求购消息太旧（默认 30 天）还没处理 → 过期；公司信息保留在线索库。"""
    days = float(cfg()["expire_days"])
    cutoff = time.time() - days * 86400
    n = 0
    for row in db.query("SELECT id, company_name FROM leads WHERE status IN ('qualified','pending_review','backup') AND "
                        "message_type='buy_request' AND posted_at IS NOT NULL AND posted_at<?", (cutoff,)):
        db.execute("UPDATE leads SET status='expired', qualified=0, not_qualified_reason='expired', updated_at=? "
                   "WHERE id=?", (db.now(), row["id"]))
        event(row["id"], "expire", f"{int(days)}d")
        n += 1
        if log:
            log("ev.pool_expired", name=row["company_name"], days=int(days))
    return n


def _resolve_country(row, pages, found) -> str:
    """国家不明的备用线索：用官网文字、域名后缀、电话区号再判断一次（和客户说的目标国家比对）。"""
    from . import countries
    contacts = db.loads(row["contacts"], []) + found
    phones = [c.get("whatsapp") for c in contacts if c.get("whatsapp")] + [ph for p in pages for ph in p.get("phones") or []]
    code, _ = countries.infer_country(" ".join(p.get("markdown", "") for p in pages), row["domain"], phones)
    return code


def _enrich(row, crawler) -> list[dict]:
    """备用池补全：官网首页 + 联系/关于页 + 官网上链接的 Facebook / 领英公开页。返回新找到的联系方式。"""
    from .sources import crawler as crawler_mod
    contacts = db.loads(row["contacts"], [])
    have_wa = {contacts_mod.norm_phone(c.get("whatsapp", "")) for c in contacts if c.get("whatsapp")}
    have_tg = {(c.get("telegram") or "").lower() for c in contacts if c.get("telegram")}
    pages = []
    try:
        home = crawler.fetch(row["website"])
        pages.append(home)
        subs = crawler_mod.key_pages(home, limit=2)
        social = [h for h, _ in home.get("links", []) if any(s in h for s in ("facebook.com/", "linkedin.com/company/"))]
        for u in subs + social[:2]:
            try:
                pages.append(crawler.fetch(u))
            except crawler_mod.Skip:
                continue
    except crawler_mod.Skip:
        pass
    found = []
    for p in pages:
        for w in p.get("whatsapp") or []:
            n = contacts_mod.norm_phone(w)
            if n and n not in have_wa:
                have_wa.add(n)
                found.append({"name": "", "title": "", "email": "", "telegram": "", "whatsapp": n, "wa_explicit": True,
                              "source_url": p["url"], "verified": True})
        for t in p.get("telegram_links") or []:
            if t.lower() not in have_tg:
                have_tg.add(t.lower())
                found.append({"name": "", "title": "", "email": "", "telegram": t, "source_url": p["url"],
                              "verified": True})
    row_pages[row["id"]] = pages
    return found


row_pages: dict[int, list] = {}


def _merge_if_duplicate(row, found, log=None) -> bool:
    """复查时新找到的 TG / WhatsApp：在黑名单里 → 丢弃；已经属于另一条线索 → 两条是同一个客户，
    把这条备用线索的联系人和来源并入那一条（保留全部来源），这条记为“重复”丢弃。"""
    tgs = [c["telegram"] for c in found if c.get("telegram")]
    nums = leads.numbers_of(found)
    bl = blacklisted("", "", [], tgs, nums)
    if bl:
        db.execute("UPDATE leads SET status='discarded', qualified=0, not_qualified_reason=?, pool_checked_at=?, "
                   "updated_at=? WHERE id=?", ("blacklist_" + bl, db.now(), db.now(), row["id"]))
        event(row["id"], "discard", "blacklist_" + bl)
        if log:
            log("ev.pool_discarded", name=row["company_name"], reason=f"@nq.blacklist_{bl}")
        row_pages.pop(row["id"], None)
        return True
    other = None
    for kind, vals in (("tg", [t.lower().lstrip("@") for t in tgs]), ("wa", nums)):
        for v in vals:
            r = db.query_one("SELECT lead_id FROM lead_identities WHERE kind=? AND value=? AND lead_id<>?",
                             (kind, v, row["id"]))
            if r and db.query_one("SELECT 1 FROM leads WHERE id=?", (r["lead_id"],)):
                other = r["lead_id"]
                break
        if other:
            break
    if not other:
        return False
    with leads.write_lock:
        leads.merge_into(other, db.loads(row["contacts"], []) + found, db.loads(row["sources"], []))
        db.execute("UPDATE leads SET status='discarded', qualified=0, not_qualified_reason='duplicate', "
                   "pool_checked_at=?, updated_at=? WHERE id=?", (db.now(), db.now(), row["id"]))
    event(row["id"], "merge", "duplicate", f"→ #{other}")
    if log:
        log("ev.pool_discarded", name=row["company_name"], reason="@nq.duplicate")
    row_pages.pop(row["id"], None)
    return True


def recheck_backup(log=None, crawler=None, force=False, should_stop=None) -> dict:
    """复查到期的备用线索（默认每 7 天一次）：补全联系方式 → 能升级就升级。"""
    from .sources import crawler as crawler_mod
    c = cfg()
    due_before = time.time() - float(c["recheck_days"]) * 86400
    rows = db.query("SELECT * FROM leads WHERE status='backup' AND (? OR pool_checked_at IS NULL OR pool_checked_at<?) "
                    "ORDER BY COALESCE(pool_checked_at,0), id LIMIT ?", (1 if force else 0, due_before, int(c["batch"])))
    if crawler is None:
        from . import llm
        crawler = crawler_mod.Crawler(mock=llm.get_config("analyst").provider == "mock",
                                      should_stop=should_stop or (lambda: False), max_pages=int(c["batch"]) * 5)
    st = {"checked": 0, "upgraded": 0, "new_contacts": 0}
    for row in rows:
        if should_stop and should_stop():
            break
        st["checked"] += 1
        since = row["pool_checked_at"] or row["created_at"]
        found = _enrich(row, crawler) if row["website"] else []
        if found and _merge_if_duplicate(row, found, log):
            continue
        if found:
            leads.merge_into(row["id"], found, [], requalify=False)
            st["new_contacts"] += len(found)
            if log:
                log("ev.pool_new_contact", name=row["company_name"], n=len(found))
        why = "new_contact" if found else "recheck"
        if not row["country"]:
            from . import countries
            code = _resolve_country(row, row_pages.pop(row["id"], []), found)
            if code:
                db.execute("UPDATE leads SET country=?, updated_at=? WHERE id=?", (code, db.now(), row["id"]))
                if countries.country_ok(code, _template(row["icp_id"])) is False:
                    db.execute("UPDATE leads SET status='discarded', not_qualified_reason='wrong_country' WHERE id=?",
                               (row["id"],))
                    event(row["id"], "discard", "wrong_country")
                    if log:
                        log("ev.pool_discarded", name=row["company_name"], reason="@nq.wrong_country")
                    db.execute("UPDATE leads SET pool_checked_at=? WHERE id=?", (db.now(), row["id"]))
                    continue
                if row["not_qualified_reason"] == "country_unknown":
                    db.execute("UPDATE leads SET not_qualified_reason='no_tg_wa' WHERE id=? AND ?=0",
                               (row["id"], int(contacts_mod.qualifying(db.loads(row["contacts"], []) + found))))
                why = "country"
        if row["not_qualified_reason"] == "grade" and _new_signal(row, since):
            g = {"C": "B", "B": "A"}.get(row["grade"])
            if g:
                db.execute("UPDATE leads SET grade=?, updated_at=? WHERE id=?", (g, db.now(), row["id"]))
                event(row["id"], "grade_up", "new_signal", f"{row['grade']}→{g}")
                why = "new_signal"
        db.execute("UPDATE leads SET pool_checked_at=? WHERE id=?", (db.now(), row["id"]))
        if requalify(row["id"], why):
            st["upgraded"] += 1
            if log:
                log("ev.pool_upgraded", name=row["company_name"], why=f"@pool.why.{why}")
    return st


def run(task_run) -> dict:
    """数据师的线索池作业（作为一个任务运行：在工作台和实时视图里能看到它在做什么）。"""
    def log(key, **p):
        task_run.emit("analyst", key, **p)
    task_run.emit("analyst", "ev.pool_started")
    d = cleanup(log)
    e = expire_stale(log)
    r = recheck_backup(log, should_stop=lambda: task_run.cancel_flag.is_set())
    task_run.bump(processed=r["checked"], qualified=r["upgraded"], discarded=d)
    task_run.emit("analyst", "ev.pool_done", level="success", state="done", checked=r["checked"], upgraded=r["upgraded"],
                  contacts=r["new_contacts"], discarded=d, expired=e)
    st = settings.get("lead_pool", {}) or {}
    st["last_run"] = time.time()
    settings.put("lead_pool", st)
    return {**r, "discarded": d, "expired": e}


def due() -> bool:
    c = cfg()
    return time.time() - float(c.get("last_run") or 0) >= float(c["run_every_h"]) * 3600


# ------------------------------------------------------------------ 线索池状况（日报 / 推送 / 线索库）

def status(lg=None) -> dict:
    t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    week = time.time() - 7 * 86400
    new = db.query_one("SELECT COUNT(*) n FROM raw_leads WHERE created_at>=?", (t0,))["n"]
    q = db.query_one("SELECT COALESCE(SUM(qualified=1),0) q, COALESCE(SUM(status='backup'),0) b FROM leads "
                     "WHERE created_at>=?", (t0,))
    disc = db.query_one("SELECT COUNT(*) n FROM raw_leads WHERE created_at>=? AND status IN "
                        "('discarded','filtered','duplicate')", (t0,))["n"] + \
        db.query_one("SELECT COUNT(*) n FROM pool_events WHERE ts>=? AND kind='discard'", (t0,))["n"]
    up = db.query_one("SELECT COUNT(*) n FROM pool_events WHERE ts>=? AND kind='upgrade'", (week,))["n"]
    per = db.query("SELECT source, COUNT(*) n, COALESCE(SUM(status='qualified'),0) q FROM raw_leads WHERE created_at>=? "
                   "GROUP BY source ORDER BY n DESC", (week,))
    zones = db.query_one("SELECT COALESCE(SUM(qualified=1),0) qualified, COALESCE(SUM(status='backup'),0) backup, "
                         "COALESCE(SUM(status='discarded'),0) discarded, COALESCE(SUM(status='expired'),0) expired, "
                         "COALESCE(SUM(deal=1),0) won FROM leads")
    pending = db.query_one("SELECT COUNT(*) n FROM raw_leads WHERE status IN ('new','processing')")["n"]
    return {"today": {"new": new, "qualified": q["q"], "backup": q["b"], "discarded": disc},
            "upgrades_week": up, "per_source": [{"source": r["source"], "n": r["n"], "qualified": r["q"],
                                                 "rate": round(r["q"] / r["n"], 3) if r["n"] else 0} for r in per],
            "zones": {**zones, "pending": pending}}


def status_lines(lg=None) -> list[str]:
    s = status()
    t = s["today"]
    lines = [i18n.t("pool.push_title", None, lg),
             i18n.t("pool.push_today", {"new": t["new"], "q": t["qualified"], "b": t["backup"], "d": t["discarded"]}, lg),
             i18n.t("pool.push_upgrades", {"n": s["upgrades_week"]}, lg)]
    if s["per_source"]:
        lines.append(i18n.t("pool.push_rate", {"x": " · ".join(
            f"{i18n.t('source.' + p['source'], None, lg)} {round(p['rate'] * 100)}%" for p in s["per_source"])}, lg))
    return lines

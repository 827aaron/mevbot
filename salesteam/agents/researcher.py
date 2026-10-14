"""🔍 调研员：按客户勾选的数据源主动搜索采集，写入线索池（raw_leads），交给数据师。

数据源：
- google  : Claude 厂商 → Claude 服务端联网搜索；其他厂商 → Serper / Brave / 免费兜底 搜索结果 + 爬虫读页面
- web     : 网页爬虫（Scrapling）。客户粘贴的论坛/名录/行业网站网址；没给网址时先搜索名录类网站
- youtube : YouTube Data API v3（频道简介 + 视频简介 + 评论区识别买家）

原则：AI 只能从页面原文提取信息；邮箱必须在原文里出现过（防止编造）；每条线索带来源网址和原文摘录。
"""
import json
import re
import time
import urllib.parse

from .. import countries, db, edition, llm, prompts, settings, source_pool, social
from ..errors import BudgetExceeded, UserError
from ..sources import crawler as crawler_mod
from ..sources import search as search_mod
from ..sources import youtube as yt
from . import memory, schemas

AGENT = "researcher"
SKIP_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
              "pinterest.", "wikipedia.org", "amazon.", "ebay.", "alibaba.com", "aliexpress.", "made-in-china.com",
              "youtube.com")


def icp_brief(icp: dict, lang="en") -> str:
    return (f"Customer's product / service: {icp.get('product', '')}\n"
            f"Registration countries: {', '.join(f'{c} ({countries.name(c)})' for c in icp.get('countries', [])) or 'any (the customer did not name countries)'}\n"
            f"Target customer types: {', '.join(icp.get('customer_types', []))}\n"
            f"Industries: {', '.join(icp.get('industries', [])) or '-'}\n"
            f"Company size: {icp.get('company_size') or '-'}\n"
            f"Product words (names and aliases of the customer's products): {', '.join(icp.get('product_words', [])) or '-'}\n"
            f"Buy words (what target customers write when they want to buy): {', '.join(icp.get('buy_words', [])) or '-'}\n"
            f"Exclude words: {', '.join(icp.get('exclude_words', [])) or '-'}\n"
            f"Exclude (companies): {', '.join(icp.get('exclude_companies', [])) or '-'}\n"
            f"Reference customers: {', '.join(icp.get('reference_customers', [])) or '-'}\n"
            f"Contact titles wanted: {', '.join(icp.get('contact_titles', [])) or '-'}" + edition.market_instructions(icp))


# 提示词在提示词库：researcher.extract / researcher.web_research / researcher.youtube_task
# 搜索词不再让大模型规划：程序用模板的 产品词 × 求购词 + 国家 组合（PRD §3，icp.search_queries）


class Researcher:
    def __init__(self, run, icp: dict):
        self.run = run
        self.icp = icp
        self.cfg = llm.get_config(AGENT)
        self.mock = self.cfg.provider == "mock"
        self.mock_ctx = {"countries": icp.get("countries") or ["MY"],      # 只给测试模式的假网站用
                         "keyword": (icp.get("product_words") or ["Solar"])[0]} if self.mock else None
        from . import icp as icp_mod
        self.icp_mod = icp_mod
        self.words = icp_mod.lead_words(icp)
        extra_words = source_pool.keywords_for(run.icp_id)
        self.words["product"] = list(dict.fromkeys(self.words["product"] + extra_words))
        self.crawler = crawler_mod.Crawler(mock=self.mock, should_stop=run.should_stop)
        if edition.current()["id"] == "my_igaming":
            self.crawler.cfg["render_js"] = True
            with run.stats_lock:
                run.stats["crawler_mode"] = "auto_http_browser"
        self.seen_urls: set[str] = set()
        self.discovered_urls: set[str] = set()
        self.fetch_failures = []
        self.discovery_context = {"expanded": 0, "max_expanded": 10, "max_depth": 2, "round_roots": True}
        self.search_cfg = settings.get("search")
        self.lang = settings.get("ui_lang")
        # 岗位说明书 + 模板：同一任务内不变，放在 system 里（Claude 提示缓存）
        self.extract_system = prompts.render("researcher.extract", icp=icp_brief(icp), memos=memory.memos_block(AGENT))
        # 断点续跑（看门狗重启后）：跳过上一个任务已经读过的网址和搜索过的词
        self.done_queries: set[str] = set()
        prev = run.params.get("resume_from")
        while prev:
            for e in db.query("SELECT key, params FROM task_events WHERE task_id=? AND key IN "
                              "('ev.fetching','ev.searching','ev.youtube_searching','ev.claude_searching')", (prev,)):
                p = db.loads(e["params"], {})
                if e["key"] == "ev.fetching":
                    self.seen_urls.add(p.get("url", ""))
                else:
                    self.done_queries.add(("yt:" if "youtube" in e["key"] else "") + str(p.get("query", "")).lower())
            row = db.query_one("SELECT params FROM tasks WHERE id=?", (prev,))
            prev = db.loads(row["params"], {}).get("resume_from") if row else None

    # ------------------------------------------------------------ 工具
    def emit(self, _key, level="info", **p):
        self.run.emit(AGENT, _key, level=level, state="working", **p)

    def stop(self):
        return self.run.should_stop()

    def plan_queries(self, source: str, n: int) -> list[dict]:
        """搜索词 = 模板的 产品词 × 求购词 + 国家（PRD §3），不调用模型。同一个任务里不同渠道错开，免得搜一样的。"""
        qps = int(self.search_cfg["queries_per_source"])
        offset = {"google": 0, "web": qps, "youtube": 2 * qps}.get(source, 0)
        # Manager retries explore later combinations from the same approved ICP.
        # The program cap is enforced again here; no setting or filter is changed.
        retry = max(0, min(3, int(self.run.params.get("manager_query_offset") or 0)))
        offset += retry * 3 * qps
        qs = self.icp_mod.search_queries(self.icp, n + offset)[offset:offset + n] or \
            self.icp_mod.search_queries(self.icp, n)
        extras = source_pool.keywords_for(self.run.icp_id)
        if extras:
            # User features and grounded learned expressions supplement the
            # approved template, without overwriting any of its fields.
            qs = [{"q": word, "country": "", "lang": ""} for word in extras[:max(1, n // 2)]] + qs
            qs = qs[:n]
        if not qs:
            self.emit("ev.no_lead_words", level="warn")
        return qs

    def page_ok(self, text: str, url: str) -> bool:
        """线索词规则（PRD §3）：网页 / 视频里至少要有一个求购词或产品词，才交给 AI 读（省钱）；排除词按每个候选判断
        （一个名录页里有很多公司，不能因为一个词丢掉整页 —— 数据师按候选的内容排除）。没设线索词时不拦。"""
        if edition.signal_mode(self.icp) and edition.validate_b2b(self.icp, [{"url": url, "text": text}])["status"] == "matched":
            return True
        if not (self.words["buy"] or self.words["product"]):
            return True
        w = {**self.words, "exclude": []}
        if self.icp_mod.match_words(text, w) == "hit":
            return True
        self.run.bump(filtered_pages=1)
        self.emit("ev.page_no_words", url=url)
        return False

    def add_candidates(self, source: str, cands: list[dict], page_text: str | None, page_url: str,
                       page_title: str = "", allowed_urls: set | None = None, post_context=None) -> int:
        """把 AI 提取的候选写入线索池。page_text 不为空时，邮箱/TG 必须在原文中出现。"""
        added = 0
        low = (page_text or "").lower()
        for c in cands:
            name = str(c.get("company_name") or "").strip()
            if not name:
                continue
            emails = [e.strip().lower() for e in c.get("emails") or [] if crawler_mod.EMAIL_RE.fullmatch(e.strip())]
            tgs = [t.strip() for t in c.get("telegram") or [] if t and t.strip()]
            was = [w for w in (c.get("whatsapp") or []) if w]
            if page_text is not None:
                emails = [e for e in emails if e in low]
                tgs = [t for t in tgs if t.lower().lstrip("@") in low]
                digits = re.sub(r"\D", "", page_text)
                was = [w for w in was if re.sub(r"\D", "", w) and re.sub(r"\D", "", w) in digits]
            src_url = str(c.get("source_url") or "").strip() or page_url
            if allowed_urls is not None and src_url not in allowed_urls and page_url:
                src_url = page_url if page_url in allowed_urls else src_url
            c = {**c, "company_name": name, "emails": emails, "telegram": tgs, "whatsapp": was, "source_url": src_url,
                 "country": countries.normalize_country(c.get("country", "")),
                 "verified_text": page_text is not None}
            if not str(c.get("evidence_quote") or "").strip():
                continue   # 没有依据的不收
            if edition.served_market(self.icp):
                quote = str(c.get("evidence_quote") or "").strip()
                # A model quote is evidence only when it appears in the actual read text.
                verified = page_text is not None and quote in page_text and src_url == page_url
                c["verified_evidence"] = {"url": src_url, "quote": quote} if verified else None
                if verified and post_context and post_context.get("author") and post_context.get("url") == page_url:
                    # Author metadata is parsed from the actual post DOM, never
                    # filled by the extraction model or another group member.
                    author = str(post_context["author"]).strip()
                    if name.lstrip("@").casefold() == author.lstrip("@").casefold():
                        c["post"] = dict(post_context)
                        c["verified_evidence"].update(origin="public_post", author=author)
            rid = db.execute(
                "INSERT INTO raw_leads(task_id, source, source_url, source_title, company_name, website, country, data, "
                "evidence, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.run.id, source, src_url, page_title, name, c.get("website") or "", c["country"],
                 db.dumps({**c, "page_excerpt": (page_text or "")[:3000]}), c.get("evidence_quote", "")[:1000],
                 "new", db.now()))
            self.run.lead_queue.put(rid)
            added += 1
        if added:
            snap = self.run.bump(pool=added)
            self.emit("ev.pool_added", count=added, total=snap["pool"])
        return added

    def extract_page(self, source: str, page: dict) -> int:
        if self.run.icp_id:
            source_pool.discover_page(self.run.icp_id, page, self.discovery_context)
        original = page.get("units")
        if original is None:
            original = [{"url": page["url"], "text": page["markdown"], "links": page.get("links", [])}]
        original = [u for u in original if str(u.get("text") or "").strip() and u.get("url")]
        units = source_pool.pending_units(self.run.icp_id, page) if self.run.icp_id else original
        changed = sum(bool(u.get("checkpoint_changed")) for u in units)
        new, unchanged = len(units) - changed, max(0, len(original) - len(units))
        self.run.bump(source_new_items=new, source_unchanged_items=unchanged, source_changed_items=changed)
        source_pool.record_scan(page["url"], new, unchanged, changed)
        added = 0
        for unit in units:
            if self.stop():
                break
            # A post/comment is evaluated independently of its neighbours.
            item = {"url": unit["url"], "title": page.get("title", ""), "markdown": unit["text"],
                    "emails": page.get("emails", []) if not page.get("units") else [],
                    "links": unit.get("links", []), "access": page.get("access", "public"),
                    "post": {"url": unit["url"], "id": unit.get("id", ""), "author": unit.get("author", ""),
                             "platform": unit.get("platform") or page.get("platform") or "forum"}}
            added += self._extract_unit(source, item)
            # An extraction error propagates before this point; the content
            # remains eligible on the next run. Raw rows are durable by now.
            if self.run.icp_id:
                source_pool.ack_unit(self.run.icp_id, unit)
        return added

    def _extract_unit(self, source: str, page: dict) -> int:
        if not self.page_ok(page.get("title", "") + "\n" + page["markdown"], page["url"]):
            return 0
        self.emit("ev.ai_reading", url=page["url"])
        user = (f"Page URL: {page['url']}\nPage title: {page.get('title', '')}\n\n"
                f"Post author (read metadata): {(page.get('post') or {}).get('author', '')}\n"
                "For a professional agent's own statement, use this actual author as the candidate name.\n"
                f"Page content (markdown):\n{page['markdown']}")
        data, _ = llm.call_json(AGENT, "extract_leads", self.extract_system, user, schemas.CANDIDATES, task_id=self.run.id,
                                context={"text": page["markdown"], "url": page["url"], "icp": self.icp,
                                         "lang": self.lang})
        cands = data.get("candidates") or []
        if not cands:
            self.emit("ev.page_no_leads", url=page["url"])
            return 0
        return self.add_candidates(source, cands, page["markdown"] + " " + " ".join(page.get("emails") or []),
                                   page["url"], page.get("title", ""), post_context=page.get("post"))

    def fetch(self, url):
        if url in self.seen_urls or not self.run.step(f"url:{url}", AGENT):
            return None
        self.seen_urls.add(url)
        self.emit("ev.fetching", url=url)
        try:
            entry = source_pool.entry_for(url)
            platform = social.platform_for(url)
            if platform:
                access = (entry or {}).get("access", "public")
                if access == "public" and platform not in ("youtube", "x", "reddit"):
                    # Use Scrapling for readable public pages. Dynamic/login
                    # walls need the separate owner-authorized browser path.
                    try:
                        p = self.crawler.fetch(url)
                    except crawler_mod.Skip as exc:
                        if exc.key not in ("ev.crawl_failed",):
                            raise
                        p = None
                    if not p or not edition.validate_b2b(self.icp, [{"url": url, "text": p.get("markdown", "")}]).get("evidence"):
                        self.crawler.reserve_rendered(url)
                        p = social.read(url, "public", self.stop)
                else:
                    self.crawler.reserve_rendered(url, authenticated=access == "login")
                    p = social.read(url, access, self.stop,
                                    int(settings.get("youtube")["max_comments_per_video"]))
            else:
                p = self.crawler.fetch(url)
            self.run.bump(pages=1)
            if entry:
                source_pool.record_read(url, "done")
            return p
        except UserError as e:
            self.fetch_failures.append(e)
            source_pool.record_read(url, "failed", e.key)
            self.emit("ev.source_failed", level="error", **{**e.params, "source": "@source.web", "error": e.key, "url": url})
            raise
        except crawler_mod.Skip as e:
            source_pool.record_read(url, "failed", e.key)
            if e.key in ("ev.cancelled",):
                return None
            self.fetch_failures.append(UserError("source_read_failed", reason=e.key, url=url))
            self.emit(e.key, level="warn", **e.params)
            if e.key in ("ev.crawl_task_limit", "ev.crawl_daily_limit"):
                raise
            return None

    @staticmethod
    def skip_url(url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return not host or any(s in host for s in SKIP_HOSTS)

    # ------------------------------------------------------------ 主流程
    def execute(self):
        srcs = [s for s in self.run.params["sources"] if hasattr(self, f"source_{s}")]   # TG 群由监控员负责
        self.emit("ev.researcher_start", sources=",".join(f"@source.{s}" for s in srcs))
        for src in srcs:
            if self.stop():
                break
            try:
                self._source_status(src, "working")
                getattr(self, f"source_{src}")()
                self._source_status(src, "done")
            except BudgetExceeded:
                raise
            except crawler_mod.Skip:
                continue
            except UserError as e:
                if e.key in ("key_missing", "key_invalid", "model_not_found", "base_url_missing", "model_missing",
                             "ollama_unreachable", "key_no_permission"):
                    raise
                self.note(src, e.key)
                self._source_status(src, "failed", e.key, e.params)
                self.emit("ev.source_failed", level="error", source=f"@source.{src}", error=e.key, **e.params)
        states = self.run.stats.get("source_status", {})
        failed = bool(srcs) and all(states.get(src, {}).get("status") == "failed" for src in srcs)
        state = "error" if failed else "done"
        self.run.agent_state.setdefault(AGENT, {})["state"] = state
        self.run.emit(AGENT, "ev.researcher_sources_failed" if failed else "ev.researcher_done",
                      level="error" if failed else "success", state=state, pool=self.run.stats["pool"])

    # ---- 每个来源的结果说明（任务没找够时用大白话告诉客户为什么，PRD §4）
    def note(self, src: str, why: str):
        self.run.source_notes.setdefault(src, why)      # 先记下的更具体（例如“没有网址，自动找名录也被拦住”）

    def _source_status(self, source, status, error="", params=None):
        with self.run.stats_lock:
            states = self.run.stats.setdefault("source_status", {})
            states[source] = {"status": status, "error": error, "params": params or {}}
        self.run.bump()

    def _search_diagnostic(self, query, status, provider="", params=None):
        snapshot = dict((getattr(self.crawler, "_free_search", {}) or {}).get("last", {}) or {})
        item = {"query": query, "status": status, "provider": provider, "params": params or {}, "fallback": snapshot}
        with self.run.stats_lock:
            history = self.run.stats.setdefault("search_diagnostics", [])
            history.append(item)
            del history[:-30]
        self.run.bump()

    FATAL_SEARCH = ("search_key_invalid", "search_quota", "search_blocked")

    def run_search(self, q: dict, n: int):
        """搜一个词。失败（免费搜索被拦、网络问题…）返回 None 并记下原因，接着搜下一个词（不因为一次失败放弃整个来源）。"""
        self.emit("ev.searching", query=q["q"], country=q["country"])
        try:
            results, provider = search_mod.search(q["q"], q["country"], q["lang"], n, crawler=self.crawler,
                                                  mock_ctx=self.mock_ctx)
        except UserError as e:
            self.search_errors.append(e)
            self._search_diagnostic(q["q"], "failed", e.params.get("provider", ""), e.params)
            # Every free endpoint marked dead will remain unavailable for this
            # task. Rotating queries and sleeping cannot revive those endpoints.
            st = getattr(self.crawler, "_free_search", {}) or {}
            exhausted = (bool(st.get("last", {}).get("failed")) and not st.get("last", {}).get("empty") and
                         all(name in st.get("dead", {}) for name in search_mod.FREE_ENDPOINTS))
            terminal = e.key in self.FATAL_SEARCH or exhausted
            self.emit("ev.search_unavailable" if terminal else "ev.search_query_failed", level="warn",
                      **{**e.params, "query": q["q"], "error": f"@errors.{e.key}"})
            if terminal:
                raise
            if not self.mock:
                self.pause_jitter(3, 8)              # 被拦后隔一会儿再搜下一个词
            return None
        self._search_diagnostic(q["q"], "done", provider)
        urls = {r.get("url") for r in results if r.get("url")}
        new = urls - self.discovered_urls
        self.discovered_urls.update(urls)
        self.run.bump(search_results=len(results), discovered=len(new))
        self.emit("ev.search_results", count=len(results), provider=f"@search.{provider}")
        return results

    def pause_jitter(self, lo: float, hi: float):
        import random
        end = time.time() + random.uniform(lo, hi)
        while time.time() < end and not self.stop():
            time.sleep(0.2)

    def all_failed(self, tried: int, ok: int):
        """这个来源的搜索词全部失败 → 报最后一个原因（整个来源算失败）。"""
        if tried and not ok and self.search_errors:
            raise self.search_errors[-1]

    # ---- Google 搜索
    def source_google(self):
        n = int(self.search_cfg["queries_per_source"])
        use_claude = self.cfg.provider == "claude" and self.search_cfg["provider"] in ("auto", "claude")
        self.emit("ev.planning_queries", source="@source.google")
        queries = self.plan_queries("google", n)
        self.run.bump(queries=len(queries))
        self.search_errors = []
        tried = ok = 0
        initial_failure = len(self.fetch_failures)
        pages_read = 0
        for q in queries:
            if self.stop():
                return
            if not self.run.step(f"search:{q['q'].lower()}", AGENT) or q["q"].lower() in self.done_queries:
                continue
            self.done_queries.add(q["q"].lower())
            if use_claude:
                self.claude_search(q)
                continue
            tried += 1
            results = self.run_search(q, int(self.search_cfg["results_per_query"]))
            if results is None:
                continue
            ok += 1
            for r in results:
                if self.stop():
                    return
                if self.skip_url(r["url"]) and not self.mock:
                    continue
                try:
                    page = self.fetch(r["url"])
                except UserError:
                    continue  # An unavailable platform must not hide other results.
                if page:
                    pages_read += 1
                    self.extract_page("google", page)
        self.all_failed(tried, ok)
        if not pages_read and len(self.fetch_failures) > initial_failure and not self.stop():
            raise self.fetch_failures[initial_failure]

    def claude_search(self, q: dict):
        self.emit("ev.claude_searching", query=q["q"], country=q["country"])
        user = prompts.render("researcher.web_research_task", icp=icp_brief(self.icp), query=q["q"],
                              country=q["country"] or "any target", lang=q["lang"] or "local")
        resp = llm.call(AGENT, "claude_web_research", prompts.get("researcher.web_research"), user,
                        schema=schemas.CANDIDATES,
                        web_search=True, task_id=self.run.id,
                        on_status=lambda k, p: self.emit(k, **p))
        data = llm._parse_json(resp.text) or {}
        cands = data.get("candidates") or []
        allowed = {s["url"] for s in resp.sources}
        # Claude 联网路径下没有页面原文：来源必须是本次搜索真实看过的网页之一，否则丢弃
        cands = [c for c in cands if str(c.get("source_url") or "") in allowed or
                 any(str(c.get("source_url") or "").rstrip("/") == u.rstrip("/") for u in allowed)]
        if not cands:
            self.emit("ev.page_no_leads", url=q["q"])
            return
        self.add_candidates("google", cands, None, "", "Claude web search", allowed_urls=allowed)

    # ---- 网页爬虫
    def source_web(self):
        urls = list(dict.fromkeys(list(self.run.params.get("urls") or []) +
                                 (source_pool.urls() if source_pool.config()["icp_id"] == self.run.icp_id else [])))
        bootstrap = False
        if not urls and not self.mock and edition.current()["id"] == "my_igaming":
            bootstrap = True
            self.run.emit("manager", "ev.source_seed_planning", state="working")
            try:
                urls = source_pool.bootstrap_urls(self.run.icp_id, self.run.id)
            except UserError as exc:
                self.run.emit("manager", "ev.source_seed_failed", level="warn", state="error", error=exc.key)
                raise
            self.run.emit("manager", "ev.source_seed_proposed", state="done", count=len(urls))
            if not urls:
                raise UserError("source_bootstrap_empty")
        if not urls:
            self.emit("ev.no_urls_search_directories")
            n = max(2, int(self.search_cfg["queries_per_source"]) // 2)
            self.search_errors = []
            tried = ok = 0
            for q in self.plan_queries("web", n):
                if self.stop():
                    return
                if not self.run.step(f"search:{q['q'].lower()}", AGENT) or q["q"].lower() in self.done_queries:
                    continue
                self.done_queries.add(q["q"].lower())
                if self.cfg.provider == "claude" and self.search_cfg["provider"] in ("auto", "claude"):
                    self.claude_search(q)
                    continue
                tried += 1
                try:
                    results = self.run_search(q, 5)
                except UserError:
                    self.note("web", "no_urls_blocked")
                    raise
                if results is None:
                    continue
                ok += 1
                urls += [r["url"] for r in results if self.mock or not self.skip_url(r["url"])]
            if tried and not ok and self.search_errors:
                self.note("web", "no_urls_blocked")      # 没有网址，自动找名录也被拦住
                raise self.search_errors[-1]
        follow = self.run.params.get("follow_links", True)
        per_site = int(settings.get("crawler")["max_pages_per_site"])
        first_failure = len(self.fetch_failures)
        failures = []
        read_count = 0
        for url in urls:
            if self.stop():
                return
            url = crawler_mod.normalize_url(url)
            try:
                page = self.fetch(url)
            except UserError as exc:
                failures.append(exc)
                continue
            if not page:
                continue
            read_count += 1
            if bootstrap:
                discovered = source_pool.accept_bootstrap_page(self.run.icp_id, page)
                self.emit("ev.source_seed_read", url=page["url"], count=int(bool(discovered)))
            self.extract_page("web", page)
            if social.platform_for(page["url"]):
                continue  # Never follow registration or account/action links.
            if follow and not self.mock:
                # 名录分页 / 列表子页：只跟进同站点、看起来像列表或分页的链接
                host = urllib.parse.urlparse(page["url"]).netloc
                subs = []
                for href, text in page.get("links", []):
                    u = urllib.parse.urlparse(href)
                    if u.netloc != host or href in self.seen_urls or href.split("#")[0] == page["url"]:
                        continue
                    if re.search(r"page|seite|pagina|list|member|exhibitor|directory|categor|[?&]p=\d", href + text, re.I):
                        subs.append(href.split("#")[0])
                for sub in list(dict.fromkeys(subs))[:max(0, per_site - 1)]:
                    if self.stop():
                        return
                    try:
                        p2 = self.fetch(sub)
                    except UserError as exc:
                        failures.append(exc)
                        continue
                    if p2:
                        self.extract_page("web", p2)
        if failures and not read_count:
            raise failures[0]
        if not read_count and len(self.fetch_failures) > first_failure and not self.stop():
            raise self.fetch_failures[first_failure]

    # ---- YouTube
    def youtube_crawler(self):
        """Public rendered comments need no key or login; use a session only by choice."""
        urls = list(dict.fromkeys(source_pool.urls("youtube") +
                [u for u in self.run.params.get("urls", []) if social.platform_for(u) == "youtube"]))
        self.search_errors = []
        if not urls:
            for query in self.plan_queries("youtube", 2):
                results = self.run_search({**query, "q": "site:youtube.com/watch " + query["q"]}, 5)
                if results:
                    urls.extend(r["url"] for r in results if social.platform_for(r["url"]) == "youtube")
            if not urls and self.search_errors:
                raise self.search_errors[-1]
        errors, read = [], 0
        for url in list(dict.fromkeys(urls))[:int(settings.get("youtube")["max_videos_per_query"])]:
            if self.stop():
                return
            try:
                page = self.fetch(url)
                if page:
                    read += 1
                    self.extract_page("youtube", page)
            except UserError as exc:
                errors.append(exc)
        if errors and not read:
            raise errors[0]

    def source_youtube(self):
        if not self.mock and not settings.get_secret("youtube_api_key"):
            return self.youtube_crawler()
        n = max(2, int(self.search_cfg["queries_per_source"]) // 2)
        self.emit("ev.planning_queries", source="@source.youtube")
        queries = self.plan_queries("youtube", n)
        self.run.bump(queries=len(queries))
        for q in queries:
            if self.stop():
                return
            if not self.run.step(f"yt:{q['q'].lower()}", AGENT) or ("yt:" + q["q"].lower()) in self.done_queries:
                continue
            self.done_queries.add("yt:" + q["q"].lower())
            self.emit("ev.youtube_searching", query=q["q"], country=q["country"])
            videos = yt.collect(q["q"], q["country"], q["lang"], mock_ctx=self.mock_ctx)
            self.emit("ev.youtube_videos", count=len(videos))
            for v in videos:
                if self.stop():
                    return
                if v["url"] in self.seen_urls:
                    continue
                self.seen_urls.add(v["url"])
                text = yt.as_text(v)
                if not self.page_ok(text, v["url"]):
                    continue
                self.emit("ev.youtube_reading", title=v.get("title", "")[:80], comments=len(v.get("comments", [])))
                user = prompts.render("researcher.youtube_task", text=text)
                data, _ = llm.call_json(AGENT, "extract_leads", self.extract_system, user, schemas.CANDIDATES,
                                        task_id=self.run.id,
                                        context={"text": text, "url": v["url"], "icp": self.icp, "lang": self.lang})
                self.add_candidates("youtube", data.get("candidates") or [], text, v["url"], v.get("title", ""))

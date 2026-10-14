"""入职向导（PRD 3.0）：AI 先自己查，再只问查不到的。

1. 客户提供资料：官网网址（主要）、产品目录 PDF、公司介绍、一句话描述
2. AI 通读整个官网：先读 sitemap，再沿站内链接读页面（默认上限 200 页）；重点产品/关于/认证/工厂/案例/新闻/联系；
   网站上的 PDF 一并读取；多语言站点选最完整的语言版本；同一产品不同颜色等重复页面自动跳过；
   每页先用便宜模型提取要点（每条带来源网址），最后用销售主管的强模型汇总；实时显示进度
3. 聊天式追问网站上查不到的内容（像新员工问老板）
4. 展示完整画像（每条带来源，完整度条 + 缺失项）→ 客户检查修改后确认保存
   结果作为画像的新版本（profile_versions），可与旧版本对比、可回退；不删除已有线索、样本和学到的经验

官网登录后只读（会员区等）见 site_login.py。
"""
import hashlib
import io
import re
import time
import urllib.parse

from .. import db, llm, paths, prompts, settings
from ..errors import UserError
from ..sources import crawler as crawler_mod
from ..sources import mockweb
from . import icp as icp_mod
from . import memory, schemas

FACT_CATEGORIES = ["company", "product_line", "product", "oem_odm", "advantage", "certification", "factory", "case",
                   "customer", "market", "trade_terms", "competitor", "contact", "other"]
LANG_CODES = {"en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ar", "tr", "ja", "ko", "zh", "cn", "vi", "th",
              "id", "ms", "cs", "sv", "da", "fi", "no", "hu", "ro", "uk", "he", "el", "zh-cn", "zh-tw", "en-us", "en-gb",
              "es-es", "pt-br", "fr-fr", "de-de"}
PRIORITY = [(re.compile(r"product|catalog|series|model|solution|item|产品", re.I), 0),
            (re.compile(r"about|company|factory|certif|quality|case|project|customer|client|oem|odm|contact|"
                        r"关于|工厂|认证|案例", re.I), 1),
            (re.compile(r"news|blog|event|exhibition|新闻", re.I), 2)]
SKIP_URL = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|zip|rar|mp4|mp3|css|js|ico|xml|docx?|xlsx?)(\?|$)|"
                      r"/(tag|tags|search|cart|checkout|login|signin|register|account|privacy|terms|cookie|"
                      r"wp-admin|wp-json|feed)\b|[?&](s|q|sort|filter|replytocom)=", re.I)
# 读官网只读这家公司自己的内容：搜索结果 / 列表筛选 / 深翻页之类的网址不读（例如 /s?wd=…、?tn=news、?page=9）
SEARCH_PARAMS = {"wd", "word", "q", "s", "query", "keyword", "keywords", "kw", "search", "searchword", "tn", "rtt",
                 "bsst", "sort", "order", "orderby", "filter", "filters", "tag", "tags", "cat", "from", "src", "rsv_spt",
                 "ie", "oq", "start", "offset"}
PAGE_PARAMS = {"page", "pn", "p", "pg", "paged", "pageno", "pagenum", "page_no"}
SEARCH_PATH = re.compile(r"(^|/)(s|search|so|find|query|tag|tags|list|lists|archive|archives|category|categories)(/|$)",
                         re.I)


EXTRACT_BATCH_CHARS = 12000        # 每批交给便宜模型提取的字数（小页面合并成一批）


def _search_like(u: str) -> bool:
    """搜索结果、筛选列表、深翻页（> 3 页）、参数很多的网址：不是公司自己的内容页。"""
    p = urllib.parse.urlparse(u)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    keys = {k.lower() for k, _ in q} - VARIANT_PARAMS
    if keys & SEARCH_PARAMS or len(keys) > 2:
        return True
    for k, v in q:
        if k.lower() in PAGE_PARAMS and (not v.isdigit() or int(v) > 3):
            return True
    m = re.search(r"/page/(\d+)", p.path)
    if m and int(m.group(1)) > 3:
        return True
    return bool(SEARCH_PATH.search(p.path))


VARIANT_PARAMS = {"color", "colour", "size", "variant", "sku", "lang", "currency", "ref", "utm_source", "utm_medium",
                  "utm_campaign", "gclid", "fbclid"}
LOGIN_HINT = re.compile(r"\blog-?in\b|login|sign-?in|anmelden|connexion|iniciar-sesion|accedi|member-?(area|login|zone)|"
                        r"dealer-?(login|portal|zone)|partner-?(login|portal)|登录|会员登录|经销商登录", re.I)

# 访谈问题：程序判断哪些没答 → 只问这些（PRD 3.0 第 3 步）
# 问题都是通用的（不预设行业，不写死 MOQ / OEM 之类）；客户的具体情况由 AI 从资料和回答里整理
QUESTIONS = ["countries", "customer_types", "best_customers", "unwanted_customers", "order_size",
             "purchase_scenarios", "competitors", "contact_titles"]
REQUIRED = ["product", "customer_types"]          # PRD 14：目标国家、开发信落款都不再必填（v1 不发开发信）
OPTIONAL = ["product_lines", "advantages", "competitors", "best_customers", "unwanted_customers", "contact_titles",
            "order_size", "purchase_scenarios", "industries", "countries"]

# 提示词在提示词库：quickstart.extract / quickstart.synthesize / quickstart.interview

FACTS_SCHEMA = schemas.obj({"facts": {"type": "array", "items": schemas.obj({"category": schemas.S, "text": schemas.S,
                                                                               "source_url": schemas.S})}})
V = schemas.obj({"text": schemas.S, "sources": schemas.SL})
SYNTH_SCHEMA = schemas.obj({
    "company": schemas.obj({
        "name": V, "summary": V, "product_lines": {"type": "array", "items": schemas.obj(
            {"name": schemas.S, "detail": schemas.S, "sources": schemas.SL})},
        "oem_odm": V, "advantages": V, "certifications": V, "factory": V, "cases": V, "markets": V,
        "moq": V, "price_range": V, "payment_terms": V, "lead_time": V, "competitors": V}),
    "icp": schemas.ICP,
    "reasons": {"type": "array", "items": schemas.obj({"field": schemas.S, "reason": schemas.S})},
})
ANSWER_SCHEMA = schemas.obj({"updates": schemas.obj({k: schemas.S for k in QUESTIONS + ["purchase_scenarios"]}),
                             "understood": schemas.B, "reply": schemas.S})


# ------------------------------------------------------------------ 开始

def start(inputs: dict, pdfs: list[tuple[str, bytes]] | None = None, redo=False) -> dict:
    from .. import tasks
    url = crawler_mod.normalize_url(inputs.get("website") or "")
    if not url and not pdfs and not (inputs.get("intro") or "").strip() and not (inputs.get("oneliner") or "").strip():
        raise UserError("icp_input_missing")
    if not settings.agent_enabled("manager"):
        raise UserError("agent_disabled", agent="@agent.manager")
    if not llm.configured("manager"):
        raise UserError("agent_not_configured", agent="@agent.manager")
    icp_id = inputs.get("icp_id")
    if redo and not icp_id:
        v = db.query_one("SELECT icp_id FROM profile_versions WHERE status='active' ORDER BY id DESC LIMIT 1")
        icp_id = v["icp_id"] if v else None
    now = db.now()
    ob_id = db.execute("INSERT INTO onboarding(icp_id, status, inputs, created_at, updated_at) VALUES(?,?,?,?,?)",
                       (icp_id, "reading", db.dumps({"website": url, "intro": inputs.get("intro", ""),
                                                     "oneliner": inputs.get("oneliner", ""), "redo": bool(redo),
                                                     "pdfs": [n for n, _ in pdfs or []]}), now, now))
    d = paths.UPLOAD_DIR / f"onboarding-{ob_id}"
    if pdfs:
        d.mkdir(parents=True, exist_ok=True)
        for i, (name, data) in enumerate(pdfs):
            (d / f"{i}.pdf").write_bytes(data)
    t = tasks.create_task({"onboarding_id": ob_id}, created_by="user", kind="onboarding")
    db.execute("UPDATE onboarding SET task_id=? WHERE id=?", (t["id"], ob_id))
    return {"onboarding_id": ob_id, "task_id": t["id"]}


def get(ob_id: int) -> dict:
    r = db.query_one("SELECT * FROM onboarding WHERE id=?", (ob_id,))
    if not r:
        raise UserError("onboarding_not_found", 404)
    d = dict(r)
    for k, dflt in (("inputs", {}), ("facts", []), ("profile", {}), ("chat", []), ("login", {})):
        d[k] = db.loads(r[k], dflt)
    d["pages"] = db.query("SELECT url, title, kind, status, facts FROM site_pages WHERE onboarding_id=? ORDER BY rowid",
                          (ob_id,))
    d["completeness"] = completeness(d["profile"])
    d["next_question"] = next_question(d["profile"], d["chat"])
    # 读资料任务现在的状态（界面据此决定显示进度、还是显示失败 + 重试 / 跳过 / 重新填写，绝不空白）
    from .. import tasks
    t = db.query_one("SELECT status, error, stats FROM tasks WHERE id=?", (r["task_id"],)) if r["task_id"] else None
    run = tasks.get_run(r["task_id"]) if r["task_id"] else None
    d["task_live"] = bool(run and not run.done)
    d["task_status"] = t["status"] if t else ""
    d["task_stats"] = (run.snapshot()["stats"] if run else db.loads(t["stats"], {})) if t else {}
    return d


def latest() -> dict | None:
    r = db.query_one("SELECT id FROM onboarding ORDER BY id DESC LIMIT 1")
    return get(r["id"]) if r else None


def cancel(ob_id: int) -> dict:
    """跳过“读资料” / 重新填写：停掉正在读的任务，这次快速开始记为“已取消”（不再停在“读取中”）。"""
    from .. import tasks
    ob = get(ob_id)
    run = tasks.get_run(ob["task_id"]) if ob["task_id"] else None
    if run and not run.done:
        run.cancel_flag.set()
        run.emit("system", "ev.cancel_requested", level="warn")
    if ob["status"] in ("reading", "queued", "failed", "robots_blocked"):
        _save(ob_id, status="cancelled")
    return {"ok": True}


def retry(ob_id: int) -> dict:
    """重试读资料：沿用已经提取的要点、跳过已经处理过的页面（断点续读）。"""
    from .. import tasks
    ob = get(ob_id)
    if ob["task_live"]:
        raise UserError("task_already_running")
    if ob["status"] not in ("reading", "queued", "failed", "robots_blocked", "cancelled"):
        raise UserError("invalid_value", field="status")
    if not llm.configured("manager"):
        raise UserError("agent_not_configured", agent="@agent.manager")
    params = {"onboarding_id": ob_id}
    if ob["task_id"]:
        params["resume_from"] = ob["task_id"]
    site = urllib.parse.urlparse(ob["inputs"].get("website") or "").netloc.lower()
    if site:                               # 客户自己点“重试”：解除这个网站的暂停，再试一次（只这一次，不循环）
        from .. import heal
        heal.domain_ok(site)
        heal.domain_ok(crawler_mod.site_key(site))
    _save(ob_id, status="reading", error="", login={k: v for k, v in (ob["login"] or {}).items() if k != "captcha"})
    t = tasks.create_task(params, created_by="user", kind="onboarding")
    db.execute("UPDATE onboarding SET task_id=? WHERE id=?", (t["id"], ob_id))
    return {"onboarding_id": ob_id, "task_id": t["id"]}


def task_ended(run, status: str, error: str):
    """读资料任务结束（失败 / 取消 / 意外退出）时，快速开始不能还停在“读取中”。"""
    ob_id = run.params.get("onboarding_id")
    if not ob_id or run.params.get("member_url"):
        return
    r = db.query_one("SELECT status, task_id FROM onboarding WHERE id=?", (ob_id,))
    if r and r["status"] in ("reading", "queued") and r["task_id"] == run.id and status != "done":
        _save(ob_id, status="failed", error=error or status)


def recover():
    """软件启动时：上次停在“读取中”但任务已经不在了（软件被关掉 / 崩溃）——
    第一次自动接着读（跳过已读页面）；已经接着读过一次还是没读完，就标记为失败，界面显示重试 / 跳过 / 重新填写。"""
    from .. import tasks
    for r in db.query("SELECT id, task_id, resumes FROM onboarding WHERE status IN ('reading','queued')"):
        run = tasks.get_run(r["task_id"]) if r["task_id"] else None
        if run and not run.done:
            continue
        t = db.query_one("SELECT status FROM tasks WHERE id=?", (r["task_id"],)) if r["task_id"] else None
        can = (t and t["status"] == "interrupted" and r["resumes"] < 1 and settings.agent_enabled("manager")
               and llm.configured("manager") and not tasks.running("onboarding"))
        if not can:
            _save(r["id"], status="failed", error="interrupted")
            continue
        try:
            nt = tasks.create_task({"onboarding_id": r["id"], "resume_from": r["task_id"]}, created_by="startup",
                                   kind="onboarding")
            db.execute("UPDATE onboarding SET task_id=?, resumes=resumes+1 WHERE id=?", (nt["id"], r["id"]))
        except Exception as e:  # noqa: BLE001
            _save(r["id"], status="failed", error=getattr(e, "key", "interrupted"))


def _save(ob_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    db.execute(f"UPDATE onboarding SET {sets} WHERE id=?",
               (*[db.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()], db.now(), ob_id))


# ------------------------------------------------------------------ 读官网

def _norm_url(u: str) -> str:
    """判重用：http / https、www / 不带 www、结尾斜杠、颜色尺寸等参数不同的同一页面算一页（不重复请求）。"""
    p = urllib.parse.urlparse(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() not in VARIANT_PARAMS]
    path = (p.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunparse(("", p.netloc.lower().removeprefix("www."), path, "", urllib.parse.urlencode(q), ""))


def _lang_of(u: str) -> str:
    seg = (urllib.parse.urlparse(u).path.strip("/").split("/") or [""])[0].lower()
    return seg if seg in LANG_CODES else ""


def _priority(u: str) -> int:
    for rx, p in PRIORITY:
        if rx.search(u):
            return p
    return 3


def _simhash(text: str) -> int:
    v = [0] * 64
    words = re.findall(r"\w{3,}", text.lower())
    for i in range(len(words) - 2):
        h = int(hashlib.md5(" ".join(words[i:i + 3]).encode()).hexdigest()[:16], 16)
        for b in range(64):
            v[b] += 1 if h >> b & 1 else -1
    return sum(1 << b for b in range(64) if v[b] > 0)


def _near_dup(h: int, seen: list[int]) -> bool:
    return any(bin(h ^ x).count("1") <= 3 for x in seen)


def _sitemap_urls(c: crawler_mod.Crawler, home: str, robots_text: str) -> list[str]:
    host = urllib.parse.urlparse(home)
    base = f"{host.scheme}://{host.netloc}"
    maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text or "") or [base + "/sitemap.xml",
                                                                        base + "/sitemap_index.xml"]
    maps = [urllib.parse.urljoin(base + "/", m) for m in maps]      # robots.txt 里也可能写相对地址（如 Sitemap: /sitemap.xml）
    urls, seen_maps = [], set()
    queue = list(maps)
    while queue and len(seen_maps) < 8 and len(urls) < 3000:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            p = c.fetch(sm)
        except crawler_mod.Skip:
            continue
        raw = p.get("raw") or ""
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", raw)
        for loc in locs:
            if loc.endswith(".xml") or "sitemap" in loc.lower() and loc.endswith((".xml", ".xml.gz")):
                queue.append(loc)
            else:
                urls.append(loc)
    return urls


def run_reading(run):
    """入职向导任务：读官网 + PDF + 客户填写的介绍 → 提取要点 → 汇总画像。"""
    ob_id = run.params["onboarding_id"]
    ob = get(ob_id)
    inputs = ob["inputs"]
    cfg = settings.get("onboarding")
    extractor = _cheap_agent()
    lang = settings.get("ui_lang")
    # 断点续跑：看门狗重启后沿用已经提取的要点
    facts: list[dict] = list(ob["facts"]) if run.params.get("resume_from") else []
    run.stats.update({"pages_total": 0, "pages_read": 0, "product_lines": 0, "certifications": 0, "cases": 0,
                      "pdfs": 0, "facts": 0})

    def add_facts(new):
        for f in new:
            if f["category"] not in FACT_CATEGORIES:
                f["category"] = "other"
            if all(f["text"].lower() != x["text"].lower() for x in facts):
                facts.append(f)
        _save(ob_id, facts=facts)      # 每提取一批就保存（可断点续做）
        cnt = {k: sum(1 for f in facts if f["category"] == k) for k in ("product_line", "certification", "case")}
        run.stats.update(product_lines=cnt["product_line"] + 0, certifications=cnt["certification"],
                         cases=cnt["case"], facts=len(facts))
        run.bump()
        run.emit("manager", "ev.ob_found", level="info", facts=len(facts), lines=cnt["product_line"],
                 certs=cnt["certification"], cases=cnt["case"])

    resumed = bool(run.params.get("resume_from"))
    # ---- 客户填写的文字
    for key in ("oneliner", "intro"):
        if resumed and any(f["source"] == f"input:{key}" for f in facts):
            continue
        txt = (inputs.get(key) or "").strip()
        if txt:
            add_facts(_extract([{"url": f"input:{key}", "title": key, "markdown": txt}], extractor, lang, run))
    # ---- 上传的 PDF
    d = paths.UPLOAD_DIR / f"onboarding-{ob_id}"
    for i, name in enumerate(inputs.get("pdfs") or []):
        if run.cancel_flag.is_set():
            return
        try:
            text = icp_mod.read_pdf((d / f"{i}.pdf").read_bytes())
        except UserError as e:
            run.emit("manager", "ev.ob_pdf_failed", level="warn", name=name, error=e.key)
            continue
        run.bump(pdfs=1)
        run.emit("manager", "ev.ob_reading_pdf", name=name)
        add_facts(_extract([{"url": f"pdf:{name}", "title": name, "markdown": text[:30000]}], extractor, lang, run))
    # ---- 官网（读不了时：有其他资料就继续，没有才报错）
    site_err = None
    if inputs.get("website"):
        try:
            _read_site(run, ob_id, inputs["website"], cfg, extractor, lang, add_facts)
        except UserError as e:
            if e.key == "ob_robots_blocked":
                raise
            site_err = e
            run.emit("manager", "ev." + e.key if e.key in list(BLOCK_ERR.values()) + ["ob_site_deferred", "ob_site_offsite"] else "ev.ob_site_failed",
                     level="warn", url=inputs["website"], to=e.params.get("to", ""))
    if run.cancel_flag.is_set():
        return
    if not facts and site_err and not any((inputs.get(k) or "").strip() for k in ("oneliner", "intro")):
        raise site_err
    if not facts:
        # 官网 / PDF / 介绍 / 一句话 都是选填（PRD 3.0）：只有一句话、模型又没提取出要点时，直接把客户自己写的话当作要点，
        # 缺的由补充访谈问
        for key in ("oneliner", "intro"):
            txt = (inputs.get(key) or "").strip()
            if txt:
                facts.append({"category": "company", "text": txt[:500], "source": f"input:{key}", "member": False})
        if facts:
            _save(ob_id, facts=facts)
    if not facts:
        raise UserError("ob_nothing_found")
    # ---- 汇总（销售主管的强模型）
    run.emit("manager", "ev.ob_synthesizing", facts=len(facts))
    prev = _previous_profile() if inputs.get("redo") else None
    profile = synthesize(facts, lang, base=prev)
    if prev:
        merge_previous(profile, prev)
    _save(ob_id, facts=facts, profile=profile, status="interview")
    run.emit("manager", "ev.ob_ready", level="success", facts=len(facts))


def _read_site(run, ob_id, url, cfg, extractor, lang, add_facts, cookies=None, member=False):
    respect = not settings.get("onboarding_flags", {}).get("ignore_robots")
    c = crawler_mod.Crawler(mock=mockweb.is_mock_url(url), should_stop=run.cancel_flag.is_set,
                            max_pages=int(cfg["max_pages"]) + 40, delay=float(cfg["delay_seconds"]),
                            respect_robots=respect, count_daily=False, keep_raw=True, cookies=cookies,
                            same_site_only=True)
    host = urllib.parse.urlparse(url).netloc.lower()
    run.emit("manager", "ev.ob_start_site", url=url)
    try:
        home = c.fetch(url)
    except crawler_mod.Skip as e:
        if e.key == "ev.robots_blocked":
            # 自己的网站被 robots.txt 全部挡住：明确告诉客户
            _save(ob_id, status="robots_blocked")
            raise UserError("ob_robots_blocked", url=url)
        if e.key in ("ev.crawl_challenge", "ev.domain_paused", "ev.domain_cooldown"):
            kind = _block_kind(ob_id, e)
            raise UserError(BLOCK_ERR[kind], url=url)
        if e.key == "ev.crawl_deferred":       # 需要隐身重试，但浏览器组件还在下载 / 正忙：不算被拦，之后点“重试”
            raise UserError("ob_site_deferred", url=url)
        if e.params.get("reason") == "offsite_redirect":    # 网址跳到了别的网站：不读（只读这家公司自己的网站）
            if not member:                              # 界面说明里要显示跳到了哪个网站
                _save(ob_id, login={**(get(ob_id).get("login") or {}), "offsite_to": e.params.get("to", "")})
            raise UserError("ob_site_offsite", url=url, to=e.params.get("to", ""))
        raise UserError("website_unreachable", url=url, reason=e.key)
    host = urllib.parse.urlparse(home["url"]).netloc.lower() or host
    robots_text = ""
    if not member:
        try:
            robots_text = c.fetch(f"{urllib.parse.urlparse(home['url']).scheme}://{host}/robots.txt").get("raw", "")
        except crawler_mod.Skip:
            pass
    candidates = [] if member else _sitemap_urls(c, home["url"], robots_text)
    run.emit("manager", "ev.ob_sitemap", n=len(candidates))
    login_hits = []
    queue: list[str] = []
    seen_norm: set[str] = set()
    hashes: list[int] = []

    def consider(u, text=""):
        u = u.split("#")[0]
        pu = urllib.parse.urlparse(u)
        if pu.scheme not in ("http", "https") or pu.netloc.lower().removeprefix("www.") != host.removeprefix("www."):
            return                      # 只读同一个主机（不跟到子域名 / 别的网站，例如 wappass.*）
        if u.lower().endswith(".pdf"):
            if u not in pdf_links:
                pdf_links.append(u)
            return
        if member and site_skip_member(u):
            return
        if LOGIN_HINT.search(pu.path + " " + text) and not member:
            if all(x["url"] != u for x in login_hits):
                login_hits.append({"url": u, "label": text[:60]})
            return
        if SKIP_URL.search(u) or _search_like(u):
            return
        n = _norm_url(u)
        if n not in seen_norm:
            seen_norm.add(n)
            queue.append(u)

    pdf_links: list[str] = []
    for u in candidates:
        consider(u)
    for href, text in home.get("links", []):
        consider(href, text)
    # 多语言：选 URL 最多的语言版本（无语言前缀的算默认版本）
    groups: dict[str, int] = {}
    for u in queue:
        groups[_lang_of(u)] = groups.get(_lang_of(u), 0) + 1
    best = max(groups.items(), key=lambda kv: (kv[1], kv[0] in ("en", ""), kv[0] == "en"))[0] if groups else ""
    if len([g for g in groups if g]) > 1 or (best and "" in groups):
        run.emit("manager", "ev.ob_language", lang=best or "default", n=len(groups))
    pages = [home]
    seen_norm.add(_norm_url(home["url"]))
    seen_norm.add(_norm_url(url))
    queue[:] = [u for u in queue if _norm_url(u) not in (_norm_url(home["url"]), _norm_url(url))]
    hashes.append(_simhash(home["markdown"]))
    _page_row(ob_id, home, "member" if member else "page", "read")
    if 'type="password"' in (home.get("raw") or "") and not member:
        login_hits.append({"url": home["url"], "label": home.get("title", "")})
    max_pages = int(cfg["max_pages"]) if not member else 100
    # 断点续跑：已提取过要点 / 判定为重复 / 跳过 / 失败的页面不再处理
    # （按规范化网址比较；跳转过的网址另记一行“alias”，所以 /blog → /blog/ 这类页面接着读时也不会再请求）
    done_urls = {_norm_url(r["url"]) for r in db.query("SELECT url FROM site_pages WHERE onboarding_id=? AND "
                                                       "(facts>=0 OR status NOT IN ('read','deferred'))", (ob_id,))} \
        if run.params.get("resume_from") else set()
    if _norm_url(home["url"]) in done_urls:
        pages = []
    # 上次已经读完并提取过的页面也算在页数上限里（接着读不会超过上限）
    already = db.query_one("SELECT COUNT(*) n FROM site_pages WHERE onboarding_id=? AND status='read' AND facts>=0 "
                           "AND kind<>'pdf'", (ob_id,))["n"] if run.params.get("resume_from") else 0
    # 每页先用便宜模型提取要点：边读边提取（小页面合并成一批，省调用次数）——软件中途被关掉时，
    # 已经提取过的页面重新打开后不再读（断点续读）；页面内容提取完就丢掉，不在内存里攒着
    batch: list[dict] = []
    bsize = [0]

    def flush():
        if batch:
            add_facts(_extract([{k: b[k] for k in ("url", "title", "markdown")} for b in batch], extractor, lang, run,
                               member))
            for b in batch:             # 提取完了，请求时的网址（跳转前）也记为已处理
                if b.get("req") and _norm_url(b["req"]) != _norm_url(b["url"]):
                    _page_row(ob_id, {"url": b["req"], "title": ""}, "page", "alias")
            batch.clear()
            bsize[0] = 0

    def queue_extract(pg, req=None):
        md = pg["markdown"][:6000]
        if bsize[0] + len(md) > EXTRACT_BATCH_CHARS and batch:
            flush()
        batch.append({"url": pg["url"], "title": pg.get("title", ""), "markdown": md, "req": req})
        bsize[0] += len(md)
    for pg in pages:
        queue_extract(pg)
    i = 0
    run.stats["pages_total"] = min(max_pages, len(queue) + 1 + already)
    run.stats["pages_read"] = len(pages) + already
    while queue and len(pages) + already < max_pages and not run.cancel_flag.is_set():
        queue.sort(key=lambda u: (0 if _lang_of(u) == best else 1, _priority(u)))
        u = queue.pop(0)
        if _norm_url(u) in done_urls:
            continue
        if not run.step(f"url:{_norm_url(u)}", "manager"):
            continue
        if _lang_of(u) not in (best, "") and best:
            _page_row(ob_id, {"url": u, "title": ""}, "page", "skipped")
            continue
        try:
            p = c.fetch(u)
        except crawler_mod.Skip as e:
            if e.key in ("ev.crawl_task_limit", "ev.cancelled"):
                break
            if e.key == "ev.crawl_deferred":
                # 浏览器暂时用不了（还在下载 / 正忙）：这一页记为“稍后再读”，“重试”时会重新读；不算网站拦截
                _page_row(ob_id, {"url": u, "title": ""}, "page", "deferred")
                continue
            if e.key in ("ev.crawl_challenge", "ev.domain_paused", "ev.domain_cooldown"):
                # 网站有验证（隐身模式开启时爬虫已经用隐身方式重试过一次）：立即停止读取这个网站，用已经读到的内容继续
                _page_row(ob_id, {"url": u, "title": ""}, "page", "failed")
                kind = _block_kind(ob_id, e)
                run.emit("manager", "ev." + BLOCK_ERR[kind], level="warn", url=u)
                break
            _page_row(ob_id, {"url": u, "title": ""}, "page", "failed")
            continue
        h = _simhash(p["markdown"])
        if not p["markdown"].strip() or _near_dup(h, hashes):
            _page_row(ob_id, p, "page", "duplicate")
            if _norm_url(p["url"]) != _norm_url(u):
                _page_row(ob_id, {"url": u, "title": ""}, "page", "alias")
            continue
        hashes.append(h)
        pages.append(p)
        _page_row(ob_id, p, "member" if member else "page", "read")
        if 'type="password"' in (p.get("raw") or "") and not member:
            login_hits.append({"url": p["url"], "label": p.get("title", "")})
        for href, text in p.get("links", []):
            consider(href, text)
        p.pop("raw", None)              # 不留整页 HTML（内存）
        p.pop("links", None)
        queue_extract(p, req=u)
        p["markdown"] = ""              # 已经交给提取批次
        run.stats["pages_total"] = min(max_pages, len(pages) + already + len(queue))
        run.stats["pages_read"] = len(pages) + already
        run.bump()
        i += 1
        run.emit("manager", "ev.ob_reading", url=p["url"], n=run.stats["pages_read"], total=run.stats["pages_total"])
    if run.cancel_flag.is_set():
        return
    flush()
    # 网站上的 PDF 产品目录 / 规格书
    for u in pdf_links[: int(settings.get("onboarding")["max_pdfs"])]:
        if run.cancel_flag.is_set():
            return
        try:
            data = c.fetch_bytes(u)
            text = icp_mod.read_pdf(data)
        except (crawler_mod.Skip, UserError):
            _page_row(ob_id, {"url": u, "title": ""}, "pdf", "failed")
            continue
        run.bump(pdfs=1)
        _page_row(ob_id, {"url": u, "title": u.rsplit("/", 1)[-1]}, "pdf", "read")
        run.emit("manager", "ev.ob_reading_pdf", name=u.rsplit("/", 1)[-1])
        add_facts(_extract([{"url": u, "title": u.rsplit("/", 1)[-1], "markdown": text[:30000]}], extractor, lang, run,
                           member))
    if login_hits and not member:
        _save(ob_id, login={"detected": login_hits[:5], "host": host})
        run.emit("manager", "ev.ob_login_found", url=login_hits[0]["url"])


# 网站验证 / 拦截的类型 → 给客户看的说明（PRD 4.1 / 16）
BLOCK_ERR = {"captcha": "ob_site_captcha",                       # 图片 / 滑块验证码：永远停（软件不识别验证码）
             "browser_check": "ob_site_verify",                  # Cloudflare 类检查、隐身模式关闭：停，提示可开启隐身模式后重试
             "browser_check_stealth_failed": "ob_site_verify_failed",   # 隐身方式重试一次仍被拦：停
             "rate_limited": "ob_site_blocked", "blocked": "ob_site_blocked"}


def _block_kind(ob_id: int, e) -> str:
    """记下这个网站被拦的类型（界面据此显示说明）；网站因为刚才被拦还在暂停期时，沿用上次的类型。"""
    login = get(ob_id).get("login") or {}
    kind = e.params.get("kind") if e.key in ("ev.crawl_challenge", "ev.domain_paused") else None
    if not kind:
        prev = login.get("captcha")
        kind = prev if isinstance(prev, str) and prev in BLOCK_ERR else "captcha"
    kind = kind if kind in BLOCK_ERR else "captcha"
    _save(ob_id, login={**login, "captcha": kind})
    return kind


def site_skip_member(u: str) -> bool:
    """登录后只读：跳过任何可能改变内容或账户的页面。"""
    return bool(re.search(r"logout|log-out|signout|sign-out|admin|wp-admin|settings|setting|profile/edit|password|"
                          r"delete|remove|cancel|unsubscribe|cart|checkout|order|pay|billing|subscription|"
                          r"edit|update|submit|action=|do=", u, re.I))


def _page_row(ob_id, p, kind, status):
    # facts = -1：已读、还没提取要点（断点续跑时会重新读取并提取）
    db.execute("INSERT OR REPLACE INTO site_pages(onboarding_id, url, title, kind, status, facts) VALUES(?,?,?,?,?,?)",
               (ob_id, p["url"], (p.get("title") or "")[:200], kind, status, -1 if status == "read" else 0))


def _cheap_agent() -> str:
    """每页提取用便宜模型：在已启用的智能体里选单价最低的那个配置。"""
    best, best_price = None, None
    for a in ("monitor", "researcher", "analyst", "manager"):
        if not settings.agent_enabled(a) or not llm.configured(a):     # 快速开始时通常只有主管配置了 API
            continue
        cfg = llm.get_config(a)
        p = llm.price_for(cfg, cfg.model)
        price = (p[0] + p[1] / 4) if p else 1e9
        if best is None or price < best_price:
            best, best_price = a, price
    return best or "manager"


def _extract(pages, agent, lang, run, member=False) -> list[dict]:
    urls = [p["url"] for p in pages]
    user = "\n\n".join(f"### PAGE {p['url']}\nTitle: {p.get('title', '')}\n{p['markdown']}" for p in pages)
    system = prompts.render("quickstart.extract", categories=", ".join(FACT_CATEGORIES),
                            lang="Simplified Chinese" if lang == "zh" else "English")
    try:
        data, _ = llm.call_json(agent, "ob_extract", system, user, FACTS_SCHEMA, effort="low", task_id=run.id,
                                context={"pages": pages})
    except UserError as e:
        if e.key in ("budget_exceeded", "agent_disabled", "key_missing", "key_invalid"):
            raise
        run.emit("manager", "ev.agent_error", level="warn", error=e.key, **e.params)
        return []
    out = []
    for f in data.get("facts") or []:
        src = str(f.get("source_url") or "")
        if src not in urls:
            src = urls[0] if len(urls) == 1 else ""
        if not src or not str(f.get("text") or "").strip():
            continue
        out.append({"category": str(f.get("category") or "other"), "text": str(f["text"]).strip()[:300],
                    "source": src, "member": member})
    for u in urls:
        n = sum(1 for f in out if f["source"] == u)
        db.execute("UPDATE site_pages SET facts=? WHERE url=? AND onboarding_id=?", (n, u, run.params.get("onboarding_id")))
    return out


# ------------------------------------------------------------------ 汇总 / 完整度

def _previous_profile():
    v = db.query_one("SELECT company, icp FROM profile_versions WHERE status='active' ORDER BY id DESC LIMIT 1")
    current = settings.get("company_profile", {}) or {}
    if not v and not current:
        return None
    company = current or db.loads(v["company"], {})
    # Manual corrections are the redo baseline; historical versions remain unchanged.
    company = {k: val for k, val in company.items() if k not in ("updated_at", "onboarding_id")}
    return {"company": company, "icp": db.loads(v["icp"], {}) if v else {}}


def synthesize(facts: list[dict], lang: str, base: dict | None = None) -> dict:
    lines = "\n".join(f"- [{f['category']}] {f['text']} (source: {f['source']})" for f in facts[:400])
    system = prompts.render("quickstart.synthesize", role=memory.role("manager"), rules=icp_mod.rules(lang),
                            lang="Simplified Chinese" if lang == "zh" else "English")
    user = "Facts:\n" + lines + ("\n\nPrevious profile (keep what is still valid):\n" + str(base)[:6000] if base else "")
    data, _ = llm.call_json("manager", "ob_synthesize", system, user, SYNTH_SCHEMA, context={"facts": facts,
                                                                                             "lang": lang})
    company = data.get("company") or {}
    icp_d = icp_mod.normalize(data.get("icp") or {})
    if not icp_d["product"]:
        icp_d["product"] = (company.get("summary") or {}).get("text", "")
    if not icp_d["name"]:
        icp_d["name"] = ((company.get("name") or {}).get("text") or "") + (" · ICP" if lang != "zh" else " · 理想客户")
    sig = (base or {}).get("company", {}).get("signature", "") if base else ""
    company.setdefault("signature", sig or (settings.get("company_profile", {}) or {}).get("signature", ""))
    return {"company": company, "icp": icp_d, "reasons": data.get("reasons") or [], "answers": {}}


def merge_previous(new: dict, old: dict):
    """重新开始向导：网站上读不到、但之前访谈里客户告诉过的内容，保留旧值（客户在差异里确认）。"""
    nc, oc = new.setdefault("company", {}), old.get("company") or {}
    for k, v in oc.items():
        cur = nc.get(k)
        empty = (not cur) or (isinstance(cur, dict) and not str(cur.get("text") or "").strip())
        if empty and v:
            nc[k] = v
    ni, oi = new.setdefault("icp", {}), old.get("icp") or {}
    for k in ("countries", "customer_types", "reference_customers", "exclude_companies", "exclude_words",
              "contact_titles", "order_size", "purchase_scenarios", "industries", "company_size"):
        if not ni.get(k) and oi.get(k):
            ni[k] = oi[k]


def _filled(profile: dict, key: str) -> bool:
    c, i, a = profile.get("company") or {}, profile.get("icp") or {}, profile.get("answers") or {}
    if key in a and str(a[key]).strip() and a[key] != "__skip__":
        return True
    m = {
        "product": lambda: bool(i.get("product") or (c.get("summary") or {}).get("text")),
        "product_lines": lambda: bool(c.get("product_lines")),
        "countries": lambda: bool(i.get("countries")),
        "customer_types": lambda: bool(i.get("customer_types")),
        "industries": lambda: bool(i.get("industries")),
        "signature": lambda: bool(str(c.get("signature") or "").strip()),
        "best_customers": lambda: len(i.get("reference_customers") or []) >= 1,
        "unwanted_customers": lambda: bool(i.get("exclude_companies") or i.get("exclude_words") or i.get("exclude_keywords")),
        "contact_titles": lambda: bool(i.get("contact_titles")),
        "order_size": lambda: bool(i.get("order_size")),
        "purchase_scenarios": lambda: bool(i.get("purchase_scenarios")),
        "certifications": lambda: bool((c.get("certifications") or {}).get("text")),
        "advantages": lambda: bool((c.get("advantages") or {}).get("text")),
        "oem_odm": lambda: bool((c.get("oem_odm") or {}).get("text")),
    }
    if key in m:
        return m[key]()
    return bool((c.get(key) or {}).get("text") if isinstance(c.get(key), dict) else c.get(key))


def completeness(profile: dict) -> dict:
    if not profile:
        return {"pct": 0, "missing_required": REQUIRED, "missing_optional": OPTIONAL, "ok": False}
    req = [k for k in REQUIRED if not _filled(profile, k)]
    opt = [k for k in OPTIONAL if not _filled(profile, k)]
    total = len(REQUIRED) + len(OPTIONAL)
    pct = round(100 * (total - len(req) - len(opt)) / total)
    return {"pct": pct, "missing_required": req, "missing_optional": opt, "ok": not req}


def next_question(profile: dict, chat: list) -> str | None:
    if not profile:
        return None
    asked_skip = {k for k, v in (profile.get("answers") or {}).items() if v == "__skip__"}
    for k in QUESTIONS:
        if k not in asked_skip and not _filled(profile, k):
            return k
    return None


def answer(ob_id: int, message: str, skip=False) -> dict:
    ob = get(ob_id)
    prof = ob["profile"]
    q = ob["next_question"]
    if not q:
        return {"done": True, **get(ob_id)}
    chat = ob["chat"]
    lang = settings.get("ui_lang")
    if skip or not (message or "").strip():
        prof.setdefault("answers", {})[q] = "__skip__"
        chat.append({"role": "user", "q": q, "content": "__skip__"})
    else:
        chat.append({"role": "user", "q": q, "content": message.strip()})
        system = prompts.render("quickstart.interview", role=memory.role("manager"),
                                lang="Simplified Chinese" if lang == "zh" else "English")
        user = f"Question topic: {q}\nAnswer: {message.strip()}"
        data, _ = llm.call_json("manager", "ob_answer", system, user, ANSWER_SCHEMA, effort="low",
                                context={"q": q, "answer": message.strip()})
        _apply_answer(prof, data.get("updates") or {}, q, message.strip())
        if data.get("reply"):
            chat.append({"role": "assistant", "content": data["reply"]})
    _save(ob_id, profile=prof, chat=chat)
    return get(ob_id)


def _split(v):
    return [x.strip() for x in re.split(r"[,，;；\n、]+", str(v or "")) if x.strip()]


def _apply_answer(prof, updates: dict, q: str, raw: str):
    from .. import countries
    i, c = prof.setdefault("icp", {}), prof.setdefault("company", {})
    a = prof.setdefault("answers", {})
    if not any(str(v or "").strip() for v in updates.values()):
        updates = {q: raw}
    for k, v in updates.items():
        v = str(v or "").strip()
        if not v:
            continue
        if k == "countries":
            i["countries"] = list(dict.fromkeys((i.get("countries") or []) +
                                                [x for x in (countries.normalize_country(s) for s in _split(v)) if x]))
        elif k == "customer_types":
            i["customer_types"] = list(dict.fromkeys((i.get("customer_types") or []) + _split(v)))
        elif k == "competitors":
            c["competitors"] = {"text": v, "sources": ["interview"]}
            i["exclude_companies"] = list(dict.fromkeys((i.get("exclude_companies") or []) + _split(v)))
        elif k == "best_customers":
            # 参考客户 = 具体的公司名 / 网址；和“客户类型”重复的词（例如把类型又答了一遍）不当作参考客户
            types = {t.lower() for t in i.get("customer_types") or []}
            names = [x for x in _split(v) if x.lower() not in types]
            i["reference_customers"] = list(dict.fromkeys((i.get("reference_customers") or []) + names))
        elif k == "unwanted_customers":
            i["exclude_words"] = list(dict.fromkeys((i.get("exclude_words") or i.get("exclude_keywords") or []) + _split(v)))
        elif k == "contact_titles":
            i["contact_titles"] = list(dict.fromkeys((i.get("contact_titles") or []) + _split(v)))
        elif k == "order_size":
            i["order_size"] = v
        elif k == "purchase_scenarios":
            i["purchase_scenarios"] = _split(v)
        elif k == "signature":
            c["signature"] = v
        elif k in ("moq", "price_range", "payment_terms", "lead_time"):
            c[k] = {"text": v, "sources": ["interview"]}
        a[k] = v


def update_profile(ob_id: int, profile: dict) -> dict:
    ob = get(ob_id)
    p = ob["profile"]
    if "company" in profile:
        p["company"] = profile["company"]
    if "icp" in profile:
        p["icp"] = icp_mod.normalize(profile["icp"])
    _save(ob_id, profile=p)
    return get(ob_id)


# ------------------------------------------------------------------ 确认 → 新版本

def confirm(ob_id: int) -> dict:
    ob = get(ob_id)
    prof = ob["profile"]
    comp = completeness(prof)
    if not comp["ok"]:
        raise UserError("profile_incomplete", fields=",".join(f"@ob.q.{m}" for m in comp["missing_required"]))
    company = prof["company"]
    icp_data = icp_mod.normalize(prof["icp"])
    # 模板“产品介绍”用画像汇总（带产品线）
    lines = "; ".join(pl.get("name", "") for pl in company.get("product_lines") or [] if pl.get("name"))
    if lines and lines not in icp_data["product"]:
        icp_data["product"] = (icp_data["product"] + (" — " if icp_data["product"] else "") + lines)[:2000]
    reasons = prof.get("reasons") or []
    if ob["icp_id"] and db.query_one("SELECT id FROM icp_templates WHERE id=?", (ob["icp_id"],)):
        tpl = icp_mod.save(icp_data, icp_id=ob["icp_id"], reasons=reasons)
    else:
        tpl = icp_mod.save(icp_data, reasons=reasons, origin="onboarding")
    settings.put("company_profile", {**company, "updated_at": time.time(), "onboarding_id": ob_id})
    with db.transaction():
        v = (db.query_one("SELECT COALESCE(MAX(version),0) v FROM profile_versions")["v"]) + 1
        db.execute("UPDATE profile_versions SET status='superseded' WHERE status='active'")
        db.execute("INSERT INTO profile_versions(version, onboarding_id, icp_id, company, icp, status, created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (v, ob_id, tpl["id"], db.dumps(company), db.dumps(icp_data), "active",
                                             db.now()))
    _save(ob_id, status="done", icp_id=tpl["id"])
    ob_cfg = settings.get("onboarding")
    ob_cfg["done"] = True
    settings.put("onboarding", ob_cfg)
    return {"icp_id": tpl["id"], "version": v}


def versions():
    rows = db.query("SELECT id, version, onboarding_id, icp_id, status, created_at, company, icp FROM profile_versions "
                    "ORDER BY version DESC")
    for r in rows:
        r["company"] = db.loads(r["company"], {})
        r["icp"] = db.loads(r["icp"], {})
    return rows


def diff(ob_id: int) -> list[dict]:
    """重新开始向导：新画像与当前生效版本的差异（供客户确认）。"""
    ob = get(ob_id)
    old = _previous_profile()
    if not old:
        return []
    out = []
    new_c, old_c = ob["profile"].get("company") or {}, old["company"]
    for k in sorted(set(new_c) | set(old_c)):
        if k in ("updated_at", "onboarding_id"):
            continue
        a, b = _plain(old_c.get(k)), _plain(new_c.get(k))
        if a != b:
            out.append({"section": "company", "field": k, "old": a, "new": b})
    new_i, old_i = ob["profile"].get("icp") or {}, old["icp"]
    for k in sorted(set(new_i) | set(old_i)):
        a, b = _plain(old_i.get(k)), _plain(new_i.get(k))
        if a != b and k not in ("relax_policy",):
            out.append({"section": "icp", "field": k, "old": a, "new": b})
    return out


def _plain(v) -> str:
    if isinstance(v, dict):
        return str(v.get("text") if "text" in v else v)
    if isinstance(v, list):
        return ", ".join(_plain(x) if not isinstance(x, dict) else str(x.get("name") or x.get("text") or x) for x in v)
    return "" if v is None else str(v)


def rollback(version: int) -> dict:
    v = db.query_one("SELECT * FROM profile_versions WHERE version=?", (version,))
    if not v:
        raise UserError("version_not_found", 404)
    company, icp_data = db.loads(v["company"], {}), db.loads(v["icp"], {})
    if v["icp_id"] and db.query_one("SELECT id FROM icp_templates WHERE id=?", (v["icp_id"],)):
        icp_mod.save(icp_data, icp_id=v["icp_id"])
    settings.put("company_profile", {**company, "updated_at": time.time()})
    with db.transaction():
        db.execute("UPDATE profile_versions SET status='rolled_back' WHERE status='active'")
        db.execute("UPDATE profile_versions SET status='active' WHERE id=?", (v["id"],))
    return {"version": version}


def first_launch() -> bool:
    """第一次打开软件默认进入向导：没完成过向导、也没有任何模板时。"""
    return not settings.get("onboarding").get("done") and not db.query_one("SELECT id FROM icp_templates LIMIT 1")


def member_read(run, ob_id: int, url: str, cookies: list[dict]):
    """登录后只读：用客户登录得到的 cookie，只发 GET 请求读取会员区页面，跳过任何会改变内容的页面。"""
    jar = {c["name"]: c["value"] for c in cookies if c.get("name")}
    ob = get(ob_id)
    facts = ob["facts"]
    lang = settings.get("ui_lang")

    def add(new):
        for f in new:
            if all(f["text"].lower() != x["text"].lower() for x in facts):
                facts.append(f)
        run.bump()
    _read_site(run, ob_id, url, settings.get("onboarding"), _cheap_agent(), lang, add, cookies=jar, member=True)
    prof = synthesize(facts, lang, base=ob["profile"] or None)
    prof["answers"] = (ob["profile"] or {}).get("answers", {})
    _save(ob_id, facts=facts, profile=prof)

"""理想客户模板（ICP）：AI 生成（官网 / 一句话 / PDF）、聊天式修改、线索词、保存/复制/编辑/删除。

线索词放在模板最上面（PRD §3）：求购词（要买的说法）/ 产品词（产品名和别名）/ 排除词，中 / 英 / 马来文。
程序规则：命中任一求购词或产品词、且没有命中排除词 → 交给判断模型 / AI 判断（match_words）；
搜索类渠道用 产品词 × 求购词（+ 国家）组合搜索词（search_queries）。

由“销售主管”的模型配置负责（他是制定找客户策略的人）。
"""
import io
import json
import re

from .. import countries, db, edition, llm, prompts
from ..errors import UserError
from ..sources import crawler as crawler_mod
from . import schemas

AGENT = "manager"
# 客户类型的常用代码（只用于界面显示翻译；不再写进提示词 —— 客户类型由 AI 从客户自己的资料里提炼）
CUSTOMER_TYPES = ["distributor", "wholesaler", "importer", "retailer", "brand_owner", "contractor",
                  "installer", "online_seller", "end_user", "agent"]
MAX_INPUT_CHARS = 60000


def _out_lang(lang):
    return "Simplified Chinese" if lang == "zh" else "English"


def rules(lang) -> str:
    """模板规则（提示词库 icp.rules）：快速开始汇总和模板生成 / 修改共用。"""
    return prompts.render("icp.rules", lang=_out_lang(lang), **scope_vars())


def scope_vars() -> dict:
    """提示词里的范围说明（v1 只做马来西亚、中 / 英 / 马来语，PRD §1）。"""
    word_langs = ", ".join(f"{countries.lang_name(x)} ({x})" for x in countries.LEAD_LANGS)   # 线索词的语言：中 / 英 / 马来
    if edition.current()["id"] == "my_igaming":
        return {"scope": edition.market_instructions({"geo_mode": "served_market", "target_markets": ["MY"], "b2b_segment": ""}),
                "langs": "zh, ms", "word_langs": word_langs}
    if countries.V1_SCOPE:
        return {"scope": prompts.get("scope.v1"), "langs": ", ".join(x for x in countries.LEAD_LANGS if x != "en"),
                "word_langs": word_langs}
    return {"scope": "", "langs": "the main business language of each target country", "word_langs": word_langs}


def _system(lang):
    from . import memory
    return prompts.render("icp.design", role=memory.role("manager"), rules=rules(lang)) + "\nPreserve geo_mode, target_markets and b2b_segment from the existing template; countries are registration criteria only when geo_mode=registered_country."


# ------------------------------------------------------------------ 输入素材

def read_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        raise UserError("pdf_unreadable")
    parts = []
    for i, page in enumerate(reader.pages):
        if i >= 60:
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    text = "\n".join(parts).strip()
    if len(text) < 30:
        raise UserError("pdf_no_text")
    return text


def read_website(url: str, mock=False) -> tuple[str, list[str]]:
    c = crawler_mod.Crawler(mock=mock, max_pages=4)
    try:
        home = c.fetch(url)
    except crawler_mod.Skip as e:
        raise UserError("website_unreachable", url=url, reason=e.key)
    pages = [home]
    for sub in crawler_mod.key_pages(home, limit=2):
        try:
            pages.append(c.fetch(sub))
        except crawler_mod.Skip:
            continue
    text = "\n\n".join(f"### Page: {p['url']}\n{p['markdown']}" for p in pages)
    return text, [p["url"] for p in pages]


def _clip(text: str) -> str:
    if len(text) > MAX_INPUT_CHARS:
        return text[:MAX_INPUT_CHARS] + "\n\n[... material truncated because it is very long ...]"
    return text


# ------------------------------------------------------------------ 校验

WORDS_MAX = 80


def _list(v):
    if isinstance(v, str):
        v = [x for x in v.replace("，", ",").split(",")]
    out = []
    for x in v or []:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def normalize(icp: dict, strict=False) -> dict:
    icp = dict(icp or {})
    mode = icp.get("geo_mode") or edition.current()["geo_mode"]
    if mode not in ("registered_country", "served_market"):
        raise UserError("invalid_value", field="geo_mode")
    segment = icp.get("b2b_segment") or ""
    if segment not in ("", "channel_team", "igaming_company"):
        raise UserError("invalid_value", field="b2b_segment")
    markets = [c for c in dict.fromkeys(countries.normalize_country(x) for x in _list(icp.get("target_markets"))) if c]
    if edition.current()["id"] == "my_igaming":
        mode, markets, segment = "served_market", ["MY"], "channel_team"
    out = {
        "geo_mode": mode, "target_markets": markets, "b2b_segment": segment,
        "name": str(icp.get("name") or "").strip()[:120],
        "product": str(icp.get("product") or "").strip() or ("查找面向马来西亚市场的公开 iGaming 业务代理线索" if edition.current()["id"] == "my_igaming" else ""),
        # 目标国家：客户自己说的（补充访谈 / 对话），可以为空 = 不按国家筛（PRD §1 / §3）
        "countries": [c for c in dict.fromkeys(countries.normalize_country(x) for x in _list(icp.get("countries"))) if c],
        "customer_types": _list(icp.get("customer_types")),
        "industries": _list(icp.get("industries")),
        "company_size": str(icp.get("company_size") or "").strip(),
        # 线索词（PRD §3）：旧模板的 求购说法 → 求购词，包含关键词（含多语言）→ 产品词，排除关键词 → 排除词
        "buy_words": _list(icp["buy_words"])[:WORDS_MAX] if "buy_words" in icp else
        _list([p for x in icp.get("intent_phrases") or [] for p in _list((x or {}).get("phrases"))])[:WORDS_MAX],
        "product_words": _list(icp["product_words"])[:WORDS_MAX] if "product_words" in icp else
        _list(_list(icp.get("include_keywords")) + [k for x in icp.get("keywords_by_lang") or []
                                                     for k in _list((x or {}).get("keywords"))])[:WORDS_MAX],
        "exclude_words": _list(icp["exclude_words"] if "exclude_words" in icp else icp.get("exclude_keywords"))[:WORDS_MAX],
        "exclude_companies": _list(icp.get("exclude_companies")),
        "reference_customers": _list(icp.get("reference_customers")),
        "contact_titles": _list(icp.get("contact_titles")),
        "relax_policy": {
            "allow_neighbor_countries": bool((icp.get("relax_policy") or {}).get("allow_neighbor_countries")),
            "allow_change_customer_type": bool((icp.get("relax_policy") or {}).get("allow_change_customer_type")),
            "allow_more_industries": bool((icp.get("relax_policy") or {}).get("allow_more_industries")),
            "notes": str((icp.get("relax_policy") or {}).get("notes") or ""),
        },
        "qualified_grades": [g for g in _list(icp.get("qualified_grades")) if g in ("A", "B", "C")] or ["A", "B"],
        # 第 2 步（快速开始 PRD 3.0）新增，选填
        "order_size": str(icp.get("order_size") or "").strip(),
        "purchase_scenarios": _list(icp.get("purchase_scenarios")),
        # 由 AI 根据客户资料生成（PRD 2.4.2：业务内容不写死在程序里）
        "grading_criteria": [],
    }
    for c in icp.get("grading_criteria") or []:
        if isinstance(c, dict) and str(c.get("name") or "").strip():
            try:
                w = max(0, min(100, int(c.get("weight") or 0)))
            except (TypeError, ValueError):
                w = 0
            out["grading_criteria"].append({"name": str(c["name"]).strip()[:60], "weight": w,
                                            "description": str(c.get("description") or "").strip()[:300]})
    out["grading_criteria"] = out["grading_criteria"][:8]
    if "A" not in out["qualified_grades"]:
        out["qualified_grades"].insert(0, "A")
    if strict:
        missing = [f for f in ("name", "product", "customer_types") if not out[f]]      # 目标国家不必填
        if missing:
            raise UserError("icp_missing_fields", fields=",".join(f"@icp.{m}" for m in missing))
    return out


# ------------------------------------------------------------------ AI 生成 / 修改

def generate(mode: str, text: str = "", url: str = "", pdf: bytes | None = None, lang="zh") -> dict:
    cfg = llm.get_config(AGENT)
    mock = cfg.provider == "mock"
    pages = []
    web = False
    if mode == "website":
        url = crawler_mod.normalize_url(url)
        if not url:
            raise UserError("icp_input_missing")
        if mock:
            material = f"Company website: {url}"
        else:
            try:
                material, pages = read_website(url)
                material = f"Company website {url}. Content of its pages:\n\n{material}"
            except UserError:
                if cfg.provider != "claude":
                    raise
                # 本地抓不到时，Claude 可以用自带的联网读取
                material, web = f"Our company website is {url}. Read it (web_fetch) to understand what we sell.", True
    elif mode == "pdf":
        if not pdf:
            raise UserError("icp_input_missing")
        material = "Our product catalogue (text extracted from PDF):\n\n" + read_pdf(pdf)
    else:
        if not (text or "").strip():
            raise UserError("icp_input_missing")
        material = "Description of our product/service: " + text.strip()
    user = prompts.render("icp.generate_task", material=_clip(material),
                          extra=text.strip() if mode != "text" and text else "")
    data, _ = llm.call_json(AGENT, "icp_generate", _system(lang), user, schemas.ICP_GENERATE, web_search=web,
                            context={"mode": mode, "input": text or url, "lang": lang})
    icp = normalize(data.get("icp") or {})
    return {"icp": icp, "reasons": data.get("reasons") or [], "summary": data.get("summary") or "",
            "pages": pages}


def chat(icp: dict, message: str, history: list | None = None, lang="zh") -> dict:
    if not (message or "").strip():
        raise UserError("icp_input_missing")
    hist = "\n".join(f"{h.get('role')}: {h.get('content')}" for h in (history or [])[-10:])
    user = prompts.render("icp.chat_task", icp_json=json.dumps(normalize(icp), ensure_ascii=False),
                          history=hist or "(none)", message=message.strip())
    data, _ = llm.call_json(AGENT, "icp_chat", _system(lang), user, schemas.ICP_CHAT,
                            context={"icp": icp, "message": message, "lang": lang})
    changed = dict(data.get("icp") or icp)
    for field in ("geo_mode", "target_markets", "b2b_segment"):
        if field in icp:
            changed[field] = icp[field]
    return {"icp": normalize(changed), "reply": data.get("reply") or "",
            "changes": data.get("changes") or []}


def translate_keywords(keywords: list[str], country_codes: list[str] | None = None, lang="zh",
                       langs: list[str] | None = None) -> list[dict]:
    keywords = _list(keywords)
    if not keywords:
        raise UserError("keywords_missing")
    langs = langs or [lg for lg in countries.languages_for(country_codes) if lg != "en"]
    if not langs:
        return []
    names = ", ".join(f"{lg} ({countries.lang_name(lg)})" for lg in langs)
    user = prompts.render("icp.keywords_translate_task", keywords=json.dumps(keywords, ensure_ascii=False),
                          languages=names)
    data, _ = llm.call_json(AGENT, "keywords_translate", prompts.get("icp.keywords_translate"), user, schemas.KEYWORDS,
                            context={"keywords": keywords, "languages": langs, "lang": lang})
    out = []
    for item in data.get("by_lang") or []:
        lg = str(item.get("lang") or "").lower()
        if lg in langs:
            out.append({"lang": lg, "keywords": _list(item.get("keywords"))})
    return out


# ------------------------------------------------------------------ 存储

def _row(r, full=True):
    if not r:
        return None
    d = {"id": r["id"], "name": r["name"], "data": db.loads(r["data"], {}), "origin": r["origin"],
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if full:
        d["reasons"] = db.loads(r["reasons"], [])
        d["chat"] = db.loads(r["chat"], [])
    return d


def list_all():
    rows = db.query("SELECT * FROM icp_templates ORDER BY updated_at DESC")
    out = []
    for r in rows:
        d = _row(r, full=False)
        d["lead_count"] = db.query_one("SELECT COUNT(*) n FROM leads WHERE icp_id=?", (r["id"],))["n"]
        d["sample_count"] = db.query_one("SELECT COUNT(*) n FROM icp_samples WHERE icp_id=?", (r["id"],))["n"]
        out.append(d)
    return out


def get(icp_id: int):
    d = _row(db.query_one("SELECT * FROM icp_templates WHERE id=?", (icp_id,)))
    if not d:
        raise UserError("icp_not_found", 404)
    return d


def _words_lang(w: str) -> str:
    return "zh" if re.search(r"[\u3400-\u9fff]", w) else "latin"


def add_languages(icp: dict, old: dict | None = None) -> dict:
    """保存模板时自动补多语言（PRD §3 / U33）：客户新加的求购词 / 产品词，由 AI 补上中 / 英 / 马来文说法。
    只翻译这次新加的词（没有新词就不调用模型）；AI 不可用时原样保存。"""
    from .. import settings
    if not settings.agent_enabled(AGENT) or not llm.configured(AGENT):
        return icp
    out = dict(icp)
    for field in ("buy_words", "product_words"):
        new = [w for w in icp.get(field) or [] if w not in ((old or {}).get(field) or [])]
        if not new or old is None:
            continue
        try:
            by_lang = translate_keywords(new[:20], langs=list(countries.LEAD_LANGS))
        except UserError:
            continue
        extra = [k for x in by_lang for k in x["keywords"]]
        out[field] = _list(out[field] + extra)[:WORDS_MAX]
    return out


def save(data: dict, icp_id: int | None = None, reasons=None, chat_log=None, origin="manual"):
    data = dict(data)
    if icp_id:
        previous = get(icp_id)["data"]
        for field in ("geo_mode", "target_markets", "b2b_segment"):
            if field not in data and field in previous:
                data[field] = previous[field]
    icp = normalize(data, strict=True)
    now = db.now()
    if icp_id:
        prev = normalize(get(icp_id)["data"])
        icp = add_languages(icp, prev)
        db.execute("UPDATE icp_templates SET name=?, data=?, reasons=COALESCE(?, reasons), chat=COALESCE(?, chat), "
                   "updated_at=? WHERE id=?",
                   (icp["name"], db.dumps(icp), db.dumps(reasons) if reasons is not None else None,
                    db.dumps(chat_log) if chat_log is not None else None, now, icp_id))
        return get(icp_id)
    if origin == "manual":
        icp = add_languages(icp, {})          # 客户自己新建、手填的词：也补上其他语言
    new_id = db.execute("INSERT INTO icp_templates(name, data, reasons, chat, origin, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (icp["name"], db.dumps(icp), db.dumps(reasons or []), db.dumps(chat_log or []),
                         origin, now, now))
    from . import learning
    learning.ensure_defaults(new_id)
    return get(new_id)


def copy(icp_id: int, suffix: str):
    src = get(icp_id)
    data = dict(src["data"])
    data["name"] = (data.get("name") or src["name"]) + suffix
    return save(data, reasons=src.get("reasons"), chat_log=[], origin="copy")


def delete(icp_id: int):
    get(icp_id)
    with db.transaction():
        db.execute("DELETE FROM icp_templates WHERE id=?", (icp_id,))
        db.execute("DELETE FROM icp_samples WHERE icp_id=?", (icp_id,))
        db.execute("DELETE FROM icp_knowledge WHERE icp_id=?", (icp_id,))
        db.execute("UPDATE leads SET icp_id=NULL WHERE icp_id=?", (icp_id,))


# ------------------------------------------------------------------ 第 2 步：必填检查、生效数据

REQUIRED = ("product", "customer_types")      # 目标国家不再必填（PRD §14）


def required_missing(data: dict) -> list[str]:
    """开始任务前必须完成的项（PRD 3.0 / 14：我方产品/服务、客户类型）。目标国家、开发信落款不再必填
    （v1 不发开发信；落款以后销售员上线时再要）。"""
    from .registry import FEATURES
    d = normalize(data)
    miss = [f for f in REQUIRED if not d[f]]
    if FEATURES["outreach"]:
        from .. import settings
        if not str((settings.get("company_profile", {}) or {}).get("signature") or "").strip():
            miss.append("signature")
    return miss


def effective_data(icp_id: int) -> dict:
    """任务实际使用的模板 = 客户填写的模板 + AI 学到的关键词 / 排除项（自动生效、可回退）。"""
    from . import learning
    d = normalize(get(icp_id)["data"])
    k = learning.effective(icp_id)
    d["product_words"] = list(dict.fromkeys((d.get("product_words") or []) + k["keywords"]))
    d["exclude_words"] = list(dict.fromkeys((d.get("exclude_words") or []) + k["exclude_keywords"]))
    d["exclude_companies"] = list(dict.fromkeys((d.get("exclude_companies") or []) + k["exclude_companies"]))
    return d


# ------------------------------------------------------------------ 线索词 / 打分标准（由 AI 根据客户资料生成）

def needs_rules(data: dict) -> bool:
    return not (data.get("buy_words") and data.get("product_words") and data.get("grading_criteria"))


def ensure_rules(icp_id: int, task_id=None) -> bool:
    """模板缺少求购词 / 产品词或打分标准时（旧模板、客户手动建的模板）让销售主管根据模板生成一次并保存
    （只补空的，客户写的不动）。返回是否生成了。失败不影响任务（数据师按模板整体判断）。"""
    from .. import settings
    try:
        d = get(icp_id)
    except UserError:
        return False
    data = normalize(d["data"])
    if not needs_rules(data) or not settings.agent_enabled(AGENT):
        return False
    lang = settings.get("ui_lang")
    try:
        out, _ = llm.call_json(AGENT, "icp_derive", _system(lang),
                               prompts.render("icp.derive_task", icp_json=json.dumps(data, ensure_ascii=False)),
                               schemas.ICP_DERIVE, effort="low", task_id=task_id, context={"icp": data, "lang": lang})
    except UserError:
        return False
    merged = normalize({**data, "buy_words": data["buy_words"] or out.get("buy_words") or [],
                        "product_words": data["product_words"] or out.get("product_words") or [],
                        "grading_criteria": data["grading_criteria"] or out.get("grading_criteria") or []})
    if needs_rules(merged) and merged == data:
        return False
    db.execute("UPDATE icp_templates SET data=?, updated_at=? WHERE id=?", (db.dumps(merged), db.now(), icp_id))
    from . import learning
    learning.seed_weights(icp_id, merged)
    return True


def lead_words(data: dict) -> dict:
    """线索词（全部来自客户的模板）：{"buy", "product", "exclude"}，小写、去重、去掉太短的。"""
    clean = lambda ws: [w for w in dict.fromkeys(str(w).strip().lower() for w in ws or [] if w and str(w).strip())
                        if len(w) >= 2]
    return {"buy": clean(data.get("buy_words")), "product": clean(data.get("product_words")),
            "exclude": clean(data.get("exclude_words"))}


def _rx(words: list[str]):
    """英文 / 马来文的词按词首匹配（inverter 也能匹配 inverters，但 need 不匹配 kneed）；中文等直接按包含匹配。"""
    if not words:
        return None
    parts = [(r"(?<![a-z0-9])" + re.escape(w)) if re.fullmatch(r"[a-z0-9][\x20-\x7e]*", w) else re.escape(w)
             for w in sorted(words, key=len, reverse=True)]
    return re.compile("|".join(parts))


def match_words(text: str, w: dict) -> str:
    """程序规则（PRD §3）：命中任一求购词或产品词、且没有命中排除词 → "hit"；命中排除词 → "excluded"；都没命中 → ""。"""
    low = (text or "").lower()
    ex = w.get("_ex_rx", ...)
    if ex is ...:
        ex = w["_ex_rx"] = _rx(w.get("exclude") or [])
        w["_in_rx"] = _rx((w.get("buy") or []) + (w.get("product") or []))
    if ex is not None and ex.search(low):
        return "excluded"
    inc = w.get("_in_rx")
    return "hit" if inc is not None and inc.search(low) else ""


def search_queries(data: dict, n: int, lang: str = "en") -> list[dict]:
    """搜索类渠道（Google / 网页 / YouTube）的搜索词 = 产品词 × 求购词 + 国家（PRD §3）。中文词配中文词、
    外文词配外文词；国家名用对应语言。轮流取不同的产品词，尽量不重复。"""
    w = {"buy": [x for x in data.get("buy_words") or [] if x.strip()],
         "product": [x for x in data.get("product_words") or [] if x.strip()]}
    codes = [c for c in (data.get("target_markets") if edition.served_market(data) else data.get("countries")) or [] if c]
    out, seen = [], set()
    prods = w["product"] or [""]
    buys = w["buy"] or [""]
    for i in range(max(len(prods), len(buys)) * max(1, len(codes) or 1) * 3):
        p = prods[i % len(prods)]
        same = [b for b in buys if not p or not b or _words_lang(b) == _words_lang(p)] or buys
        b = same[(i // len(prods)) % len(same)]
        code = codes[i % len(codes)] if codes else ""
        zh = _words_lang(p or b) == "zh"
        cn = countries.name(code, "zh" if zh else "en") if code else ""
        q = " ".join(x for x in (p, b, cn) if x).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append({"q": q, "country": code, "lang": "zh" if zh else ""})
        if len(out) >= n:
            break
    for p in w["product"]:                          # 词少时：再加 产品词 + 国家
        if len(out) >= n:
            break
        for code in codes or [""]:
            cn = countries.name(code, "zh" if _words_lang(p) == "zh" else "en") if code else ""
            q = f"{p} {cn}".strip()
            if q.lower() not in seen and len(out) < n:
                seen.add(q.lower())
                out.append({"q": q, "country": code, "lang": "zh" if _words_lang(p) == "zh" else ""})
    return out


def criteria_text(data: dict, weights: dict | None = None) -> str:
    """数据师提示词里的“打分标准”：来自模板（AI 根据客户资料生成），权重可被学习调整（按名称覆盖）。"""
    lines = []
    for c in data.get("grading_criteria") or []:
        w = (weights or {}).get(c["name"], c.get("weight"))
        lines.append(f"- {c['name']} (weight {w:g}): {c.get('description', '')}" if isinstance(w, (int, float))
                     else f"- {c['name']}: {c.get('description', '')}")
    return "\n".join(lines) or "- (no explicit criteria yet: judge the fit against the template as a whole)"

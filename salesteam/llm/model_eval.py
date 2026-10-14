"""AI 自动选模型（PRD 2.4.1）：看目录 → 初选 → 实测 → 给出一个方案 → 客户确认后一键配置。

- 顾问模型：按规则自动选（relay.pick_advisor），只负责初选和给写作类测试打分；不在界面显示；配置完成后退出
- 实测：内置小测试集（通用占位材料：消息分类、名单页提取公司、按模板打分、写双语短消息），记录
  拒绝、格式正确、质量、费用、速度；以实测为主，不以网上评测为准
- 选法“够用就好”（老板决定，见 select()）：先过门槛（格式全对 + 岗位质量线，主管的线更高，监控员的更低），
  过了门槛的质量不再加分；主模型 = 价格 / 速度 / 拒答加权最好；备用 = 其余合格模型里拒答最少。
  门槛和权重在 presets.json（功能配置）。没有“省钱 / 均衡 / 最强”三档，客户确认即可
- 预计费用 ≤ $1 自动执行；> $1 等客户确认
- 之后的重新评估由销售主管发起（第 3 步），客户一键同意即重跑
"""
import json
import time

from .. import db, settings
from ..errors import UserError
from . import relay
from .base import AgentConfig, LLMRequest

ROLES = ["manager", "scout", "researcher", "monitor", "analyst", "sales", "followup", "decision"]
ROLE_TESTS = {"manager": ["grade", "write"], "researcher": ["extract"], "monitor": ["classify"],
              "analyst": ["grade", "extract"], "sales": ["write"], "followup": ["classify"], "decision": ["classify"],
              "scout": ["classify"]}
UNKNOWN_PRICE = (3.0, 15.0)     # 估算时对未知价格的保守假设

# 选模型用的测试材料：通用占位内容（“示例产品 X”“示例公司”），不含任何行业 / 产品 / 国家假设（PRD 2.4.2）。
# 只用来比较模型能力：多语言（中 / 英 / 马来）分类、从名单页提取公司、按模板打分、写短消息。
CLASSIFY = [
    ("Hi all, we are looking for a supplier of Product X, about 500 units per month. Please DM your price.", "buy"),
    ("🔥 Best price for Product X!!! Order now, DM us 👉", "ad"),
    ("Saya nak cari pembekal Produk X, perlukan 300 unit bulan depan. Minta sebut harga.", "buy"),
    ("Good morning everyone ☀️", "chat"),
    ("我们公司需要采购 2000 件 X 产品，请有货源的朋友报价", "buy"),
    ("Jual Produk X, diskaun 30% minggu ini sahaja, WhatsApp kami", "ad"),
    ("Has anyone been to the trade fair last week? How was it?", "chat"),
    ("Ada supplier Produk X? Harga borong untuk 200 unit.", "buy"),
    ("加入我们的VIP群，每天100倍信号 🚀", "ad"),
    ("大家一般用哪个牌子的 X 产品？", "chat"),
]
EXTRACT_PAGE = """# Members — Example Trade Association (sample page)
## Alpha Trading Co.
Distributor of Product X. Email: office@alpha-trading.example
## Beta Supply Ltd
Service network for Product X. Contact: sales@beta-supply.example
## Gamma Wholesale
Pengedar Produk X.
## Delta Imports
X 产品进口商。www.delta-imports.example"""
EXTRACT_EXPECTED = ["Alpha Trading", "Beta Supply", "Gamma Wholesale", "Delta Imports"]
GRADE_PAGE = """Company: Alpha Trading Co. Distributor of Product X, 80 staff, supplies 400 resellers.
Recently posted: "looking for a second supplier of Product X, 2,000 units/year"."""
GRADE_ICP = "We sell Product X. Target: distributors and wholesalers with 10-500 staff."
WRITE_BRIEF = ("Write a short first message (max 120 words each) to Alpha Trading Co., in English AND Malay, from "
               "Example Company (maker of Product X). Mention their search for a second supplier. No prices.")

SCHEMAS = {
    "classify": {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["idx", "label"],
                                   "properties": {"idx": {"type": "integer"}, "label": {"type": "string",
                                                                                       "enum": ["buy", "ad", "chat"]}}}}}},
    "extract": {"type": "object", "additionalProperties": False, "required": ["companies"],
                "properties": {"companies": {"type": "array", "items": {"type": "string"}}}},
    "grade": {"type": "object", "additionalProperties": False, "required": ["grade", "reason"],
              "properties": {"grade": {"type": "string", "enum": ["A", "B", "C", "D"]}, "reason": {"type": "string"}}},
    "judge": {"type": "object", "additionalProperties": False, "required": ["score", "reason"],
              "properties": {"score": {"type": "number"}, "reason": {"type": "string"}}},
}


def state() -> dict:
    return settings.get("model_eval", {}) or {}


def _put(**kw):
    s = state()
    s.update(kw)
    settings.put("model_eval", s)
    return s


def _cost(m, inp, out):
    p = (m["price_in"], m["price_out"]) if m.get("price_in") is not None else None
    if p is None:
        manual = (settings.get("relay_prices", {}) or {}).get(f"{m['relay']}::{m['id']}")
        p = tuple(manual) if manual else None
    return (inp / 1e6 * p[0] + out / 1e6 * (p[1] or 0)) if p else None


def _cheapness(m):
    if m.get("price_in") is None:
        return 1e9
    return m["price_in"] + (m["price_out"] or 0) / 4


# ------------------------------------------------------------------ 1-2. 目录 + 初选

def shortlist(emit=lambda *a, **k: None, roles=None, relay_id: str = "") -> dict:
    models = relay.catalog(relay_id) if relay_id else relay.all_models()
    if not models:
        raise UserError("relay_no_models")
    pr = relay.presets()
    # 同名模型在多个中转站：保留有价格、更便宜的那一个来比较
    best_by_norm = {}
    for m in models:
        cur = best_by_norm.get(m["norm"])
        if cur is None or (_cheapness(m) < _cheapness(cur)):
            best_by_norm[m["norm"]] = m
    uniq = list(best_by_norm.values())
    usable = [m for m in uniq if m.get("structured") is not False]
    # 规则初选（提示表 + 价格档）—— 也是顾问失败时的后备
    fallback = {}
    for role, cfg in pr["roles"].items():
        hits = [m for h in cfg["hint"] for m in usable if h in m["norm"]]
        priced = sorted([m for m in usable if m["price_in"] is not None], key=_cheapness)
        extra = ([priced[len(priced) // 2], priced[0], priced[-1]] if priced else usable[:3])
        seen, out = set(), []
        for m in hits + extra:
            if m["norm"] not in seen:
                seen.add(m["norm"])
                out.append(m)
        fallback[role] = [m["id"] for m in out[: cfg["candidates"]]]
    # 顾问初选（看完整目录）
    emit("ev.me_shortlisting", n=len(uniq))
    digest = "\n".join(f"{m['id']} | in ${m['price_in'] if m['price_in'] is not None else '?'} / out "
                       f"${m['price_out'] if m['price_out'] is not None else '?'} per 1M | ctx {m['context']}"
                       for m in sorted(usable, key=_cheapness)[:120])
    adv = relay.advisor_config()
    from ..agents.registry import is_active
    only_roles = list(roles or [])            # 只给这几个岗位选（下面的 roles 变量是顾问的初选结果，别混用）
    role_list = [r for r in pr["roles"] if is_active(r) and (not only_roles or r in only_roles)]   # v1：不给销售员 / 跟进员选模型
    schema = {"type": "object", "additionalProperties": False, "required": ["roles"], "properties": {"roles": {
        "type": "object", "additionalProperties": False, "required": role_list,
        "properties": {r: {"type": "array", "items": {"type": "string"}} for r in role_list}}}}
    role_desc = {"manager": "manager (planning and coordination; needs the highest quality bar)",
                 "scout": "channel scout (evaluate chat groups and channels, cheap & fast)", "researcher": "researcher (read web pages, extract companies)",
                 "monitor": "monitor (classify many short chat messages, cheap & fast)",
                 "analyst": "analyst (grade leads, write profiles)", "sales": "sales (write outreach in local languages)",
                 "followup": "followup (classify replies, short answers)"}
    roles = {}
    try:
        if not role_list:                       # 只给判断模型选：不用顾问初选
            raise LookupError
        data = _call(adv, "eval_shortlist", "You pick candidate LLMs for each role of a B2B sales agent team. Prefer "
                     "models good at multilingual text, JSON output and instruction following; mix price levels; 2-3 "
                     "per role. Use exact ids from the list.",
                     "Roles: " + ", ".join(role_desc.get(r, r) for r in role_list) + ".\n\nModels:\n" + digest, schema, context={"fallback": fallback}, agent="setup")[0]
        roles = data.get("roles") or {}
    except (UserError, LookupError):
        roles = {}
    ids = {m["id"]: m for m in uniq}
    cands = {}
    for role, cfg in pr["roles"].items():
        if role not in role_list:
            continue
        chosen = [ids[i] for i in roles.get(role, []) if i in ids]
        for i in fallback[role]:
            if len(chosen) >= cfg["candidates"]:
                break
            if i in ids and ids[i] not in chosen:
                chosen.append(ids[i])
        cands[role] = chosen[: cfg["candidates"]]
    # 判断模型：有 Decisions API（OpenRouter）就把决策模型也作为候选；最便宜的大模型兜底
    dec = []
    for r in relay.relays():
        if r.get("decisions"):
            dec += [{"relay": r["id"], "id": d, "norm": relay.norm(d), "price_in": 0.042 if "microsoft" in d else None,
                     "price_out": 0.0, "decisions": True} for d in pr.get("decision_candidates", [])]
    cheapest = sorted([m for m in usable if m["price_in"] is not None], key=_cheapness)[:3] or usable[:2]
    if not only_roles or "decision" in only_roles:
        # 判断模型和其他 AI 一样（PRD 2.3）：「重新选模型」的整体方案包含它（客户确认后才用上），
        # 它自己那一行的「让主管帮我选模型」只选它；不会在客户不知道的时候配置它
        cands["decision"] = dec + cheapest
    return {role: [_slim(m) for m in ms] for role, ms in cands.items()}


def _slim(m):
    return {k: m.get(k) for k in ("relay", "id", "norm", "price_in", "price_out", "context", "decisions")}


def estimate(cands: dict) -> float:
    total = 0.0
    adv = state().get("advisor_price") or UNKNOWN_PRICE
    done = set()
    for role, ms in cands.items():
        for m in ms:
            for t in ROLE_TESTS[role]:
                key = (m["relay"], m["id"], t)
                if key in done:
                    continue
                done.add(key)
                inp, out = (1500, 600) if t == "write" else (1200, 300)
                c = _cost(m, inp, out)
                total += c if c is not None else inp / 1e6 * UNKNOWN_PRICE[0] + out / 1e6 * UNKNOWN_PRICE[1]
                if t == "write":
                    total += 1200 / 1e6 * adv[0] + 150 / 1e6 * adv[1]
    return round(total, 4)


# ------------------------------------------------------------------ 3. 实测

def _call(cfg: AgentConfig, purpose, system, user, schema, context=None, agent="setup"):
    from . import budget_state, business_context, record
    if budget_state()["paused"]:
        raise UserError("budget_exceeded", 402, period="@period.day", used="", limit="")
    req = LLMRequest(system=business_context() + system, messages=[{"role": "user", "content": user}], purpose=purpose,
                     json_schema=schema, max_tokens=4000, context=context or {})
    t = time.time()
    try:
        resp = relay.complete(cfg, req)
    except UserError as e:
        record(agent, cfg, purpose, None, ok=False, refused=e.key == "model_refused")
        raise
    cost = record(agent, cfg, purpose, resp)
    from . import _parse_json
    data = _parse_json(resp.text) if schema else resp.text
    return data, {"cost": cost, "latency": time.time() - t, "raw": resp.text}


def _run_test(m, test, adv_cfg) -> dict:
    out = {"refused": False, "format_ok": False, "score": 0.0, "cost": 0.0, "latency": 0.0, "error": ""}
    try:
        if m.get("decisions"):
            return _run_decision_test(m)
        cfg = AgentConfig(agent="setup", provider="relay", preset=m["relay"], model=m["id"], effort="low")
        if test == "classify":
            items = "\n".join(f"[{i}] {t}" for i, (t, _) in enumerate(CLASSIFY))
            data, meta = _call(cfg, "eval_classify", "Classify each Telegram message: buy (wants to buy/source), ad "
                               "(selling/spam), chat (other). Return JSON.", items, SCHEMAS["classify"],
                               context={"labels": [lab for _, lab in CLASSIFY], "options": ["buy", "ad", "chat"]})
            ok = isinstance(data, dict) and isinstance(data.get("items"), list)
            got = {it.get("idx"): it.get("label") for it in (data or {}).get("items", []) if isinstance(it, dict)} if ok else {}
            out["score"] = sum(1 for i, (_, lab) in enumerate(CLASSIFY) if got.get(i) == lab) / len(CLASSIFY)
        elif test == "extract":
            data, meta = _call(cfg, "eval_extract", "List the company names on this page. Return JSON.", EXTRACT_PAGE,
                               SCHEMAS["extract"], context={"expected": EXTRACT_EXPECTED})
            ok = isinstance(data, dict) and isinstance(data.get("companies"), list)
            got = [str(x).lower() for x in (data or {}).get("companies", [])] if ok else []
            tp = sum(1 for e in EXTRACT_EXPECTED if any(e.lower() in g for g in got))
            prec = tp / max(1, len(got))
            rec = tp / len(EXTRACT_EXPECTED)
            out["score"] = 0 if not tp else 2 * prec * rec / (prec + rec)
        elif test == "grade":
            data, meta = _call(cfg, "eval_grade", "Grade this lead A/B/C/D against the profile (A = clear buying signal "
                               "and good fit). Return JSON.", f"Profile: {GRADE_ICP}\n\n{GRADE_PAGE}", SCHEMAS["grade"],
                               context={"expected": "A"})
            ok = isinstance(data, dict) and data.get("grade") in ("A", "B", "C", "D")
            out["score"] = {"A": 1.0, "B": 0.6}.get((data or {}).get("grade"), 0.0) if ok else 0.0
        else:   # write
            text, meta = _call(cfg, "eval_write", "You write concise, polite B2B outreach emails.", WRITE_BRIEF, None)
            ok = bool(text and len(str(text)) > 40)
            jd, jm = _call(adv_cfg, "eval_judge", "You are a strict judge of B2B outreach emails. Score 0-10 for "
                           "personalisation, correctness of both languages, tone, no prices. Return JSON.",
                           f"Brief: {WRITE_BRIEF}\n\nEmail:\n{text}", SCHEMAS["judge"])
            meta["cost"] += jm["cost"]
            out["score"] = max(0.0, min(1.0, float((jd or {}).get("score") or 0) / 10)) if ok else 0.0
        out.update(format_ok=bool(ok), cost=meta["cost"], latency=meta["latency"])
        if not ok:
            out["score"] = 0.0
    except UserError as e:
        out["refused"] = e.key == "model_refused"
        out["error"] = e.key
        if e.key == "budget_exceeded":
            raise
    return out


def _run_decision_test(m) -> dict:
    from . import decision
    key = relay.relay_key(m["relay"])
    opts = {"buy": "wants to buy / source products", "ad": "selling or spam", "chat": "other conversation"}
    correct, t0, cost = 0, time.time(), 0.0
    for text, lab in CLASSIFY:
        probs, extra = decision._openrouter(text, opts, "What kind of message is this?", m["id"], key)
        if max(probs, key=probs.get) == lab:
            correct += 1
    return {"refused": False, "format_ok": True, "score": correct / len(CLASSIFY), "cost": cost,
            "latency": (time.time() - t0) / len(CLASSIFY), "error": ""}


def run(task_run):
    """model_eval 任务主体（后台运行，与快速开始向导并行）。"""
    emit = lambda k, **p: task_run.emit("manager", k, **p)   # noqa: E731
    _put(status="shortlisting", started_at=time.time(), error="")
    adv = relay.advisor_config()
    adv_m = next((m for m in relay.catalog(adv.preset) if m["id"] == adv.model), None)
    _put(advisor_price=[adv_m["price_in"], adv_m["price_out"]] if adv_m and adv_m["price_in"] is not None else None)
    only = task_run.params.get("roles") or None          # 只给这几个岗位选（设置里“让主管帮我选模型”）
    cands = shortlist(emit, roles=only, relay_id=task_run.params.get("relay") or "")
    est = estimate(cands)
    _put(candidates=cands, estimate_usd=est)
    emit("ev.me_estimate", usd=f"{est:.2f}")
    limit = float(relay.presets().get("auto_run_max_usd", 1.0))
    if est > limit and not task_run.params.get("confirmed"):
        _put(status="awaiting_confirm")
        emit("ev.me_need_confirm", level="warn", usd=f"{est:.2f}")
        return
    _put(status="testing")
    results: dict[str, list] = {}
    cache = {}
    spent = 0.0
    n_total = sum(len(ms) for ms in cands.values())
    done = 0
    for role, ms in cands.items():
        results[role] = []
        for m in ms:
            if task_run.cancel_flag.is_set():
                _put(status="cancelled")
                return
            tests = []
            for t in ROLE_TESTS[role]:
                key = (m["relay"], m["id"], t, bool(m.get("decisions")))
                if key not in cache:
                    emit("ev.me_testing", model=relay.norm(m["id"]), test=f"@me.test.{t}", role=f"@agent.{role}")
                    cache[key] = _run_test(m, t, adv)
                    spent += cache[key]["cost"] or 0
                tests.append(cache[key])
            n = len(tests)
            r = {**m, "score": round(sum(x["score"] for x in tests) / n, 3),
                 "refusal_rate": round(sum(1 for x in tests if x["refused"]) / n, 3),
                 "format_rate": round(sum(1 for x in tests if x["format_ok"]) / n, 3),
                 "cost": round(sum(x["cost"] or 0 for x in tests), 5),
                 "latency": round(sum(x["latency"] for x in tests) / n, 2),
                 "errors": [x["error"] for x in tests if x["error"]]}
            results[role].append(r)
            done += 1
            task_run.stats.update(models_done=done, models_total=n_total)
            task_run.bump()
    rec = recommend(results)
    _put(status="done", results=results, recommendation=rec, actual_usd=round(spent, 4), finished_at=time.time())
    emit("ev.me_done", level="success", usd=f"{spent:.2f}")
    if only:
        apply_roles(only)                                # 单个岗位：选完直接用上（主模型 + 拒答最少的备用）


def apply_roles(roles: list[str]) -> dict:
    """只把实测方案里这几个岗位的主模型 + 备用用上（不动其他 AI）。"""
    from . import backup, update_config
    rec = (state().get("recommendation") or {}).get("balanced") or {}
    out = {}
    fbs = dict(settings.get("model_fallbacks", {}) or {})
    for role in roles:
        p = (rec.get("plan") or {}).get(role)
        if not p:
            continue
        if role == "decision":                       # 判断模型：只有主模型，没有备用
            from . import decision
            decision.configure(p["relay"], p["id"])
            out[role] = p["norm"]
            continue
        update_config(role, {"provider": "relay", "preset": p["relay"], "model": p["id"], "base_url": ""})
        if p.get("fallback"):
            fbs[role] = {"provider": "relay", "relay": p["fallback"]["relay"], "model": p["fallback"]["id"],
                         "auto": True}
        else:
            fbs.pop(role, None)
        out[role] = p["norm"]
    settings.put("model_fallbacks", fbs)
    for role in roles:
        if role != "decision" and not backup.get(role):
            backup.auto_assign(role, check=False)
    return out


# ------------------------------------------------------------------ 4. 方案

def _norm_scores(vals: list, lower_better=True) -> list[float]:
    """把一组数值归一到 0–1（越好越接近 1）；None 视为最差。"""
    known = [v for v in vals if v is not None]
    if not known:
        return [0.5 for _ in vals]
    lo, hi = min(known), max(known)
    out = []
    for v in vals:
        if v is None:
            out.append(0.0)
        elif hi - lo < 1e-12:
            out.append(1.0)
        else:
            x = (v - lo) / (hi - lo)
            out.append(1 - x if lower_better else x)
    return out


def select(rs: list[dict], role: str) -> tuple[dict | None, dict | None, list[dict]]:
    """“够用就好”（PRD 2.4.1）：先过门槛（格式全对 + 岗位质量线），过了门槛的质量不再加分；
    主模型 = 价格 / 速度 / 拒答 加权得分最高；备用 = 其余合格模型里拒答最少（再看价格、不同厂商）。
    门槛和权重都在 presets.json（功能配置，不是业务内容）。"""
    pr = relay.presets()
    bar = pr["decision_quality_bar"] if role == "decision" else pr["roles"].get(role, {}).get("quality_bar", 0.65)
    fbar = float(pr.get("format_bar", 1.0))
    ok = [r for r in rs if r["score"] >= bar - 1e-9 and r.get("format_rate", 0) >= fbar - 1e-9]
    if not ok:
        return None, None, []
    w = pr["selection_weights"]
    sp = _norm_scores([None if r.get("price_in") is None else _cheapness(r) for r in ok])
    ss = _norm_scores([r.get("latency") for r in ok])
    sr = _norm_scores([r.get("refusal_rate", 0) for r in ok])
    for r, a, b, c in zip(ok, sp, ss, sr):
        r["sel_score"] = round(w["price"] * a + w["speed"] * b + w["refusal"] * c, 4)
    pick = max(ok, key=lambda r: (r["sel_score"], -_cheapness(r)))
    rest = [r for r in ok if (r["id"], r["relay"]) != (pick["id"], pick["relay"]) and r["norm"] != pick["norm"]]
    pv = pick.get("vendor") or relay.vendor_of(pick["id"])
    second = min(rest, key=lambda r: (r.get("refusal_rate", 0), _cheapness(r),
                                      1 if (r.get("vendor") or relay.vendor_of(r["id"])) == pv else 0)) if rest else None
    return pick, second, ok


def recommend(results: dict) -> dict:
    pr = relay.presets()
    plan = {}
    for role, rs in results.items():
        pick, second, ok = select(rs, role)
        if not pick:
            plan[role] = None
            continue
        mt = pr["monthly_tokens"].get(role, [0, 0])
        monthly = None if pick.get("price_in") is None else round(
            mt[0] / 1e6 * pick["price_in"] + mt[1] / 1e6 * (pick.get("price_out") or 0), 2)
        plan[role] = {"relay": pick["relay"], "id": pick["id"], "norm": pick["norm"], "vendor": pick.get("vendor", ""),
                      "score": pick["score"], "sel_score": pick["sel_score"], "passed": len(ok),
                      "refusal_rate": pick["refusal_rate"], "format_rate": pick["format_rate"],
                      "latency": pick.get("latency"), "price_in": pick.get("price_in"),
                      "price_out": pick.get("price_out"), "monthly_usd": monthly, "decisions": bool(pick.get("decisions")),
                      "fallback": {"relay": second["relay"], "id": second["id"], "norm": second["norm"],
                                   "refusal_rate": second["refusal_rate"]} if second else None}
    total = [p["monthly_usd"] for p in plan.values() if p and p["monthly_usd"] is not None]
    # 只有一个方案（"balanced" 键保留给界面 / 接口兼容）
    return {"balanced": {"plan": plan, "monthly_usd": round(sum(total), 2),
                         "unknown_price": any(p and p["monthly_usd"] is None for p in plan.values())}}


def apply(tier="balanced") -> dict:
    tier = "balanced"                    # 只有一个方案（PRD 2.4.1 “够用就好”）
    from . import decision, test_agent, update_config
    st = state()
    rec = (st.get("recommendation") or {}).get(tier)
    if not rec:
        raise UserError("model_eval_not_ready")
    fallbacks = {}
    applied = {}
    for role, p in rec["plan"].items():
        if not p:
            continue
        if role == "decision":
            # 判断模型和其他 AI 一样绑定 API + 模型；用 Decisions API 还是普通模型由程序按 API 和模型自动决定
            decision.configure(p["relay"], p["id"])
            applied[role] = p["norm"]
            continue
        if role == "manager":
            # 主管用客户在快速开始里给它配置的模型（主管就是“顾问”），这里只给它选备用：
            # 其余达到门槛的模型里拒答最少的（PRD 2.4.1）
            adv = (settings.get("relay_state", {}) or {}).get("advisor") or {}
            if adv.get("id"):
                cand = [r for r in (st.get("results") or {}).get("manager", [])
                        if r["score"] >= relay.presets()["roles"]["manager"].get("quality_bar", 0.8) - 1e-9
                        and relay.norm(r["id"]) != relay.norm(adv["id"])]
                if cand:
                    fb = min(cand, key=lambda r: (r.get("refusal_rate", 0), _cheapness(r)))
                    fallbacks[role] = {"provider": "relay", "relay": fb["relay"], "model": fb["id"], "auto": True}
                applied[role] = relay.norm(adv["id"])
                continue
        update_config(role, {"provider": "relay", "preset": p["relay"], "model": p["id"], "base_url": ""})
        if p.get("fallback"):
            fallbacks[role] = {"provider": "relay", "relay": p["fallback"]["relay"], "model": p["fallback"]["id"],
                               "auto": True}
        applied[role] = p["norm"]
    settings.put("model_fallbacks", fallbacks)
    from . import backup
    backup.ensure_all(check=False)          # 每个 AI 都要有备用模型：实测里没有第二个合格模型的，按规则补一个
    rs = settings.get("relay_state", {}) or {}
    rs["provisional"] = False
    settings.put("relay_state", rs)
    tests = {}
    for role in applied:
        if role == "decision":
            continue
        try:
            info = test_agent(role)
            tests[role] = {"ok": True, "ms": info.get("ms")}
        except UserError as e:
            tests[role] = {"ok": False, "error": e.key}
    _put(status="applied", applied_tier=tier, applied_at=time.time(), connectivity=tests)
    return {"tier": tier, "applied": applied, "tests": tests}


def history_json():
    return json.dumps(state(), ensure_ascii=False)

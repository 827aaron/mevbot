"""大模型统一入口：每个智能体独立配置（厂商/模型/Key/预算），调用前检查预算，调用后记账。

用法：
    data = llm.call_json("analyst", purpose="score_lead", system=..., user=..., schema=SCORE, context={...})
"""
import json
import re
import time

from .. import db, vault
from ..agents.registry import AGENT_KEYS, DEFAULTS as AGENT_DEFAULTS
from ..errors import BudgetExceeded, UserError
from . import claude, mock, openai_compat, providers
from .base import AgentConfig, LLMRequest, LLMResponse

from . import relay  # noqa: E402

ADAPTERS = {"claude": claude, "openai_compat": openai_compat, "gemini": openai_compat,
            "ollama": openai_compat, "mock": mock, "relay": relay}

def business_context() -> str:
    """每次调用最前面的统一说明（提示词库 common.context，客户可改）：工作内容是公开信息、正常的客户开发；
    关于客户业务的一切都来自下面给出的画像 / 模板 / 例子 / 要求。"""
    from .. import prompts
    try:
        t = prompts.get("common.context").strip()
    except UserError:
        return ""
    return t + "\n\n" if t else ""


# ------------------------------------------------------------------ 配置

def init_defaults():
    for key in AGENT_KEYS:
        if not db.query_one("SELECT agent FROM agent_configs WHERE agent=?", (key,)):
            d = AGENT_DEFAULTS[key]
            db.execute("INSERT INTO agent_configs(agent, provider, model, effort, updated_at) VALUES(?,?,?,?,?)",
                       (key, d["provider"], d["model"], d["effort"], db.now()))


def get_config(agent: str) -> AgentConfig:
    row = db.query_one("SELECT * FROM agent_configs WHERE agent=?", (agent,))
    if not row:
        raise UserError("unknown_agent", 404, agent=agent)
    return AgentConfig(agent=agent, provider=row["provider"], model=row["model"],
                       api_key=vault.decrypt(row["api_key_enc"]), base_url=row["base_url"], preset=row["preset"],
                       effort=row["effort"], alloc_daily=row["alloc_daily"], alloc_monthly=row["alloc_monthly"],
                       price_in=row["price_in"], price_out=row["price_out"])


EDITABLE = ("provider", "preset", "base_url", "model", "effort", "price_in", "price_out")


def update_config(agent: str, data: dict):
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    sets, args = [], []
    if "provider" in data and data["provider"] not in providers.PROVIDERS:
        raise UserError("invalid_value", field="provider")
    if "effort" in data and data["effort"] not in providers.PROVIDERS["claude"]["efforts"]:
        raise UserError("invalid_value", field="effort")
    for k in EDITABLE:
        if k in data:
            v = data[k]
            if k in ("price_in", "price_out"):
                if v in ("", None):
                    v = None
                else:
                    try:
                        v = max(0.0, float(v))
                    except (TypeError, ValueError):
                        raise UserError("invalid_value", field=k)
            else:
                v = (v or "").strip()
            sets.append(f"{k}=?")
            args.append(v)
    if "api_key" in data and data["api_key"] is not None:
        key = vault.clean_secret(data["api_key"])
        if key:
            sets.append("api_key_enc=?")
            args.append(vault.encrypt(key))
    if data.get("clear_api_key"):
        sets.append("api_key_enc=''")
    if not sets:
        return
    sets.append("updated_at=?")
    args.append(db.now())
    db.execute(f"UPDATE agent_configs SET {', '.join(sets)} WHERE agent=?", (*args, agent))


def public_config(agent: str) -> dict:
    cfg = get_config(agent)
    return {
        "agent": agent, "provider": cfg.provider, "preset": cfg.preset, "base_url": cfg.base_url,
        "model": cfg.model, "effort": cfg.effort, "alloc_daily": cfg.alloc_daily,
        "alloc_monthly": cfg.alloc_monthly, "price_in": cfg.price_in, "price_out": cfg.price_out,
        "api_key_set": bool(cfg.api_key), "api_key_hint": vault.mask(cfg.api_key),
        "effective_price": list(price_info(cfg, cfg.model)[0]) if cfg.model else None,
        "price_src": price_info(cfg, cfg.model)[1] if cfg.model else "",
        "fallback": _fallback_public(agent),
        "configured": configured(agent),
    }


def configured(agent: str) -> bool:
    """这个 AI 有没有可用的 API（中转站 / 测试模式 / 本地 Ollama / 直连厂商且有 Key）。没配置的 AI 不运行。"""
    try:
        cfg = get_config(agent)
    except UserError:
        return False
    if cfg.provider in ("relay", "mock", "ollama"):
        return bool(cfg.model)
    return bool(cfg.api_key and cfg.model)


def _fallback_public(agent: str):
    from .. import settings as st
    fb = (st.get("model_fallbacks", {}) or {}).get(agent)
    return {"relay": fb.get("relay", ""), "model": fb.get("model", ""), "auto": bool(fb.get("auto"))} if fb else None


# ------------------------------------------------------------------ 价格 / 用量 / 预算

def price_info(cfg: AgentConfig, model: str) -> tuple[tuple[float, float], str]:
    """单价（美元 / 百万 token）和来源（PRD 2.4.1「单价未知要解决」）：
    customer = 客户在这个 AI 的弹窗里填的 > api = API 返回的（Claude 官方内置价也算）> reference = 内置参考价 >
    estimate = 什么都查不到时的偏高估算（美元预算一定生效）。fixed = 测试 / 本地模型。"""
    model = model or cfg.model
    if cfg.price_in is not None and cfg.price_out is not None:
        return (cfg.price_in, cfg.price_out), "customer"
    if cfg.provider == "mock":
        return providers.MOCK_PRICE, "fixed"
    if cfg.provider == "ollama":
        return (0.0, 0.0), "fixed"   # 本地模型不花钱
    if cfg.provider == "relay":
        for m in (model, cfg.model):
            mine = relay.customer_price(cfg.preset, m)
            if mine:
                return mine, "customer"
        try:
            for m in (model, cfg.model):
                cat = next((x for x in relay.catalog(cfg.preset) if x["id"] == m), None)
                if cat and cat.get("price_in") is not None:
                    return (cat["price_in"], cat["price_out"] or 0.0), cat.get("price_src") or "api"
        except UserError:
            pass
    if cfg.provider == "claude":
        p = providers.CLAUDE_PRICES.get(model) or providers.CLAUDE_PRICES.get(cfg.model)
        if p:
            return p, "api"
    ref = relay.reference_price(model) or relay.reference_price(cfg.model)
    if ref:
        return (ref[0], ref[1]), "reference"
    return relay.default_estimate(), "estimate"


def price_for(cfg: AgentConfig, model: str):
    """返回 (输入价, 输出价) 美元/百万 token（总有值：查不到时是偏高的估算）。"""
    return price_info(cfg, model)[0]


def estimate_cost(cfg: AgentConfig, resp: LLMResponse) -> float:
    p = price_for(cfg, resp.model)
    cost = 0.0
    if p:
        plain = max(0, resp.input_tokens - resp.cache_read_tokens - resp.cache_write_tokens)
        cost += (plain + resp.cache_read_tokens * 0.1 + resp.cache_write_tokens * 1.25) / 1e6 * p[0] \
            + resp.output_tokens / 1e6 * p[1]
    if cfg.provider == "claude" and resp.web_searches:
        cost += resp.web_searches / 1000 * providers.CLAUDE_WEB_SEARCH_PER_1K
    return round(cost, 6)


def _day():
    return time.strftime("%Y-%m-%d")


def _month():
    return time.strftime("%Y-%m")


def record(agent, cfg: AgentConfig, purpose, resp: LLMResponse | None, ok=True, task_id=None, refused=False):
    cost = estimate_cost(cfg, resp) if resp else 0.0
    src = price_info(cfg, resp.model if resp else cfg.model)[1] if resp else ""
    # 只有“估算”的才同时算进内部的 token 兜底上限（不给客户看），美元预算照常按估算的费用算
    priced = 0 if src == "estimate" else 1
    db.execute("INSERT INTO usage(agent, ts, day, month, provider, model, purpose, input_tokens, output_tokens, "
               "extra_units, cost, ok, task_id, refused, priced, price_src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (agent, db.now(), _day(), _month(), cfg.provider, (resp.model if resp else cfg.model) or "",
                purpose, resp.input_tokens if resp else 0, resp.output_tokens if resp else 0,
                resp.web_searches if resp else 0, cost, int(ok), task_id, int(refused), priced, src))
    return cost


def approx_src(where: str, args) -> str:
    """一组用量里最“不准”的价格来源：estimate（估算）> reference（参考价）> 其他（准的）。界面据此显示「约 $x（…）」。"""
    rows = {r["price_src"] for r in db.query(f"SELECT DISTINCT price_src FROM usage WHERE {where}", args)}
    return "estimate" if "estimate" in rows else "reference" if "reference" in rows else ""


def _sum(where: str, args) -> dict:
    r = db.query_one("SELECT COALESCE(SUM(cost),0) c, COALESCE(SUM(input_tokens),0) i, "
                     "COALESCE(SUM(output_tokens),0) o, COUNT(*) n, COALESCE(SUM(refused),0) rf "
                     f"FROM usage WHERE {where}", args)
    return {"cost": r["c"], "input_tokens": r["i"], "output_tokens": r["o"], "calls": r["n"], "refused": r["rf"],
            "refusal_rate": round(r["rf"] / r["n"], 3) if r["n"] else 0}


def spent(agent: str | None = None) -> dict:
    """agent=None 时返回全部智能体合计。"""
    if agent:
        return {"today": _sum("agent=? AND day=?", (agent, _day())),
                "month": _sum("agent=? AND month=?", (agent, _month()))}
    return {"today": _sum("day=?", (_day(),)), "month": _sum("month=?", (_month(),))}


def get_budget() -> dict:
    from .. import settings
    b = dict(settings.get("budget"))
    tf = relay.presets().get("budget_token_fallback", {})
    b.setdefault("daily_tokens", tf.get("daily_tokens", 5_000_000))
    b.setdefault("monthly_tokens", tf.get("monthly_tokens", 100_000_000))
    return b


def set_budget(daily, monthly, daily_tokens=None, monthly_tokens=None) -> dict:
    from .. import settings
    try:
        b = {"daily": max(0.0, float(daily or 0)), "monthly": max(0.0, float(monthly or 0))}
        cur = get_budget()
        b["daily_tokens"] = max(0, int(daily_tokens if daily_tokens not in (None, "") else cur["daily_tokens"]))
        b["monthly_tokens"] = max(0, int(monthly_tokens if monthly_tokens not in (None, "") else cur["monthly_tokens"]))
    except (TypeError, ValueError):
        raise UserError("invalid_value", field="budget")
    b["set_by_user"] = True          # U54：客户保存过才显示“你设定”，没改过显示“默认值，可以改”
    settings.put("budget", b)
    return b


def _unpriced_tokens():
    d = db.query_one("SELECT COALESCE(SUM(input_tokens+output_tokens),0) n FROM usage WHERE priced=0 AND day=?", (_day(),))
    m = db.query_one("SELECT COALESCE(SUM(input_tokens+output_tokens),0) n FROM usage WHERE priced=0 AND month=?",
                     (_month(),))
    return d["n"], m["n"]


def budget_state() -> dict:
    """总预算状态：paused=True 表示到达上限，全部智能体暂停（次日/次月自动恢复，或提高上限）。
    价格未知的模型无法按美元计算 → 按 token 数兜底限制（PRD 2.4.1）。"""
    b = get_budget()
    s = spent()
    period = ""
    if b["daily"] > 0 and s["today"]["cost"] >= b["daily"]:
        period = "day"
    elif b["monthly"] > 0 and s["month"]["cost"] >= b["monthly"]:
        period = "month"
    ud, um = _unpriced_tokens()
    td, tm = int(b.get("daily_tokens") or 0), int(b.get("monthly_tokens") or 0)
    if not period and td and ud >= td:
        period = "day_tokens"
    elif not period and tm and um >= tm:
        period = "month_tokens"
    return {"paused": bool(period), "period": period, "budget": b, "spent": s,
            "unpriced_tokens": {"today": ud, "month": um}}


def check_budget(agent: str, cfg: AgentConfig):
    st = budget_state()
    if st["paused"]:
        if st["period"] in ("day_tokens", "month_tokens"):
            per = st["period"].split("_")[0]
            raise BudgetExceeded(per, st["unpriced_tokens"]["today" if per == "day" else "month"],
                                 st["budget"]["daily_tokens" if per == "day" else "monthly_tokens"], tokens=True)
        if st["period"] == "day":
            raise BudgetExceeded("day", st["spent"]["today"]["cost"], st["budget"]["daily"])
        raise BudgetExceeded("month", st["spent"]["month"]["cost"], st["budget"]["monthly"])
    # 第 3 步：销售主管在总预算内给单个智能体分配的额度
    if cfg.alloc_daily is not None or cfg.alloc_monthly is not None:
        mine = spent(agent)
        if cfg.alloc_daily is not None and mine["today"]["cost"] >= cfg.alloc_daily:
            raise BudgetExceeded("day", mine["today"]["cost"], cfg.alloc_daily, agent=agent)
        if cfg.alloc_monthly is not None and mine["month"]["cost"] >= cfg.alloc_monthly:
            raise BudgetExceeded("month", mine["month"]["cost"], cfg.alloc_monthly, agent=agent)


def usage_dashboard() -> dict:
    from ..agents.registry import is_active
    agents = []
    for key in AGENT_KEYS:
        if not is_active(key) and not db.query_one("SELECT 1 FROM usage WHERE agent=? LIMIT 1", (key,)):
            continue
        cfg = get_config(key)
        agents.append({"agent": key, "provider": cfg.provider, "model": cfg.model, "spent": spent(key),
                       "approx": _approx(key), "alloc_daily": cfg.alloc_daily, "alloc_monthly": cfg.alloc_monthly})
    if db.query_one("SELECT 1 FROM usage WHERE agent='setup' LIMIT 1"):
        agents.append({"agent": "setup", "provider": "relay", "model": "", "spent": spent("setup"), "approx": _approx("setup"),
                       "alloc_daily": None, "alloc_monthly": None})
    from . import decision
    dc = decision.get_config()
    agents.append({"agent": decision.AGENT, "provider": dc["provider"],
                   "model": dc["model"] if decision.configured(dc) else "", "spent": spent(decision.AGENT),
                   "approx": _approx(decision.AGENT), "alloc_daily": None, "alloc_monthly": None})
    total = budget_state()
    total["approx"] = {"today": approx_src("day=?", (_day(),)), "month": approx_src("month=?", (_month(),))}
    return {"total": total, "agents": agents}


def _approx(agent: str) -> dict:
    """这个 AI 今天 / 本月的费用里有没有按参考价 / 估算算的（界面显示「约 $x（参考价 / 估算）」）。"""
    return {"today": approx_src("agent=? AND day=?", (agent, _day())), "month": approx_src("agent=? AND month=?", (agent, _month()))}


# ------------------------------------------------------------------ 调用

def _parse_json(text: str):
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            pass
    return None


KEY_PROBLEMS = ("key_invalid", "key_no_permission", "key_missing")


def call(agent: str, purpose: str, system: str, user: str, schema: dict | None = None, web_search=False,
         context: dict | None = None, on_status=None, task_id=None, max_tokens=32000,
         effort: str | None = None) -> LLMResponse:
    from .. import alerts, settings as st
    cfg = get_config(agent)
    if cfg.provider not in ADAPTERS:
        raise UserError("invalid_value", field="provider")
    # AI 启用开关（PRD 2.1.2）：关闭的 AI 不消耗 token —— 由程序把关
    if not st.agent_enabled(agent):
        raise UserError("agent_disabled", agent=f"@agent.{agent}")
    if effort and cfg.provider == "claude":
        cfg.effort = effort
    check_budget(agent, cfg)
    adapter = ADAPTERS[cfg.provider]
    system = business_context() + system
    req = LLMRequest(system=system, messages=[{"role": "user", "content": user}], purpose=purpose,
                     json_schema=schema, web_search=web_search and cfg.provider == "claude",
                     max_tokens=max_tokens, context=context or {}, on_status=on_status)
    from .. import heal
    tmo = float(heal.cfg()["call_timeout_s"])
    if cfg.provider == "relay":
        tmo = tmo * 2 + 30            # 中转站内部还有自己的超时 + 换中转站

    def _run(c):
        return heal.call_with_timeout(lambda: ADAPTERS[c.provider].complete(c, req), tmo)
    try:
        try:
            resp = _run(cfg)
        except heal.CallTimeout:
            # 没反应：先重试一次，仍然超时就换备用模型（PRD 9.0）
            record(agent, cfg, purpose + ":timeout", None, ok=False, task_id=task_id)
            try:
                resp = _run(cfg)
            except heal.CallTimeout:
                # 还是没反应：换成这个 AI 的备用模型重试一次（备用模型用它自己所在中转站的地址和 Key）
                from . import backup
                fb_cfg = backup.config_for(agent, cfg)
                if not fb_cfg:
                    raise UserError("call_timeout", seconds=int(tmo))
                try:
                    resp = _run(fb_cfg)
                except heal.CallTimeout:
                    record(agent, fb_cfg, purpose + ":fallback:timeout", None, ok=False, task_id=task_id)
                    raise UserError("call_timeout", seconds=int(tmo))
                except UserError as e2:
                    record(agent, fb_cfg, purpose + ":fallback", None, ok=False, task_id=task_id,
                           refused=e2.key == "model_refused")
                    e2.fallback_done = True           # 已经用过备用模型：外层不要再记录 / 再换一次
                    raise
                record(agent, fb_cfg, purpose + ":fallback", resp, ok=True, task_id=task_id)
                resp.raw = fb_cfg
                return resp
    except UserError as e:
        if getattr(e, "fallback_done", False):
            raise
        if e.key == "model_refused":
            # 被拒绝：记录，换成这个 AI 的备用模型重试一次（PRD 2.4.1）。备用模型用它自己所在中转站的地址和 Key；
            # 备用也拒绝 / 出错时同样记录（拒答统计要准），再报错
            record(agent, cfg, purpose, None, ok=False, task_id=task_id, refused=True)
            from . import backup
            cfg2 = backup.config_for(agent, cfg)
            if cfg2 and (cfg2.model, cfg2.preset) != (cfg.model, cfg.preset):
                try:
                    resp = ADAPTERS[cfg2.provider].complete(cfg2, req)
                except UserError as e2:
                    record(agent, cfg2, purpose + ":fallback", None, ok=False, task_id=task_id,
                           refused=e2.key == "model_refused")
                    raise
                record(agent, cfg2, purpose + ":fallback", resp, ok=True, task_id=task_id)
                resp.raw = cfg2
                return resp
            raise
        record(agent, cfg, purpose, None, ok=False, task_id=task_id)
        # API Key 失效 / 欠费：紧急提醒（PRD 7.2），同一问题只提醒一次
        if e.key in KEY_PROBLEMS or (e.key == "rate_limited" and "credit" in str(e.params).lower()):
            alerts.raise_alert(f"llm_key:{agent}", "urgent", "llm_key", link="#/settings/models",
                               agent=f"@agent.{agent}", error=e.key, provider=e.params.get("provider", ""))
        raise
    resp.raw = cfg
    record(agent, cfg, purpose, resp, ok=True, task_id=task_id)
    alerts.resolve(f"llm_key:{agent}")
    return resp


def call_json(agent: str, purpose: str, system: str, user: str, schema: dict, **kw) -> tuple[dict, LLMResponse]:
    resp = call(agent, purpose, system, user, schema=schema, **kw)
    data = _parse_json(resp.text)
    if data is None:
        # 输出格式错：提醒一次再试；连续出错就换备用模型（PRD 9.0）
        resp = call(agent, purpose, system, user + "\n\nIMPORTANT: reply with ONE valid JSON object only.",
                    schema=schema, **kw)
        data = _parse_json(resp.text)
    if data is None:
        # 连续两次格式错：换成这个 AI 的备用模型（用它自己所在中转站的地址和 Key）
        from . import backup
        cfg2 = backup.config_for(agent, get_config(agent))
        if cfg2:
            req = LLMRequest(system=business_context() + system, messages=[{"role": "user", "content": user}],
                             purpose=purpose, json_schema=schema, context=kw.get("context") or {})
            try:
                resp = ADAPTERS[cfg2.provider].complete(cfg2, req)
                record(agent, cfg2, purpose + ":fallback", resp, ok=True, task_id=kw.get("task_id"))
                data = _parse_json(resp.text)
            except UserError as e2:
                record(agent, cfg2, purpose + ":fallback", None, ok=False, task_id=kw.get("task_id"),
                       refused=e2.key == "model_refused")
                data = None
    if not isinstance(data, dict):
        raise UserError("bad_json")
    return data, resp


def test_agent(agent: str) -> dict:
    cfg = get_config(agent)
    adapter = ADAPTERS.get(cfg.provider)
    if adapter is None:
        raise UserError("invalid_value", field="provider")
    started = time.time()
    info = adapter.test(cfg)
    if info.get("input_tokens"):
        record(agent, cfg, "key_test", LLMResponse(text="", model=info.get("model", cfg.model),
                                                   input_tokens=info["input_tokens"],
                                                   output_tokens=info.get("output_tokens", 0)))
    info["ms"] = int((time.time() - started) * 1000)
    return info


def list_models(agent: str, overrides: dict | None = None) -> list[str]:
    cfg = get_config(agent)
    for k, v in (overrides or {}).items():
        if v not in (None, "") and hasattr(cfg, k):
            setattr(cfg, k, v)
    return ADAPTERS[cfg.provider].list_models(cfg)

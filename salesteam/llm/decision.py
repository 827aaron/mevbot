"""判断模型（PRD 2.3）：统一的“选择题接口”。

    decide(content, options, instructions) -> Decision(probs={选项: 概率}, choice, confidence, escalate)

实现：
- openrouter : OpenRouter Decisions API（POST https://openrouter.ai/api/alpha/decisions，不是聊天接口），
               只用于 OpenRouter 上已知的决策模型（见 MODELS / presets.json decision_candidates）。请求/响应格式按 OpenRouter
               官方 API 参考（alphadecisions / submit-a-decisions-questions-and-answers-request）编写。
- llm        : 大模型兜底——用调用方智能体自己的大模型配置，以结构化 JSON 返回各选项概率
- mock       : 测试用，按规则给出概率
- none       : 不使用判断模型（调用方直接走大模型流程）

拿不准就升级：最高概率 < 阈值（默认 70%，可在设置里调）时 escalate=True，调用方必须交给大模型复核。
判断模型只用于“筛掉明显不符合的 / 分流”，不会单独决定一条线索合格（红线由程序把关）。
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .. import db, settings
from ..errors import UserError

OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
MODELS = {
    "openrouter": ["microsoft/microsoft-decision-1", "typesafe/jev-1.13"],
}
# 美元 / 百万 token（OpenRouter 响应里有 usage.cost 时以它为准）
PRICES = {"microsoft/microsoft-decision-1": (0.042, 0.0)}
# 不预设任何模型（PRD 2.3「不限定 Microsoft-Decision-1」）：任何 API、任何模型都可以，便宜的就行
DEFAULTS = {"provider": "none", "model": ""}
# “拿不准就升级”阈值：程序内部默认值，客户不需要设置（PRD 5.2.1 零配置）
THRESHOLD = 0.7
AGENT = "decider"   # 用量看板里单独一行


@dataclass
class Decision:
    probs: dict
    choice: str
    confidence: float
    escalate: bool
    by: str
    extra: dict = field(default_factory=dict)


def get_config() -> dict:
    cfg = settings.get("decision", {}) or {}
    return {**DEFAULTS, **cfg}


# ------------------------------------------------------------------ 配置：和其他 AI 一样（PRD 2.3，0.2.4 起）
# 判断模型 = 「设置 → AI 模型」表格里的第 6 行，用和其他 AI 同一个弹窗配：API（中转站）+ 模型。
# 存成 {"provider": "relay", "relay": <API id>, "model": <模型>}；没配置 = provider "none" = 不使用。
# 程序决定怎么调用：API 是 OpenRouter 且模型是决策模型 → Decisions API（用这个 API 的 Key）；其他模型 → 当普通大模型
# 用结构化输出答选择题。"mock" 只给开发 / 测试用；"openrouter"（单独的 Key）和 "llm" 是旧版设置，启动时迁移。

def decision_models() -> set[str]:
    from . import relay
    return {m.lower() for m in MODELS["openrouter"] + list(relay.presets().get("decision_candidates", []))}


def _is_openrouter(r: dict) -> bool:
    return r.get("preset") == "openrouter" or "openrouter.ai" in (r.get("base_url") or "").lower()


def uses_decisions_api(r: dict | None, model: str) -> bool:
    return bool(r) and _is_openrouter(r) and (model or "").lower() in decision_models()


def _relay_row(cfg: dict) -> dict | None:
    from . import relay
    rid = cfg.get("relay")
    return next((r for r in relay.relays() if r["id"] == rid), None) if rid else None


def configured(cfg: dict | None = None) -> bool:
    """配置了才使用（没配置 = 各 AI 直接用大模型）。"""
    cfg = cfg or get_config()
    prov = cfg.get("provider")
    if prov == "mock":
        return True
    if prov == "relay":
        return bool(cfg.get("model")) and _relay_row(cfg) is not None
    if prov == "openrouter":                     # 旧版设置（迁移前）
        return bool(settings.get_secret("openrouter_api_key"))
    return False


def cheapest_structured(relay_id: str) -> str:
    """规则挑模型：这个 API 上最便宜、支持结构化输出的模型（判断模型只做选择题，便宜够用即可）。"""
    from . import relay
    models = [m for m in relay.catalog(relay_id) if m.get("structured") is not False]
    if not models:
        raise UserError("relay_no_models")
    priced = [m for m in models if m.get("price_in") is not None]
    if priced:
        return min(priced, key=lambda m: (m["price_in"] + (m.get("price_out") or 0) / 4, m["id"]))["id"]
    return (relay.pick_cheap(models) or models[0])["id"]


def configure(relay_id: str, model: str = "") -> dict:
    """给判断模型配上某个 API 和模型（弹窗里「保存」/「和主管一样」/ 实测结果）。没填模型 → 这个 API 上最便宜、
    支持结构化输出的模型。OpenRouter 的决策模型（Decisions API）不在普通模型列表里，也照样接受。"""
    from . import relay
    r = relay.get_relay(relay_id)
    model = (model or "").strip()
    if model and not uses_decisions_api(r, model):
        # 任何模型都可以（PRD 2.3）：模型列表里有就用列表里的写法；没有（有的 API 不给列表）就照客户填的，
        # 能不能用由「测试」实际答一道选择题来验证
        try:
            cat = relay.catalog(relay_id)
        except UserError:
            cat = []
        want = relay.norm(model)
        hit = next((m for m in cat if m["id"] == model), None) or next((m for m in cat if m["norm"] == want), None)
        if hit:
            model = hit["id"]
    if not model:
        model = cheapest_structured(relay_id)
    cfg = get_config()
    cfg.update({"provider": "relay", "relay": relay_id, "model": model})
    cfg.pop("fallback_active", None)
    cfg.pop("fallback_reason", None)
    settings.put("decision", cfg)
    return public_config()


def disable() -> dict:
    cfg = get_config()
    cfg.update({"provider": "none"})
    cfg.pop("fallback_active", None)
    settings.put("decision", cfg)
    return public_config()


def migrate():
    """旧版设置迁移（启动时，只做一次，不联网）：
    - 单独的 OpenRouter Key + 决策模型 → 变成 API 列表里的一个 OpenRouter API（已经有同一个 Key 的 OpenRouter API 就直接用它，
      不重复添加；已有的 OpenRouter API 用的是别的 Key 时另加一个，不改动它）+ 判断模型绑定它
    - 大模型兜底（llm）/ 不使用 → 未配置
    - 中转站模型（relay + model）→ 原样保留"""
    done = list(settings.get("applied_defaults", []) or [])
    if "decision_as_agent" in done:
        return
    from . import relay
    cfg = get_config()
    prov = cfg.get("provider")
    if prov == "openrouter":
        key = settings.get_secret("openrouter_api_key")
        if key:
            rs = relay.relays()
            same = next((r for r in rs if _is_openrouter(r) and relay.relay_key(r["id"]) == key), None)
            if same:
                rid = same["id"]
            else:
                rid = "openrouter"
                n = 2
                while any(r["id"] == rid for r in rs):
                    rid = f"openrouter-{n}"
                    n += 1
                pre = next(p for p in relay.presets()["relay_presets"] if p["key"] == "openrouter")
                rs.append({"id": rid, "name": pre.get("name", "OpenRouter"), "kind": "openai",
                           "base_url": pre["base_url"], "preset": "openrouter", "decisions": True,
                           "added_at": time.time(), "listed": True, "manual_models": []})
                settings.put("relays", rs)
                settings.set_secret(f"relay_key:{rid}", key)
            cfg.update({"provider": "relay", "relay": rid, "model": cfg.get("model") or MODELS["openrouter"][0]})
        else:
            cfg["provider"] = "none"
    elif prov in ("llm", "none", None, ""):
        cfg["provider"] = "none"
    settings.put("decision", cfg)
    done.append("decision_as_agent")
    settings.put("applied_defaults", done)


def public_config() -> dict:
    """判断模型这一行（和其他 AI 的行同样的字段：是否配置、哪个 API、模型、怎么调用、今天的花费）。"""
    import sys
    from . import relay, spent
    cfg = get_config()
    r = _relay_row(cfg) if cfg.get("provider") == "relay" else None
    ok = configured(cfg)
    via = ""
    if ok and cfg["provider"] == "relay":
        via = "decisions_api" if uses_decisions_api(r, cfg.get("model", "")) else "chat"
    elif ok and cfg["provider"] == "openrouter":
        via = "decisions_api"
    eff, src = None, ""
    if via == "decisions_api":                   # 决策模型按次计价（OpenRouter 响应里带实际费用），这里只给参考
        p = PRICES.get(cfg.get("model", ""))
        eff, src = (list(p), "reference") if p else (None, "")
    elif ok and cfg["provider"] == "relay" and cfg.get("model"):
        from . import price_info
        from .base import AgentConfig
        p, src = price_info(AgentConfig(agent=AGENT, provider="relay", preset=cfg["relay"], model=cfg["model"]), cfg["model"])
        eff = list(p)
    return {**cfg, "agent": "decision", "configured": ok, "preset": cfg.get("relay", "") if r else "",
            "effective_price": eff, "price_src": src,
            "api_name": relay.display_name(r.get("preset", ""), r.get("base_url", "")) if r else "",
            "via": via, "usage": {"agent": AGENT, "spent": spent(AGENT)},
            # 安装包里不给客户选“测试用 mock”（会让所有筛选变成假的）；开发 / 测试环境保留
            "dev": not getattr(sys, "frozen", False)}


def update_config(data: dict) -> dict:
    """接口 / 实测结果写入：provider = relay（+ relay、model）/ none / mock（测试）。"""
    prov = data.get("provider", get_config().get("provider"))
    if prov not in ("none", "mock", "relay"):
        raise UserError("invalid_value", field="provider")
    if prov == "none":
        return disable()
    if prov == "relay":
        from . import relay
        rid = data.get("relay") or get_config().get("relay")
        if not rid or not any(r["id"] == rid for r in relay.relays()):
            raise UserError("relay_not_configured")
        return configure(rid, data.get("model") or "")
    cfg = get_config()
    cfg["provider"] = "mock"
    settings.put("decision", cfg)
    return public_config()


def enabled() -> bool:
    return configured()


def _record(model, inp, out, cost, ok=True, task_id=None, purpose="decision"):
    db.execute("INSERT INTO usage(agent, ts, day, month, provider, model, purpose, input_tokens, output_tokens, "
               "extra_units, cost, ok, task_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (AGENT, db.now(), time.strftime("%Y-%m-%d"), time.strftime("%Y-%m"), get_config()["provider"], model,
                purpose, inp, out, 0, cost, int(ok), task_id))


def _finish(probs: dict, by: str, threshold: float, extra=None) -> Decision:
    total = sum(max(0.0, float(v)) for v in probs.values()) or 1.0
    probs = {k: max(0.0, float(v)) / total for k, v in probs.items()}
    choice = max(probs, key=probs.get)
    conf = probs[choice]
    return Decision(probs=probs, choice=choice, confidence=conf, escalate=conf < threshold, by=by, extra=extra or {})


# ------------------------------------------------------------------ OpenRouter Decisions API

def _openrouter(content: str, options: dict, instructions: str, model: str, key: str, task_id=None) -> tuple[dict, dict]:
    if not key:
        raise UserError("decision_key_missing")
    body = {"model": model, "state": content[:100_000],
            "questions": {"q": {"type": "choice", "instructions": instructions, "criteria": options}}}
    req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8", "ignore")).get("error", {}).get("message", "")[:200]
        except ValueError:
            pass
        _record(model, 0, 0, 0, ok=False, task_id=task_id)
        if e.code in (401, 403):
            raise UserError("key_invalid", provider="OpenRouter")
        if e.code == 402:
            raise UserError("rate_limited", provider="OpenRouter")
        if e.code == 429:
            raise UserError("rate_limited", provider="OpenRouter")
        if e.code == 404:
            raise UserError("model_not_found", provider="OpenRouter")
        raise UserError("provider_error", provider="OpenRouter", code=e.code, detail=detail)
    except (urllib.error.URLError, OSError, ValueError):
        raise UserError("network_error", provider="OpenRouter")
    ans = (data.get("answers") or {}).get("q") or {}
    probs = ans.get("probabilities") or ({ans["choice"]: 1.0} if ans.get("choice") else {})
    usage = data.get("usage") or {}
    inp, out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    cost = usage.get("cost")
    if cost is None:
        p = PRICES.get(model, (0, 0))
        cost = inp / 1e6 * p[0] + out / 1e6 * p[1]
    _record(data.get("model") or model, inp, out, float(cost), task_id=task_id)
    if not probs:
        raise UserError("bad_json")
    return probs, {"model": data.get("model"), "confidence": ans.get("confidence")}


# ------------------------------------------------------------------ 大模型兜底

def _llm(agent: str, content: str, options: dict, instructions: str, task_id=None, context=None,
         relay_model: dict | None = None) -> dict:
    from . import call_json
    from ..agents.schemas import obj
    schema = obj({"probabilities": {"type": "array", "items": obj({"option": {"type": "string"},
                                                                    "probability": {"type": "number"}})}})
    from .. import prompts
    system = prompts.render("decision.fallback", options="\n".join(f"- {k}: {v}" for k, v in options.items()))
    user = f"Question: {instructions}\n\nContent:\n{content}"
    ctx = {**(context or {}), "options": options}
    if relay_model:
        # 中转站没有 Decisions API：用实测选出的最便宜合格模型答选择题
        from . import model_eval
        from .base import AgentConfig
        cfg = AgentConfig(agent=AGENT, provider="relay", preset=relay_model["relay"], model=relay_model["model"],
                          effort="low")
        data, _ = model_eval._call(cfg, "decision", system, user, schema, context=ctx, agent=AGENT)
        data = data or {}
    else:
        data, _ = call_json(agent, "decision", system, user, schema, task_id=task_id, context=ctx)
    probs = {}
    for it in data.get("probabilities") or []:
        if it.get("option") in options:
            probs[it["option"]] = float(it.get("probability") or 0)
    if not probs:
        raise UserError("bad_json")
    return probs


# ------------------------------------------------------------------ 测试用 mock

def _mock(options: dict, context: dict) -> dict:
    hint = (context or {}).get("mock_hint")
    keys = list(options)
    if hint in options:
        return {k: (0.62 if k == hint else 0.38 / max(1, len(keys) - 1)) for k in keys} if context.get("mock_unsure") \
            else {k: (0.9 if k == hint else 0.1 / max(1, len(keys) - 1)) for k in keys}
    return {k: 1 / len(keys) for k in keys}


# ------------------------------------------------------------------ 统一入口

def set_fallback(on: bool, reason: str = ""):
    """每周自检发现准确率明显下降 → 自动切换到备用（大模型兜底）；恢复后切回。"""
    cfg = get_config()
    cfg["fallback_active"] = bool(on)
    cfg["fallback_reason"] = reason
    cfg["fallback_at"] = time.time() if on else None
    settings.put("decision", cfg)


def decide(content: str, options: dict, instructions: str, agent: str = "analyst", task_id=None,
           context: dict | None = None, force_primary=False) -> Decision | None:
    """返回 Decision；判断模型未启用时返回 None（调用方直接走大模型流程）。"""
    cfg = get_config()
    prov = cfg["provider"]
    if not configured(cfg):
        return None                          # 没配置 = 不使用，调用方直接走大模型
    if cfg.get("fallback_active") and not force_primary:
        prov = "llm"      # 自检不合格期间用备用判断（自检本身仍测主判断模型，合格后自动切回）
    from . import check_budget, get_config as agent_cfg
    check_budget(agent, agent_cfg(agent))   # 总预算由程序把关
    if prov == "openrouter":
        probs, extra = _openrouter(content, options, instructions, cfg["model"],
                                   settings.get_secret("openrouter_api_key"), task_id)
        return _finish(probs, f"openrouter:{cfg['model']}", THRESHOLD, extra)
    if prov == "mock":
        _record("mock-decision", max(1, len(content) // 4), 1, 0.0, task_id=task_id)
        return _finish(_mock(options, context or {}), "mock", THRESHOLD)
    if prov == "relay":
        from . import relay as relay_mod
        r = _relay_row(cfg)
        if uses_decisions_api(r, cfg["model"]):      # OpenRouter 的决策模型：Decisions API，用这个 API 自己的 Key
            probs, extra = _openrouter(content, options, instructions, cfg["model"], relay_mod.relay_key(r["id"]),
                                       task_id)
            return _finish(probs, f"openrouter:{cfg['model']}", THRESHOLD, extra)
        return _finish(_llm(agent, content, options, instructions, task_id, context,
                            relay_model={"relay": cfg["relay"], "model": cfg["model"]}), f"relay:{cfg['model']}", THRESHOLD)
    return _finish(_llm(agent, content, options, instructions, task_id, context), f"llm:{agent}", THRESHOLD)


def test(data: dict | None = None) -> dict:
    data = data or {}
    if data:
        update_config(data)
    if not configured():
        raise UserError("decision_disabled")
    # 测试句子用客户模板里的产品（没有模板时用通用说法），不写死任何业务内容（PRD 2.4.2）
    from .. import db as _db, prompts
    r = _db.query_one("SELECT data FROM icp_templates ORDER BY updated_at DESC LIMIT 1")
    product = (_db.loads(r["data"], {}).get("product") or "").split("\n")[0][:80] if r else ""
    q, opts = prompts.options("decision.connection_test")
    # 用真正会用到的那条路（Decisions API 或普通模型答选择题）实际答一道选择题，并检查回答格式（PRD 2.3）
    try:
        d = decide(prompts.render("decision.connection_test_text", product=product or "your products"), opts, q,
                   context={"mock_hint": "buyer"}, force_primary=True)
    except UserError as e:
        if e.key == "bad_json":               # 回答不是合格的选择题格式（不是 JSON、没有给出选项的概率…）
            raise UserError("decision_bad_format")
        raise                                 # 网络 / Key 等：照原样显示服务商的说法
    if d is None or d.choice not in opts:
        raise UserError("decision_bad_format")
    return {"choice": d.choice, "confidence": round(d.confidence, 3), "by": d.by, "escalate": d.escalate}

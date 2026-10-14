"""每个 AI 都有一个备用模型（PRD 2.4.1）。备用模型主要用来应对“拒答”：主模型拒绝 / 超时时，换成这个 AI 的备用模型重试一次。

选备用的顺序（老板决定）：① 拒答最少 → ② 能力符合（结构化输出；调研员需要联网时优先能联网的）→ ③ 价格接近 →
④ 同等时优先不同厂商。
- 一键配置（实测）：在达到质量线的候选里选实测拒答最少的（model_eval.recommend），主模型仍是综合最好的。
- 客户手动选了主模型：按本机记录的各模型拒答率（usage 表 refused），没有数据时看 presets.json 的 low_refusal 提示，
  再对排名前 1–2 个候选做一次很小的拒答检查（2 个岗位测试题）+ 连通测试，选没有拒答的那个。
- 结果存 settings.model_fallbacks[agent] = {provider: relay, relay, model, auto}；设置页显示“备用：<模型>（自动）”，客户可以改。
"""
import math

from .. import db, settings
from ..errors import UserError
from .base import AgentConfig

CHECK_TESTS = {"monitor": ["classify", "extract"], "researcher": ["extract", "grade"], "analyst": ["grade", "extract"],
               "manager": ["grade", "extract"], "followup": ["classify", "grade"], "sales": ["extract", "grade"],
               "scout": ["classify", "extract"]}
def refusal_stats() -> dict:
    """本机记录的拒答率：{"model": {norm: (拒答, 调用)}, "vendor": {厂商: (拒答, 调用)}}（usage 表 refused）。"""
    from .relay import norm, vendor_of
    by_m: dict[str, list] = {}
    by_v: dict[str, list] = {}
    for r in db.query("SELECT model, SUM(refused) r, COUNT(*) n FROM usage WHERE model<>'' AND provider<>'mock' "
                      "GROUP BY model"):
        for d, k in ((by_m, norm(r["model"])), (by_v, vendor_of(r["model"]))):
            cur = d.setdefault(k, [0, 0])
            cur[0] += r["r"] or 0
            cur[1] += r["n"] or 0
    return {"model": {k: tuple(v) for k, v in by_m.items()}, "vendor": {k: tuple(v) for k, v in by_v.items()}}


def _low_refusal_hint(norm_id: str) -> bool:
    from .relay import presets
    return any(h and h in norm_id for h in presets().get("low_refusal", []))


def refusal_estimate(norm_id: str, vendor: str = "", stats: dict | None = None) -> float:
    """拒答率估计：本机这个模型的记录（≥3 次）→ presets.json 的 low_refusal 提示 → 本机同厂商的记录（≥5 次）→ 默认 0.2。"""
    stats = stats if stats is not None else refusal_stats()
    st = stats["model"].get(norm_id)
    if st and st[1] >= 3:
        return st[0] / st[1]
    if _low_refusal_hint(norm_id):
        return 0.05
    sv = stats["vendor"].get(vendor) if vendor else None
    if sv and sv[1] >= 5:
        return sv[0] / sv[1]
    return 0.2


def _price(m):
    if m.get("price_in") is None:
        return None
    return m["price_in"] + (m.get("price_out") or 0) / 4


def candidates(agent: str, primary_model: str, primary_price=None, primary_web=False) -> list[dict]:
    """按 拒答 → 能力 → 价格 → 不同厂商 排好序的候选（所有已配置中转站的模型目录）。"""
    from . import relay
    pn = relay.norm(primary_model)
    pv = relay.vendor_of(primary_model)
    needs_structured = agent != "sales"
    stats = refusal_stats()
    out = []
    for m in relay.all_models():
        if m["norm"] == pn or (needs_structured and m.get("structured") is False) or m.get("tools") is False and agent != "sales":
            continue
        cap = 0 if m.get("structured") else 1                      # 不确定支持结构化输出的排后面
        if agent == "researcher" and primary_web and not m.get("web"):
            cap += 1
        p = _price(m)
        dist = abs(math.log((p + 1e-6) / (primary_price + 1e-6))) if (p is not None and primary_price) else 3.0
        out.append({**m, "_key": (round(refusal_estimate(m["norm"], m.get("vendor", ""), stats), 2), cap, round(dist, 2),
                                  1 if (m.get("vendor") or relay.vendor_of(m["id"])) == pv else 0)})
    out.sort(key=lambda m: m["_key"])
    return out


def _refusal_check(agent: str, m: dict) -> bool:
    """很小的拒答检查：这个岗位的 2 个测试题 + 连通测试。返回是否通过（没有拒答、能连上）。"""
    from . import model_eval, relay
    try:
        relay.test(AgentConfig(agent="setup", provider="relay", preset=m["relay"], model=m["id"], effort="low"))
    except UserError:
        return False
    for t in CHECK_TESTS.get(agent, ["extract", "grade"])[:2]:
        r = model_eval._run_test({"relay": m["relay"], "id": m["id"]}, t, None)
        if r["refused"] or r["error"] in ("network_error", "relay_unreachable", "key_invalid", "model_not_found"):
            return False
    return True


def get(agent: str) -> dict | None:
    return (settings.get("model_fallbacks", {}) or {}).get(agent)


def _store(agent: str, fb: dict | None):
    fbs = dict(settings.get("model_fallbacks", {}) or {})
    if fb:
        fbs[agent] = fb
    else:
        fbs.pop(agent, None)
    settings.put("model_fallbacks", fbs)


def auto_assign(agent: str, check=True) -> dict | None:
    """客户手动选了主模型后（或某个 AI 还没有备用时）自动选备用模型。没有中转站 / 没有候选时返回 None。"""
    from . import get_config, price_for
    cfg = get_config(agent)
    if cfg.provider == "mock":
        return None
    try:
        pp = price_for(cfg, cfg.model)
    except Exception:  # noqa: BLE001
        pp = None
    cands = candidates(agent, cfg.model, (pp[0] + pp[1] / 4) if pp else None,
                       primary_web=cfg.provider == "claude" or (cfg.provider == "relay" and _is_web(cfg)))
    if not cands:
        _store(agent, None)
        return None
    pick = None
    for m in cands[:2] if check else cands[:1]:
        if not check or _refusal_check(agent, m):
            pick = m
            break
    if pick is None:
        pick = cands[0]                     # 前两个都没通过检查：仍然给一个备用（总比没有好），以后按本机统计再换
    fb = {"provider": "relay", "relay": pick["relay"], "model": pick["id"], "auto": True}
    _store(agent, fb)
    return fb


def _is_web(cfg) -> bool:
    from . import relay
    try:
        return any(m["id"] == cfg.model and m.get("web") for m in relay.catalog(cfg.preset))
    except UserError:
        return False


def set_manual(agent: str, relay_id: str, model: str) -> dict:
    """客户自己指定备用模型（先做连通测试）。"""
    from . import relay
    relay.get_relay(relay_id)
    relay.test(AgentConfig(agent="setup", provider="relay", preset=relay_id, model=model, effort="low"))
    fb = {"provider": "relay", "relay": relay_id, "model": model, "auto": False}
    _store(agent, fb)
    return fb


def ensure_all(check=False) -> dict:
    """每个 AI 都要有备用模型：还没有的自动补上（一键配置 / 中转站初始化之后调用）。"""
    from ..agents.registry import active_keys
    out = {}
    for a in active_keys():
        if not get(a):
            try:
                out[a] = auto_assign(a, check=check)
            except UserError:
                out[a] = None
    return out


def config_for(agent: str, primary: AgentConfig) -> AgentConfig | None:
    """备用模型的调用配置：用备用模型自己所在中转站的地址和 Key（不沿用主模型的 Key / 地址）。"""
    fb = get(agent)
    if not fb or not fb.get("model"):
        return None
    provider = fb.get("provider") or primary.provider
    if provider == "relay":
        from . import relay
        try:
            relay.get_relay(fb.get("relay") or primary.preset)
        except UserError:
            return None
        return AgentConfig(agent=agent, provider="relay", preset=fb.get("relay") or primary.preset, model=fb["model"],
                           api_key="", base_url="", effort=primary.effort)
    if provider == primary.provider:          # 同一家直连厂商：同一个账号的 Key / 地址
        return AgentConfig(agent=agent, provider=provider, preset=primary.preset, model=fb["model"],
                           api_key=primary.api_key, base_url=primary.base_url, effort=primary.effort)
    return None                               # 不同的直连厂商：没有它的 Key，不能用

"""中转站（PRD 2.4.1）：一个 Key 用遍各家模型。

- 内置预设（presets.json）：OpenRouter（推荐）、Vercel AI Gateway、Claude / OpenAI / DeepSeek / Gemini / 通义 / Kimi 官方，
  以及“自定义中转站”（地址 + Key）；测试用假中转站 mock_a / mock_b
- 自动识别：规范化地址（需要时补 /v1）；判断 OpenAI 兼容还是 Anthropic 兼容；列出模型；友好错误（地址错 / Key 错 / 余额不足）
- 模型 ID 统一：不同中转站对同一模型的命名不同（claude-opus-5-5 / anthropic/claude-opus-5.5），用 norm() 对齐
- 可配置多个中转站：选模型时一起比较；某个中转站故障时，同名模型自动切到另一个中转站（per-request failover）
- 智能体用 provider="relay"，preset=中转站 ID，model=该中转站上的模型 ID
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import alerts, settings, tls
from ..errors import UserError
from .base import AgentConfig, LLMRequest, LLMResponse

PRESETS_PATH = Path(__file__).with_name("presets.json")
_catalog_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()


def presets() -> dict:
    return json.loads(PRESETS_PATH.read_text("utf-8"))


def norm(model_id: str) -> str:
    """统一模型名：去掉厂商前缀、日期/后缀，点和下划线改成连字符，小写。"""
    s = (model_id or "").lower().strip()
    s = s.split(":")[0]
    s = s.lstrip("~")
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    s = re.sub(r"[._ ]+", "-", s)
    s = re.sub(r"-(20\d{6}|latest|preview)$", "", s)
    return s


NAME_VENDORS = (("claude", "anthropic"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
                ("chatgpt", "openai"), ("gemini", "google"), ("gemma", "google"), ("deepseek", "deepseek"),
                ("qwen", "qwen"), ("kimi", "moonshotai"), ("moonshot", "moonshotai"), ("glm", "z-ai"),
                ("llama", "meta-llama"), ("mistral", "mistralai"), ("grok", "x-ai"))


def vendor_of(model_id: str, owned_by: str = "") -> str:
    """模型厂商：优先用中转站自己的标识 —— OpenRouter 式 ID 前缀（anthropic/…）或 /models 里的 vendor / owned_by；
    都没有时才按模型名推断（claude → anthropic、gpt / o 系列 → openai、gemini → google …）。"""
    s = (model_id or "").lower().strip().lstrip("~")
    if "/" in s:
        return s.split("/", 1)[0]
    o = (owned_by or "").lower().strip()
    if o and o not in ("system", "openai-internal", "user", "organization-owner"):
        return o
    n = norm(s)
    for head, v in NAME_VENDORS:
        if n == head or n.startswith(head + "-") or (head in ("o1", "o3", "o4") and n.startswith(head)):
            return v
    return n.split("-")[0] if n else ""


# ------------------------------------------------------------------ 中转站配置

def relays() -> list[dict]:
    return list(settings.get("relays", []) or [])


def get_relay(relay_id: str) -> dict:
    r = next((x for x in relays() if x["id"] == relay_id), None)
    if not r:
        raise UserError("relay_not_found", 404)
    return r


def relay_key(relay_id: str) -> str:
    return settings.get_secret(f"relay_key:{relay_id}")


def public_relays():
    from .. import vault
    out = []
    for r in relays():
        k = relay_key(r["id"])
        out.append({**r, "key_set": bool(k), "key_hint": vault.mask(k)})
    return out


def normalize_base(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if not u:
        raise UserError("relay_bad_url")
    if u.startswith("mock://"):
        return u
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urllib.parse.urlparse(u)
    if not p.netloc or "." not in p.netloc and p.hostname not in ("localhost", "127.0.0.1"):
        raise UserError("relay_bad_url")
    return u


def _http(url, key, kind="openai", timeout=20):
    headers = {"Accept": "application/json", "User-Agent": "JamesAgent/0.2"}
    if kind == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls.context()) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:400]
        except Exception:  # noqa: BLE001
            pass
        return e.code, {"_error": body}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UserError("relay_unreachable", detail=str(e)[:120])


def display_name(preset: str = "", base_url: str = "") -> str:
    """给客户看的服务商名字：常用平台用 i18n（@relay.name.deepseek → DeepSeek 官方），自定义的用网址的主机名。"""
    if preset and preset != "custom" and any(p["key"] == preset for p in presets()["relay_presets"]):
        return f"@relay.name.{preset}"
    host = urllib.parse.urlparse(base_url or "").netloc or (base_url or "").replace("mock://", "")
    return host or "@relay.name.custom"


def _map_status(code, body="", name="") -> UserError:
    """中转站返回的错误 → 友好提示，并附上服务商的原文（打码、截短），方便判断是 Key、余额还是地址的问题。"""
    from ..vault import provider_said
    b = (body or "").lower()
    said = provider_said(body)
    name = name or "@relay.name.custom"
    if code in (401, 403):
        return UserError("key_invalid", provider=name, said=said)
    if code == 402 or "insufficient" in b or "balance" in b or "credit" in b:
        return UserError("relay_no_balance", provider=name, said=said)
    if code == 404:
        return UserError("relay_bad_url", provider=name, said=said)
    if code == 429:
        return UserError("rate_limited", provider=name, said=said)
    return UserError("relay_error", provider=name, code=code, detail=said, said=said)


def _post(url, key, kind, payload, timeout=40):
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "JamesAgent/0.2"}
    if kind == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls.context()) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")[:400]
        except Exception:  # noqa: BLE001
            return e.code, ""
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UserError("relay_unreachable", detail=str(e)[:120])


def _probe(kind, base, key, model) -> tuple[int, str]:
    """没有模型列表的中转站：用客户填的模型名发一个极小的请求，确认地址、Key、模型都能用。"""
    msg = [{"role": "user", "content": "ping"}]
    if kind == "anthropic":
        return _post(base + "/v1/messages", key, kind, {"model": model, "max_tokens": 5, "messages": msg})
    return _post(base + "/chat/completions", key, kind, {"model": model, "max_tokens": 5, "messages": msg})


def clean_models(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,，;；\n]+", v)
    return [x.strip() for x in dict.fromkeys(str(m).strip() for m in v or []) if x.strip()][:20]


def detect(base_url: str, key: str, kind_hint: str = "auto", models=None, name: str = "") -> dict:
    """识别中转站类型（OpenAI 兼容 / Anthropic 兼容）并验证 Key。返回 {kind, base_url, listed}。
    先读 /models；读不到时，如果客户填了模型名，就用第一个模型发一个极小的请求来确认（listed=False）。"""
    base = normalize_base(base_url)
    models = clean_models(models)
    if base.startswith("mock://"):
        from . import mock_relay
        try:
            mock_relay.check(base, key)
        except UserError as e:
            if "provider" in e.params:
                e.params["provider"] = name or base
            raise
        listed = bool(mock_relay.catalog(base))
        if not listed and not models:
            raise UserError("relay_models_needed")
        return {"kind": "mock", "base_url": base, "listed": listed}
    tries = []
    if kind_hint in ("auto", "openai"):
        tries += [("openai", base)]
        if not re.search(r"/v\d+(beta)?(/|$)", base):
            tries.append(("openai", base + "/v1"))
    if kind_hint in ("auto", "anthropic"):
        tries.append(("anthropic", base.removesuffix("/v1")))
    last = None
    for kind, b in tries:
        url = (b + "/v1/models") if kind == "anthropic" else (b + "/models")
        code, data = _http(url, key, kind)
        if code == 200 and isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
            return {"kind": kind, "base_url": b, "listed": True}
        last = (code, data.get("_error", "") if isinstance(data, dict) else "")
        if code in (401, 402, 403):
            raise _map_status(*last, name=name)
    if not models:
        # 地址能连上但没有模型列表：请客户手动填模型名（一个或多个）
        raise UserError("relay_models_needed")
    for kind, b in tries:
        code, body = _probe(kind, b, key, models[0])
        if code == 200:
            return {"kind": kind, "base_url": b, "listed": False}
        last = (code, body)
        if code in (401, 402, 403):
            raise _map_status(code, body, name=name)
    from ..vault import provider_said
    raise UserError("relay_model_failed", model=models[0], code=(last or (0, ""))[0], provider=name,
                    said=provider_said((last or (0, ""))[1]))


def add_relay(data: dict) -> dict:
    """添加中转站 / API（PRD 2.4.1：默认就是自定义 API —— 地址 + Key + 可选的模型名）。
    地址和某个常用平台一样时按那个平台处理（名字、接口类型）。"""
    preset = next((p for p in presets()["relay_presets"] if p["key"] == data.get("preset")), None)
    if (not preset or preset["key"] == "custom") and data.get("base_url"):
        u = str(data["base_url"]).strip().rstrip("/").lower()
        preset = next((p for p in presets()["relay_presets"] if p.get("base_url") and
                       p["base_url"].rstrip("/").lower() == u), preset)
    base = data.get("base_url") or (preset or {}).get("base_url") or ""
    from ..vault import clean_secret
    key = clean_secret(data.get("api_key"))
    if not key and not str(base).startswith("mock://"):
        raise UserError("relay_key_missing")
    # 协议由客户选（PRD 2.4.1）：OpenAI 协议（/models + /chat/completions）或 Claude 协议（/v1/models + /v1/messages），
    # 严格按所选协议连接、不自动识别；常用平台按钮会预先选好（Claude 官方 → Claude 协议，其他 → OpenAI 协议）
    proto = str(data.get("protocol") or "").lower()
    hint = {"openai": "openai", "claude": "anthropic", "anthropic": "anthropic"}.get(proto) or \
        (preset or {}).get("kind", "openai")
    if hint == "auto":
        hint = "openai"
    manual = clean_models(data.get("models") or data.get("model"))
    info = detect(base, key, hint, manual,
                  name=display_name((preset or {}).get("key", "custom"), base))
    rid = (preset["key"] if preset and preset["key"] != "custom" else "") or \
        re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlparse(info["base_url"]).netloc.lower() or "relay").strip("-")
    rs = [r for r in relays() if r["id"] != rid]
    rs.append({"id": rid, "name": data.get("name") or (preset or {}).get("name") or rid, "kind": info["kind"],
               "base_url": info["base_url"], "preset": (preset or {}).get("key", "custom"),
               "decisions": bool((preset or {}).get("decisions")), "added_at": time.time(),
               "listed": info.get("listed", True), "manual_models": manual})
    settings.put("relays", rs)
    settings.set_secret(f"relay_key:{rid}", key)
    _catalog_cache.pop(rid, None)
    if manual and not info.get("listed", True):
        # 客户手动填的模型：逐个做连通测试，只留能用的；一个都不能用就不添加
        ok = []
        for m in manual:
            try:
                test(AgentConfig(agent="setup", provider="relay", preset=rid, model=m, effort="low"))
                ok.append(m)
            except UserError:
                continue
        if not ok:
            remove_relay(rid)
            raise UserError("relay_model_failed", model=", ".join(manual), code="")
        rs = [dict(r, manual_models=ok) if r["id"] == rid else r for r in relays()]
        settings.put("relays", rs)
        _catalog_cache.pop(rid, None)
    return {"id": rid, **info, "preset": (preset or {}).get("key", "custom"), "models": len(catalog(rid))}


def remove_relay(relay_id: str):
    settings.put("relays", [r for r in relays() if r["id"] != relay_id])
    settings.set_secret(f"relay_key:{relay_id}", "")
    _catalog_cache.pop(relay_id, None)


# ------------------------------------------------------------------ 模型目录

def catalog(relay_id: str, refresh=False) -> list[dict]:
    with _lock:
        hit = _catalog_cache.get(relay_id)
        if hit and not refresh and time.time() - hit[0] < 3600:
            return hit[1]
    r = get_relay(relay_id)
    models = _fetch(r, relay_key(relay_id))
    for m in models:
        m["relay"] = relay_id
        m["norm"] = norm(m["id"])
        m["vendor"] = vendor_of(m["id"], m.get("owned_by", ""))
        # 价格：API 给了就用（api）；没给就用内置参考价（reference，PRD 2.4.1「单价未知要解决」）
        if m.get("price_in") is not None:
            m["price_src"] = "api"
        else:
            ref = reference_price(m["id"])
            if ref:
                m["price_in"], m["price_out"], m["price_src"] = ref[0], ref[1], "reference"
    with _lock:
        _catalog_cache[relay_id] = (time.time(), models)
    return models


def list_remote(data: dict) -> list[str]:
    """快速开始“给主管配置 API”：还没保存时先读一下模型列表，让客户从里面选主管用的模型。读不到返回空列表。"""
    from ..vault import clean_secret
    base = normalize_base(data.get("base_url") or "")
    proto = str(data.get("protocol") or "openai").lower()
    kind = "mock" if base.startswith("mock://") else ("anthropic" if proto in ("claude", "anthropic") else "openai")
    key = clean_secret(data.get("api_key"))
    try:
        info = detect(base, key, kind if kind != "mock" else "auto", None, name=display_name("", base))
    except UserError as e:
        if e.key == "relay_models_needed":
            return []
        raise
    r = {"kind": info["kind"], "base_url": info["base_url"], "listed": info.get("listed", True)}
    return sorted(m["id"] for m in _fetch(r, key))


def _fetch(r: dict, key: str) -> list[dict]:
    if not r.get("listed", True):
        # 没有模型列表的中转站：用客户手动填的模型名（价格未知 → 按 token 数控制预算，可在设置里填单价）
        models = [{"id": m, "name": m, "price_in": None, "price_out": None, "context": 0, "structured": None,
                   "tools": None, "web": None, "owned_by": "", "manual": True} for m in r.get("manual_models") or []]
    elif r["kind"] == "mock":
        from . import mock_relay
        models = mock_relay.catalog(r["base_url"])
    elif r["kind"] == "anthropic":
        code, data = _http(r["base_url"] + "/v1/models?limit=100", key, "anthropic")
        if code != 200:
            raise _map_status(code, data.get("_error", ""), name=display_name(r.get("preset", ""), r.get("base_url", "")))
        from .providers import CLAUDE_PRICES
        models = []
        for m in data.get("data", []):
            p = CLAUDE_PRICES.get(m["id"])
            models.append({"id": m["id"], "name": m.get("display_name", m["id"]), "price_in": p[0] if p else None,
                           "price_out": p[1] if p else None, "context": m.get("max_input_tokens") or 0,
                           "structured": True, "tools": True, "web": True, "owned_by": "anthropic"})
    else:
        code, data = _http(r["base_url"] + "/models", key, "openai", timeout=30)
        if code != 200:
            raise _map_status(code, data.get("_error", ""), name=display_name(r.get("preset", ""), r.get("base_url", "")))
        models = []
        for m in data.get("data", []):
            mid = m.get("id") or ""
            if not mid or mid.endswith(":batch") or mid.startswith("~"):
                continue
            arch = m.get("architecture") or {}
            if arch and "text" not in (arch.get("output_modalities") or ["text"]):
                continue
            pr = m.get("pricing") or {}
            try:
                pin = float(pr["prompt"]) * 1e6 if "prompt" in pr else None
                pout = float(pr["completion"]) * 1e6 if "completion" in pr else None
            except (TypeError, ValueError):
                pin = pout = None
            params = m.get("supported_parameters")
            models.append({"id": mid, "name": m.get("name", mid), "price_in": pin, "price_out": pout,
                           "owned_by": m.get("vendor") or m.get("owned_by") or "",
                           "context": m.get("context_length") or 0,
                           "structured": None if params is None else bool({"response_format", "structured_outputs"}
                                                                          & set(params)),
                           "tools": None if params is None else "tools" in params,
                           "web": None if params is None else "web_search_options" in params})
    return models


def all_models() -> list[dict]:
    out = []
    for r in relays():
        try:
            out += catalog(r["id"])
        except UserError:
            continue
    return out


def customer_price(relay_id: str, model_id: str):
    """客户自己在这个 AI 的弹窗里填的单价（美元 / 百万 token）；没填返回 None。"""
    manual = (settings.get("relay_prices", {}) or {}).get(f"{relay_id}::{model_id}")
    return (float(manual[0]), float(manual[1] if len(manual) > 1 else 0)) if manual else None


def reference_price(model_id: str):
    """内置参考价（presets.json reference_prices）：先按统一后的模型名精确匹配，再按家族（名字里的档位）匹配。
    返回 (输入价, 输出价, verified) 或 None。"""
    ref = presets().get("reference_prices") or {}
    n = norm(model_id)
    for e in ref.get("models") or []:
        if norm(e["match"]) == n:
            return (float(e["in"]), float(e["out"]), bool(e.get("verified", True)))
    low = (model_id or "").lower()
    for e in ref.get("families") or []:
        if re.search(e["match"], low) or re.search(e["match"], n):
            return (float(e["in"]), float(e["out"]), False)
    return None


def default_estimate() -> tuple[float, float]:
    """什么都查不到时的估算（故意偏高，让美元预算一定生效）。"""
    d = (presets().get("reference_prices") or {}).get("default_estimate") or {}
    return (float(d.get("in", 3.0)), float(d.get("out", 15.0)))


def price_of(relay_id: str, model_id: str):
    """价格顺序（PRD 2.4.1）：客户自己填的 > API 返回的 > 内置参考价；都没有返回 None（调用方再用估算）。"""
    mine = customer_price(relay_id, model_id)
    if mine:
        return mine
    try:
        cat = catalog(relay_id)
    except UserError:
        cat = []
    for m in cat:
        if m["id"] == model_id and m.get("price_in") is not None:
            return (m["price_in"], m["price_out"] or 0.0)
    ref = reference_price(model_id)
    return (ref[0], ref[1]) if ref else None


def price_source(relay_id: str, model_id: str) -> str:
    if customer_price(relay_id, model_id):
        return "customer"
    try:
        cat = catalog(relay_id)
    except UserError:
        cat = []
    m = next((x for x in cat if x["id"] == model_id), None)
    if m and m.get("price_in") is not None:
        return m.get("price_src") or "api"
    return "reference" if reference_price(model_id) else "estimate"


# ------------------------------------------------------------------ 按规则选“顾问模型”和“便宜模型”（PRD 2.4.1 / 3.0）

def pick_advisor(models: list[dict]) -> dict | None:
    pri = [norm(x) for x in presets()["advisor_priority"]]
    by_norm = {}
    for m in models:
        by_norm.setdefault(m["norm"], m)
    for p in pri:
        if p in by_norm:
            return by_norm[p]
    priced = [m for m in models if m["price_in"] is not None and m.get("tools") is not False]
    if priced:
        return max(priced, key=lambda m: (m["price_in"] + m["price_out"] / 4))
    return models[0] if models else None


def pick_cheap(models: list[dict]) -> dict | None:
    ok = [m for m in models if m.get("structured") is not False and m["price_in"] is not None and
          (m["price_in"] + (m["price_out"] or 0)) > 0 and (m.get("context") or 32000) >= 16000]
    if ok:
        return min(ok, key=lambda m: m["price_in"] + (m["price_out"] or 0) / 4)
    return None


def configure_agent(agent: str, relay_id: str, model: str = "") -> dict:
    """给一个 AI 配上某个中转站 / API 和模型（没填模型就按规则挑：主管挑强的，其他挑便宜够用的），并按规则选备用。"""
    from . import backup, update_config
    models = catalog(relay_id)
    if not models:
        raise UserError("relay_no_models")
    want = norm(model) if model else ""
    m = next((x for x in models if x["id"] == model), None) or next((x for x in models if want and x["norm"] == want),
                                                                    None)
    if not m:
        m = pick_advisor(models) if agent == "manager" else (pick_cheap(models) or pick_advisor(models))
    update_config(agent, {"provider": "relay", "preset": relay_id, "model": m["id"], "base_url": ""})
    if agent == "manager":
        st = settings.get("relay_state", {}) or {}
        st.update({"advisor": {"relay": relay_id, "id": m["id"]}, "bootstrapped_at": time.time()})
        settings.put("relay_state", st)
    try:
        backup.auto_assign(agent, check=False)
    except UserError:
        pass
    return {"agent": agent, "relay": relay_id, "model": m["id"], "norm": m["norm"]}


def bootstrap(manager_model: str = "") -> dict:
    """给主管配置好 API 后立即调用（PRD 3.0 / 2.4.1）：主管用客户选的模型（没选就按规则挑一个），主管就是“顾问”——
    带着快速开始往下走，同时在后台用同一个 API 给其他 AI 实测选模型和备用；实测完之前其他 AI 临时用便宜模型。"""
    from . import update_config
    models = all_models()
    if not models:
        raise UserError("relay_no_models")
    want = norm(manager_model) if manager_model else ""
    adv = next((m for m in models if m["id"] == manager_model), None) or \
        next((m for m in models if want and m["norm"] == want), None) or pick_advisor(models)
    cheap = pick_cheap(models) or adv
    st = settings.get("relay_state", {}) or {}
    st.update({"advisor": {"relay": adv["relay"], "id": adv["id"]}, "cheap": {"relay": cheap["relay"], "id": cheap["id"]},
               "provisional": True, "bootstrapped_at": time.time()})
    settings.put("relay_state", st)
    from ..agents.registry import AGENT_KEYS
    for agent in AGENT_KEYS:
        m = adv if agent == "manager" else cheap
        update_config(agent, {"provider": "relay", "preset": m["relay"], "model": m["id"], "base_url": ""})
    from . import backup
    backup.ensure_all(check=False)           # 每个 AI 都要有备用模型（PRD 2.4.1）
    return {"advisor": adv["norm"], "cheap": cheap["norm"], "models": len(models)}


def advisor_config() -> AgentConfig:
    st = settings.get("relay_state", {}) or {}
    a = st.get("advisor")
    if not a:
        raise UserError("relay_not_configured")
    return AgentConfig(agent="advisor", provider="relay", preset=a["relay"], model=a["id"], effort="medium")


# ------------------------------------------------------------------ 适配器（provider = "relay"）

def _cfg_for(cfg: AgentConfig, relay: dict) -> AgentConfig:
    key = relay_key(relay["id"])
    if relay["kind"] == "anthropic":
        return AgentConfig(agent=cfg.agent, provider="claude", model=cfg.model, api_key=key,
                           base_url=relay["base_url"], effort=cfg.effort)
    return AgentConfig(agent=cfg.agent, provider="openai_compat", preset="custom", model=cfg.model, api_key=key,
                       base_url=relay["base_url"], effort=cfg.effort)


def _do(relay, cfg, req):
    if relay["kind"] == "mock":
        from . import mock_relay
        return mock_relay.complete(relay, cfg, req)
    from . import claude, openai_compat
    rc = _cfg_for(cfg, relay)
    return (claude if relay["kind"] == "anthropic" else openai_compat).complete(rc, req)


def complete(cfg: AgentConfig, req: LLMRequest) -> LLMResponse:
    from .. import heal
    relay = get_relay(cfg.preset)
    try:
        try:
            resp = heal.call_with_timeout(lambda: _do(relay, cfg, req), heal.cfg()["call_timeout_s"])
        except heal.CallTimeout:
            raise UserError("relay_unreachable", detail="timeout")
        alerts.resolve(f"relay_down:{relay['id']}")
        return resp
    except UserError as e:
        if "provider" in e.params:          # 显示中转站自己的名字（不是 “relay” / “OpenAI-compatible”）
            e.params["provider"] = display_name(relay.get("preset", ""), relay.get("base_url", ""))
        if e.key not in ("network_error", "provider_server_error", "relay_unreachable"):
            raise
        # 中转站故障：找另一个中转站上的同名模型，自动切过去重试一次
        n = norm(cfg.model)
        for other in relays():
            if other["id"] == relay["id"]:
                continue
            try:
                alt = next((m for m in catalog(other["id"]) if m["norm"] == n), None)
            except UserError:
                alt = None
            if not alt:
                continue
            alerts.raise_alert(f"relay_down:{relay['id']}", "general", "relay_down", link="#/settings/models",
                               relay=relay["name"], other=other["name"])
            ocfg = AgentConfig(agent=cfg.agent, provider="relay", preset=other["id"], model=alt["id"], effort=cfg.effort)
            resp = _do(other, ocfg, req)
            resp.raw = {"failover": other["id"]}
            return resp
        raise


def test(cfg: AgentConfig) -> dict:
    relay = get_relay(cfg.preset)
    t = time.time()
    resp = complete(cfg, LLMRequest(system="Reply with JSON {\"ok\": true}.", messages=[{"role": "user", "content": "ping"}],
                                    purpose="key_test", json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                                     "required": ["ok"], "additionalProperties": False},
                                    max_tokens=200))
    return {"model": cfg.model, "relay": relay["name"], "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens, "ms": int((time.time() - t) * 1000)}


def list_models(cfg: AgentConfig) -> list[str]:
    return [m["id"] for m in catalog(cfg.preset)]

"""OpenAI 兼容接口（官方 openai SDK + base_url）：OpenAI、DeepSeek、通义千问、Kimi、Gemini、Ollama 本地模型。"""
import json
import re
import urllib.error
import urllib.request

import openai

from ..errors import UserError
from . import providers
from .base import AgentConfig, LLMRequest, LLMResponse

_THINK = re.compile(r"<think>.*?</think>", re.S)


def resolve_base_url(cfg: AgentConfig) -> str:
    if cfg.base_url:
        return cfg.base_url.strip()
    if cfg.provider == "gemini":
        return providers.PROVIDERS["gemini"]["base_url"]
    if cfg.provider == "ollama":
        return providers.PROVIDERS["ollama"]["base_url"]
    preset = providers.OPENAI_PRESETS.get(cfg.preset or "openai") or {}
    return preset.get("base_url", "")


def _label(cfg: AgentConfig) -> str:
    if cfg.provider == "gemini":
        return "Gemini"
    if cfg.provider == "ollama":
        return "Ollama"
    return {"openai": "OpenAI", "deepseek": "DeepSeek", "qwen": "Qwen", "qwen_intl": "Qwen",
            "kimi": "Kimi", "kimi_cn": "Kimi"}.get(cfg.preset, "OpenAI-compatible")


def _client(cfg: AgentConfig) -> openai.OpenAI:
    base_url = resolve_base_url(cfg)
    if not base_url:
        raise UserError("base_url_missing", agent=f"@agent.{cfg.agent}")
    if cfg.provider != "ollama" and not cfg.api_key:
        raise UserError("key_missing", agent=f"@agent.{cfg.agent}")
    return openai.OpenAI(api_key=cfg.api_key or "ollama", base_url=base_url, max_retries=2, timeout=600)


def _said(e) -> str:
    """服务商返回的原文（打码、截短），附在友好提示后面。"""
    from ..vault import provider_said
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        import json
        body = json.dumps(body)
    return provider_said(body or getattr(e, "message", "") or str(e))


def _translate(cfg: AgentConfig, e: Exception) -> UserError:
    p = _label(cfg)
    if isinstance(e, openai.AuthenticationError):
        return UserError("key_invalid", said=_said(e), provider=p)
    if isinstance(e, openai.PermissionDeniedError):
        return UserError("key_no_permission", said=_said(e), provider=p, detail=getattr(e, "message", "")[:300])
    if isinstance(e, openai.NotFoundError):
        return UserError("model_not_found", said=_said(e), provider=p)
    if isinstance(e, openai.RateLimitError):
        return UserError("rate_limited", said=_said(e), provider=p)
    if isinstance(e, openai.BadRequestError):
        m = (getattr(e, "message", "") or str(e)).lower()
        if "api key" in m or "api_key" in m or "apikey" in m:
            return UserError("key_invalid", said=_said(e), provider=p)
        return UserError("provider_bad_request", said=_said(e), provider=p, detail=getattr(e, "message", "")[:300])
    if isinstance(e, openai.APIStatusError):
        if e.status_code >= 500:
            return UserError("provider_server_error", said=_said(e), provider=p, code=e.status_code)
        return UserError("provider_error", said=_said(e), provider=p, code=e.status_code, detail=getattr(e, "message", "")[:300])
    if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError)):
        if cfg.provider == "ollama":
            return UserError("ollama_unreachable")
        return UserError("network_error", provider=p)
    return UserError("provider_error", said=_said(e), provider=p, code="", detail=str(e)[:300])


def complete(cfg: AgentConfig, req: LLMRequest) -> LLMResponse:
    client = _client(cfg)
    if not cfg.model:
        raise UserError("model_missing", agent=f"@agent.{cfg.agent}")
    system = req.system
    if req.json_schema:
        system += ("\n\nReturn ONLY one valid JSON object (no markdown fences, no commentary) that matches this "
                   "JSON Schema:\n" + json.dumps(req.json_schema, ensure_ascii=False))
    messages = [{"role": "system", "content": system}] + [dict(m) for m in req.messages]
    kwargs = dict(model=cfg.model, messages=messages)
    if req.json_schema:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        try:
            resp = client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            # 有些厂商/模型不支持 response_format，去掉再试一次
            msg = str(e).lower()
            if "response_format" in kwargs and ("response_format" in msg or "json" in msg):
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            else:
                raise
    except UserError:
        raise
    except openai.OpenAIError as e:
        raise _translate(cfg, e) from e

    choice = resp.choices[0] if resp.choices else None
    # 拒答：content_filter 结束原因，或模型返回 refusal 字段
    if choice is not None and (getattr(choice, "finish_reason", "") == "content_filter" or
                               getattr(choice.message, "refusal", None)):
        raise UserError("model_refused", category=getattr(choice, "finish_reason", "") or "refusal")
    text = (choice.message.content or "") if choice else ""
    text = _THINK.sub("", text).strip()
    usage = getattr(resp, "usage", None)
    return LLMResponse(
        text=text,
        model=getattr(resp, "model", cfg.model) or cfg.model,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def _ollama_tags(cfg: AgentConfig) -> list[str]:
    host = resolve_base_url(cfg).rstrip("/")
    if host.endswith("/v1"):
        host = host[:-3]
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        raise UserError("ollama_unreachable")


def list_models(cfg: AgentConfig) -> list[str]:
    if cfg.provider == "ollama":
        return _ollama_tags(cfg)
    client = _client(cfg)
    try:
        ids = [m.id for m in client.models.list()]
    except openai.OpenAIError as e:
        raise _translate(cfg, e) from e
    return [i.removeprefix("models/") for i in ids]


def test(cfg: AgentConfig) -> dict:
    if cfg.provider == "ollama":
        names = _ollama_tags(cfg)
        if cfg.model and cfg.model not in names and f"{cfg.model}:latest" not in names:
            raise UserError("ollama_model_missing", model=cfg.model)
        return {"model": cfg.model, "models": names}
    if not cfg.model:
        raise UserError("model_missing", agent=f"@agent.{cfg.agent}")
    # 用一次极小的对话请求验证 Key + 模型（有些厂商不支持 /models 接口）
    client = _client(cfg)
    try:
        resp = client.chat.completions.create(model=cfg.model, max_tokens=5,
                                              messages=[{"role": "user", "content": "Reply with OK"}])
    except openai.OpenAIError as e:
        raise _translate(cfg, e) from e
    usage = getattr(resp, "usage", None)
    return {"model": getattr(resp, "model", cfg.model), "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0}

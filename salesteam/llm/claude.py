"""Claude（官方 anthropic SDK）。

遵循 claude-api 规范：默认 claude-opus-5-5、adaptive thinking、output_config.effort、
流式 + get_final_message、服务端 web_search/web_fetch、pause_turn 续跑、refusal 处理、
服务端 fallbacks="default"（安全分类器误拒时由服务端换模型重试）。
"""
import anthropic

from ..errors import UserError
from . import providers
from .base import AgentConfig, LLMRequest, LLMResponse

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5


def _client(cfg: AgentConfig) -> anthropic.Anthropic:
    if not cfg.api_key:
        raise UserError("key_missing", agent=f"@agent.{cfg.agent}")
    kw = {"base_url": cfg.base_url} if cfg.base_url else {}   # Anthropic 兼容的中转站
    return anthropic.Anthropic(api_key=cfg.api_key, max_retries=2, timeout=900, **kw)


def _said(e) -> str:
    """服务商返回的原文（打码、截短），附在友好提示后面。"""
    from ..vault import provider_said
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        import json
        body = json.dumps(body)
    return provider_said(body or getattr(e, "message", "") or str(e))


def _translate(e: Exception) -> UserError:
    if isinstance(e, anthropic.AuthenticationError):
        return UserError("key_invalid", said=_said(e), provider="Claude")
    if isinstance(e, anthropic.PermissionDeniedError):
        return UserError("key_no_permission", said=_said(e), provider="Claude", detail=getattr(e, "message", ""))
    if isinstance(e, anthropic.NotFoundError):
        return UserError("model_not_found", said=_said(e), provider="Claude")
    if isinstance(e, anthropic.RateLimitError):
        return UserError("rate_limited", said=_said(e), provider="Claude")
    if isinstance(e, anthropic.BadRequestError):
        return UserError("provider_bad_request", said=_said(e), provider="Claude", detail=getattr(e, "message", "")[:300])
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code >= 500:
            return UserError("provider_server_error", said=_said(e), provider="Claude", code=e.status_code)
        return UserError("provider_error", said=_said(e), provider="Claude", code=e.status_code,
                         detail=getattr(e, "message", "")[:300])
    if isinstance(e, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return UserError("network_error", provider="Claude")
    return UserError("provider_error", said=_said(e), provider="Claude", code="", detail=str(e)[:300])


def _web_tools(model: str):
    if model in providers.CLAUDE_WEB_TOOLS_V2:
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6}]
    return [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
            {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 6}]


def complete(cfg: AgentConfig, req: LLMRequest) -> LLMResponse:
    client = _client(cfg)
    model = cfg.model or providers.PROVIDERS["claude"]["default_model"]
    output_config = {"effort": cfg.effort or "medium"}
    # 结构化输出：没有联网工具时直接用 JSON Schema 约束；联网时由提示词要求 JSON，再解析
    if req.json_schema and not req.web_search:
        output_config["format"] = {"type": "json_schema", "schema": req.json_schema}
    base = dict(
        model=model,
        max_tokens=req.max_tokens or 32000,
        # 提示缓存：system = 岗位说明书 + 模板（同一任务内不变），放在缓存断点之前；变化的线索材料放在 user 消息里
        system=([{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
                if req.cache_system else req.system),
        thinking={"type": "adaptive"},
        output_config=output_config,
    )
    if model in providers.CLAUDE_FALLBACK_MODELS:
        base["betas"] = [FALLBACK_BETA]
        base["fallbacks"] = "default"
    if req.web_search:
        base["tools"] = _web_tools(model)

    messages = [dict(m) for m in req.messages]
    assistant_blocks: list = []
    texts: list[str] = []
    out = LLMResponse(text="", model=model)
    seen_urls = set()
    status = req.on_status or (lambda k, p: None)

    try:
        for _ in range(MAX_CONTINUATIONS + 1):
            msgs = messages + ([{"role": "assistant", "content": assistant_blocks}] if assistant_blocks else [])
            with client.beta.messages.stream(messages=msgs, **base) as stream:
                for event in stream:
                    if event.type == "content_block_stop":
                        block = getattr(event, "content_block", None)
                        if block is not None and block.type == "server_tool_use":
                            inp = block.input if isinstance(block.input, dict) else {}
                            if block.name == "web_search":
                                status("ev.web_searching", {"query": inp.get("query", "")})
                            elif block.name == "web_fetch":
                                status("ev.web_reading", {"url": inp.get("url", "")})
                msg = stream.get_final_message()

            u = msg.usage
            cw = getattr(u, "cache_creation_input_tokens", 0) or 0
            cr = getattr(u, "cache_read_input_tokens", 0) or 0
            out.input_tokens += (u.input_tokens or 0) + cw + cr
            out.cache_write_tokens += cw
            out.cache_read_tokens += cr
            out.output_tokens += u.output_tokens or 0
            stu = getattr(u, "server_tool_use", None)
            if stu is not None:
                out.web_searches += getattr(stu, "web_search_requests", 0) or 0
            out.model = getattr(msg, "model", model) or model

            for b in msg.content:
                if b.type == "text":
                    texts.append(b.text)
                elif b.type == "web_search_tool_result":
                    content = b.content
                    if isinstance(content, list):   # 成功是列表；出错是对象
                        for r in content:
                            url = getattr(r, "url", "")
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                out.sources.append({"url": url, "title": getattr(r, "title", "")})
                elif b.type == "web_fetch_tool_result":
                    url = getattr(getattr(b, "content", None), "url", "") or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        out.sources.append({"url": url, "title": ""})

            if msg.stop_reason == "refusal":
                details = getattr(msg, "stop_details", None)
                raise UserError("model_refused", category=getattr(details, "category", "") or "")
            if msg.stop_reason == "pause_turn":
                # 服务端联网循环暂停：带上已生成内容再请求一次，服务端会接着做（不要额外加 "Continue"）
                assistant_blocks = assistant_blocks + list(msg.content)
                continue
            if msg.stop_reason == "max_tokens" and req.json_schema:
                raise UserError("output_truncated")
            break
    except UserError:
        raise
    except anthropic.APIError as e:
        raise _translate(e) from e

    out.text = "".join(texts).strip()
    return out


def test(cfg: AgentConfig) -> dict:
    """一键测试：读取模型信息（不消耗 token），能验证 Key 有效且模型存在。"""
    client = _client(cfg)
    try:
        m = client.models.retrieve(cfg.model or providers.PROVIDERS["claude"]["default_model"])
        return {"model": m.id, "name": getattr(m, "display_name", m.id)}
    except anthropic.APIError as e:
        raise _translate(e) from e


def list_models(cfg: AgentConfig) -> list[str]:
    client = _client(cfg)
    try:
        return [m.id for m in client.models.list(limit=100)]
    except anthropic.APIError as e:
        raise _translate(e) from e

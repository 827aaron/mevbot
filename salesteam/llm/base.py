"""厂商适配器的统一请求 / 响应结构。"""
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentConfig:
    agent: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    preset: str = ""
    effort: str = "medium"
    alloc_daily: float | None = None     # 第 3 步销售主管在总预算内的分配（客户不填）
    alloc_monthly: float | None = None
    price_in: float | None = None
    price_out: float | None = None


@dataclass
class LLMRequest:
    system: str
    messages: list                     # [{"role": "user"|"assistant", "content": str}]
    purpose: str = ""                  # 用途标签：icp_generate / extract_leads / score_lead ...（mock 据此返回假数据）
    json_schema: dict | None = None    # 需要结构化 JSON 输出时提供
    web_search: bool = False           # 仅 Claude：开启服务端联网搜索
    max_tokens: int = 32000
    # system 里放“岗位说明书 + 模板”等不变内容；True 时对 Claude 开启提示缓存（其他厂商多为自动缓存）
    cache_system: bool = True
    context: dict = field(default_factory=dict)   # 给 mock 用的结构化上下文
    on_status: Callable[[str, dict], None] | None = None   # 进度回调 (i18n key, params)


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    cache_read_tokens: int = 0       # 已包含在 input_tokens 里；按 0.1 倍价格计费
    cache_write_tokens: int = 0      # 已包含在 input_tokens 里；按 1.25 倍价格计费
    sources: list = field(default_factory=list)   # 联网搜索看过的网页 [{url, title}]
    raw: Any = None

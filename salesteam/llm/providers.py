"""模型厂商目录：界面下拉框、默认地址、建议模型、内置价格表都来自这里。

价格单位：美元 / 百万 token（估算用）。只内置了我们确认过的 Claude 价格；
其他厂商价格变化快，由客户在设置里自己填写单价（不填则费用显示“未知”，预算只按已知费用计）。
"""

CLAUDE_MODELS = [
    # (模型 ID, 输入价, 输出价)
    ("claude-opus-5-5", 4.00, 20.00),
    ("claude-sonnet-5-5", 2.00, 10.00),
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-fable-5-1", 10.00, 50.00),
    ("claude-opus-5", 5.00, 25.00),
    ("claude-sonnet-5", 2.00, 10.00),
]
CLAUDE_PRICES = {m: (i, o) for m, i, o in CLAUDE_MODELS}
CLAUDE_WEB_SEARCH_PER_1K = 10.00  # Claude 服务端联网搜索：每 1000 次搜索的费用

# 服务端 refusal 自动换模型（fallbacks: "default"）支持的模型
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
# 支持带动态过滤的新版 web_search / web_fetch 工具的模型；其他模型用基础版
CLAUDE_WEB_TOOLS_V2 = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5"}

# OpenAI 兼容接口的预设（base_url + 建议模型；模型可在界面里“获取模型列表”或手动填写）
OPENAI_PRESETS = {
    "openai": {"base_url": "https://api.openai.com/v1", "models": ["gpt-5", "gpt-5-mini"]},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"]},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "models": ["qwen-max", "qwen-plus", "qwen-turbo"]},
    "qwen_intl": {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                  "models": ["qwen-max", "qwen-plus", "qwen-turbo"]},
    "kimi": {"base_url": "https://api.moonshot.ai/v1", "models": ["kimi-latest", "moonshot-v1-32k"]},
    "kimi_cn": {"base_url": "https://api.moonshot.cn/v1", "models": ["kimi-latest", "moonshot-v1-32k"]},
    "custom": {"base_url": "", "models": []},
}

PROVIDERS = {
    "claude": {"needs_key": True, "web_search": True, "models": [m for m, _, _ in CLAUDE_MODELS],
               "default_model": "claude-opus-5-5", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    "openai_compat": {"needs_key": True, "web_search": False, "presets": OPENAI_PRESETS,
                      "default_preset": "openai"},
    "gemini": {"needs_key": True, "web_search": False,
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
    "ollama": {"needs_key": False, "web_search": False, "base_url": "http://127.0.0.1:11434/v1", "models": []},
    "relay": {"needs_key": False, "web_search": False, "models": []},   # 中转站（Key 在中转站配置里）
    "mock": {"needs_key": False, "web_search": False, "models": ["mock-model"], "test_only": True},
}

MOCK_PRICE = (1.00, 2.00)  # 测试用假价格，便于验证用量看板和预算功能


def public_catalog():
    return PROVIDERS

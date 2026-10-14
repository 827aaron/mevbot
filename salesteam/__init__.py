"""本地多智能体销售团队 —— Python 核心包（产品名见 APP_NAME）。

模块划分：
- paths / db / secrets / settings : 基础设施（路径、SQLite、本地加密、设置）
- llm/       : 大模型厂商适配（Claude / OpenAI 兼容 / Gemini / Ollama / mock）+ 用量与预算
- sources/   : 数据源（搜索、Scrapling 爬虫、YouTube；第 2/4 步加 TG / FB / Ins / 海关）
- agents/    : 6 个智能体（第 1 步实现 调研员 + 数据师 + ICP 生成）
- tasks      : 任务运行器 + 实时事件（SSE）
- api/       : HTTP 接口（FastAPI，只监听 127.0.0.1）
"""

VERSION = "0.3.2"

# Runtime selection also works with an embedded edition.json in frozen builds.
from . import edition
_EDITION = edition.current()
APP_NAME = _EDITION["name"]
APP_TAGLINE = _EDITION["tagline"]

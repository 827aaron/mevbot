"""本地多智能体销售团队 —— Python 核心包（产品名见 APP_NAME）。

模块划分：
- paths / db / secrets / settings : 基础设施（路径、SQLite、本地加密、设置）
- llm/       : 大模型厂商适配（Claude / OpenAI 兼容 / Gemini / Ollama / mock）+ 用量与预算
- sources/   : 数据源（搜索、Scrapling 爬虫、YouTube；第 2/4 步加 TG / FB / Ins / 海关）
- agents/    : 6 个智能体（第 1 步实现 调研员 + 数据师 + ICP 生成）
- tasks      : 任务运行器 + 实时事件（SSE）
- api/       : HTTP 接口（FastAPI，只监听 127.0.0.1）
"""

VERSION = "0.3.0"

# 产品名（暂定，只在这里和 web/i18n/*.json 的 app.name / app.tagline 里出现）
APP_NAME = {"zh": "詹姆斯智能体", "en": "James Agent"}
APP_TAGLINE = {"zh": "本地多智能体销售团队", "en": "Your local AI sales team"}

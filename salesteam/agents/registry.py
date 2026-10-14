"""智能体的元信息。名称/职责文案在前端 i18n（agent.<key> / agent_desc.<key>）。

v1 团队 = 5 个 AI（范围决定 #5）：👔 销售主管 · 🧭 渠道拓展员 · 🔍 调研员 · 👁️ 监控员 · 🧩 数据师。
step = 在第几步完整上线。渠道拓展员第 3 步完整上线；第 2 步由“AI 找群”作业驱动（找群、预览、打分，不加群）。
"""

AGENTS = [
    {"key": "manager", "icon": "👔", "step": 3},     # 销售主管：第 1 步负责“AI 生成理想客户模板”
    {"key": "scout", "icon": "🧭", "step": 3},       # 渠道拓展员（新）：找渠道（TG 群 / 频道，以后 FB 群、论坛、名录…）
    {"key": "researcher", "icon": "🔍", "step": 1},
    {"key": "monitor", "icon": "👁️", "step": 2},
    {"key": "analyst", "icon": "🧩", "step": 1},
    {"key": "sales", "icon": "✉️", "step": 3},
    {"key": "followup", "icon": "🔁", "step": 3},
]
AGENT_KEYS = [a["key"] for a in AGENTS]

# v1 只做“找客户”（PRD §1 阶段表）：销售员 / 跟进员不上线，审核通过的线索晚上通过 TG 推送（Excel）交给客户自己的销售。
# 注册信息、模型配置和代码钩子都保留；以后把 FEATURES["outreach"] 改成 True 即可重新出现在界面和选模型里。
FEATURES = {"outreach": False,
            # v1 没有人工审核环节（PRD §1）：合格线索通过程序硬检查后直接进晚上的推送。审核台代码保留，
            # 以后销售员上线时可能作为“发送前确认”回来
            "review": False}
OUTREACH_AGENTS = {"sales", "followup"}


def is_active(key: str) -> bool:
    return FEATURES["outreach"] or key not in OUTREACH_AGENTS


def active_agents() -> list[dict]:
    return [a for a in AGENTS if is_active(a["key"])]


def active_keys() -> list[str]:
    return [a["key"] for a in active_agents()]

# 默认模型（PRD 2.2：监控员用便宜快速模型；销售员、跟进员、销售主管用强模型）
_STRONG = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium"}
DEFAULTS = {
    "manager": dict(_STRONG),
    "researcher": dict(_STRONG),
    "monitor": {**_STRONG, "model": "claude-haiku-5-5", "effort": "low"},
    "scout": {**_STRONG, "model": "claude-haiku-5-5", "effort": "low"},
    "analyst": dict(_STRONG),
    "sales": {**_STRONG, "effort": "high"},
    "followup": dict(_STRONG),
}

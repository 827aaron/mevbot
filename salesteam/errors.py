"""给用户看的错误：只带一个 i18n key + 参数，由前端翻译成中/英文友好提示。"""


class UserError(Exception):
    """可读错误。key 对应前端 i18n 的 errors.<key>。"""

    def __init__(self, key: str, status: int = 400, **params):
        super().__init__(key)
        self.key = key
        self.status = status
        self.params = {k: ("" if v is None else str(v)) for k, v in params.items()}

    def to_dict(self):
        return {"error": self.key, "params": self.params}


class BudgetExceeded(UserError):
    """总预算（或第 3 步销售主管分配的智能体额度）用完：全部智能体暂停。"""

    def __init__(self, period: str, used: float, limit: float, agent: str = "", tokens: bool = False):
        if tokens:      # 价格未知的模型按 token 数控制预算：显示 token 数，不显示 $0.00
            super().__init__("budget_tokens_exceeded", 402, period=f"@period.{period}",
                             used=f"{int(used):,}", limit=f"{int(limit):,}", agent="")
        else:
            super().__init__("budget_exceeded" if not agent else "budget_alloc_exceeded", 402,
                             period=f"@period.{period}", used=f"{used:.2f}", limit=f"{limit:.2f}",
                             agent=f"@agent.{agent}" if agent else "")
        self.agent = agent


class Cancelled(Exception):
    """任务被用户取消。"""

"""智能体之间传递数据的 JSON Schema（同时用于 Claude 结构化输出约束、其他厂商的提示词、mock 假数据）。

为兼容 Claude 结构化输出：每个对象都写 additionalProperties: false，并把所有字段列为 required。
"""


def obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props.keys()), "additionalProperties": False}


S = {"type": "string"}
SL = {"type": "array", "items": {"type": "string"}}
B = {"type": "boolean"}
I = {"type": "integer"}

ICP = obj({
    "name": S,                       # 模板名称
    "product": S,                    # 我方产品 / 服务介绍（必填）
    "countries": SL,                 # 目标国家两位代码（必填）
    "customer_types": SL,            # 目标客户类型（必填）
    "industries": SL,
    "company_size": S,               # 员工数 / 营收 / 门店数 的描述
    # 线索词（PRD §3）：中 / 英 / 马来文，由 AI 根据客户资料生成，客户可改
    "buy_words": SL,                 # 求购词：要买的说法
    "product_words": SL,             # 产品词：产品名和别名
    "exclude_words": SL,             # 排除词：出现就不要
    "exclude_companies": SL,         # 同行、已合作客户（名称或网址）
    "reference_customers": SL,       # 已成交客户网址（找相似客户）
    "contact_titles": SL,
    "relax_policy": obj({
        "allow_neighbor_countries": B,
        "allow_change_customer_type": B,
        "allow_more_industries": B,
        "notes": S,
    }),
    "qualified_grades": SL,          # 默认 ["A","B"]
    "order_size": S,                 # 一般订单大小
    "purchase_scenarios": SL,        # 采购场景
    # 由 AI 根据客户自己的资料生成（不再写死在程序里，PRD 2.4.2）：
    "grading_criteria": {"type": "array", "items": obj({"name": S, "weight": I, "description": S})},  # 打分标准
})

ICP_GENERATE = obj({
    "icp": ICP,
    "reasons": {"type": "array", "items": obj({"field": S, "reason": S})},
    "summary": S,
})

ICP_CHAT = obj({
    "icp": ICP,
    "reply": S,
    "changes": SL,
})

ICP_DERIVE = obj({
    "buy_words": SL,
    "product_words": SL,
    "grading_criteria": {"type": "array", "items": obj({"name": S, "weight": I, "description": S})},
})

KEYWORDS = obj({
    "by_lang": {"type": "array", "items": obj({"lang": S, "keywords": SL})},
})

CANDIDATE = obj({
    "company_name": S,
    "website": S,
    "country": S,                    # 两位代码，未知留空
    "customer_type": S,
    "industry": S,
    "emails": SL,
    "telegram": SL,
    "whatsapp": SL,                  # 只填页面上明确标注为 WhatsApp 的号码 / wa.me 链接
    "phones": SL,
    "people": {"type": "array", "items": obj({"name": S, "title": S, "email": S})},
    "description": S,
    "buying_signal": S,              # 如“评论区主动求购”，没有留空
    "evidence_quote": S,             # 原文摘录（证明这家公司/这个人的信息来自来源）
    "source_url": S,
})

CANDIDATES = obj({
    "candidates": {"type": "array", "items": CANDIDATE},
})

SOURCE = obj({"url": S, "title": S, "quote": S})

SCORE = obj({
    "grade": {"type": "string", "enum": ["A", "B", "C", "D"]},
    "score": I,                      # 0-100
    "country": S,
    "customer_type": S,
    "industry": S,
    "hard_match": obj({"country_ok": B, "customer_type_ok": B, "excluded": B, "note": S}),
    "grade_reason": S,
    "profile": obj({
        "summary": S,                # 公司简介与主营
        "size": S,
        "markets": S,
        "recent_news": SL,           # 近期动态
        "pain_points": SL,           # 可能痛点与需求
        "buying_signals": SL,
        "risks": SL,
        "fit_points": SL,            # 与我方产品的契合点
    }),
    "decision_makers": {"type": "array", "items": obj({"name": S, "title": S, "email": S, "source_url": S})},
    "emails": SL,
    "telegram": SL,
    "sources": {"type": "array", "items": SOURCE},
})

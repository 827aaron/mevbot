"""【仅供开发测试】两个假中转站，用来端到端测试“一键配置 / AI 选模型 / 拒答换模型 / 中转站故障切换”。

- relay-a：模型名带厂商前缀（acme/strong-pro），有价格
- relay-b：同样的模型换了写法（Strong.Pro、balanced-2025…），没有价格；设置 mock_relay_b_down=True 时模拟宕机
- 模型行为（按统一名）：strong-pro 高质量贵；balanced 中等；cheap-flash 便宜、偶尔格式错；
  strict-safe 质量不错但经常拒答；tiny 很便宜但质量差、常格式错
内容仍由 mock.py 按用途生成；这里只叠加“拒答 / 格式错 / 质量 / 价格 / 延迟”的差异。
"""
import hashlib
import json
import random
import time

from .. import settings
from ..errors import UserError
from .base import LLMResponse

BEHAVIOR = {   # quality, refuse_rate, format_err_rate, price_in, price_out, latency_s
    "strong-pro": (0.95, 0.0, 0.0, 6.0, 30.0, 0.05),
    "balanced": (0.85, 0.0, 0.02, 1.0, 4.0, 0.03),
    "cheap-flash": (0.72, 0.0, 0.12, 0.10, 0.40, 0.01),
    "strict-safe": (0.86, 0.5, 0.0, 1.0, 4.0, 0.03),
    "tiny": (0.40, 0.0, 0.5, 0.02, 0.05, 0.01),
    "junk": (0.10, 0.0, 1.0, 0.01, 0.01, 0.0),        # 永远答不出合格格式（测试“这个模型不能按格式回答”）
}
CATALOG = {
    "mock://relay-a": [("acme/strong-pro", "strong-pro"), ("acme/balanced", "balanced"), ("acme/cheap-flash", "cheap-flash"),
                       ("acme/strict-safe", "strict-safe"), ("acme/tiny", "tiny")],
    "mock://relay-b": [("Strong.Pro", "strong-pro"), ("balanced", "balanced"), ("cheap_flash", "cheap-flash")],
    "mock://relay-c": [],          # 没有模型列表的“自定义 API”：要客户手动填模型名
}
KEY_OK = "test-key"   # 假中转站接受任何非空 Key，除了 "bad"


def _behavior(model_id):
    from .relay import norm
    return BEHAVIOR.get(norm(model_id), (0.6, 0.0, 0.1, 1.0, 2.0, 0.01))


def check(base, key):
    if base not in CATALOG:
        raise UserError("relay_bad_url")
    if not key or key == "bad":
        raise UserError("key_invalid", provider="relay")
    if base == "mock://relay-b" and settings.get("mock_relay_b_down", False):
        raise UserError("relay_unreachable", detail="mock outage")


def catalog(base):
    out = []
    for mid, n in CATALOG.get(base, []):
        q, rr, fe, pin, pout, lat = BEHAVIOR[n]
        priced = base == "mock://relay-a"
        out.append({"id": mid, "name": mid, "price_in": pin if priced else None, "price_out": pout if priced else None,
                    "context": 128000, "structured": True, "tools": n != "tiny", "web": False})
    return out


def _rng(*parts):
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def complete(relay, cfg, req) -> LLMResponse:
    from . import mock
    if relay["base_url"] == "mock://relay-b" and settings.get("mock_relay_b_down", False):
        raise UserError("network_error", provider=relay["name"])
    q, refuse, ferr, pin, pout, lat = _behavior(cfg.model)
    time.sleep(lat)
    r = _rng(cfg.model, req.purpose, req.messages[-1]["content"][:200])
    # 拒答：只在“看起来像在找人/收集联系方式”的任务上发生（模拟安全分类器误判）
    if refuse and req.purpose not in ("key_test", "eval_judge", "eval_shortlist") and r.random() < refuse:
        raise UserError("model_refused", category="mock")
    if req.purpose.startswith("eval_"):
        data = _eval(req, q, r)
        text = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data
    else:
        resp = mock.complete(cfg, req)
        text = resp.text
    if req.json_schema and r.random() < ferr:
        text = "Sure! Here is the result: " + text[: max(10, len(text) // 2)]   # 格式错：不完整的 JSON
    prompt_len = len(req.system) + sum(len(str(m.get("content", ""))) for m in req.messages)
    return LLMResponse(text=text, model=cfg.model, input_tokens=max(1, prompt_len // 4), output_tokens=max(1, len(text) // 4))


def _eval(req, q, r):
    ctx = req.context or {}
    kind = req.purpose
    if kind == "eval_classify":
        labels = ctx["labels"]
        options = ctx["options"]
        return {"items": [{"idx": i, "label": lab if r.random() < q else r.choice(options)} for i, lab in enumerate(labels)]}
    if kind == "eval_extract":
        names = ctx["expected"]
        got = [n for n in names if r.random() < q] + (["Fake Company Ltd"] if r.random() > q else [])
        return {"companies": got}
    if kind == "eval_grade":
        return {"grade": ctx["expected"] if r.random() < q else r.choice(["B", "C", "D"]), "reason": "mock"}
    if kind == "eval_write":
        return f"[mock-quality={q:.2f}] Subject: Hello\n\nDear partner, ... (EN)\n\nSehr geehrte Damen und Herren, ... (DE)"
    if kind == "eval_judge":
        import re
        m = re.search(r"mock-quality=([0-9.]+)", req.messages[-1]["content"])
        return {"score": round(float(m.group(1)) * 10, 1) if m else 5.0, "reason": "mock judge"}
    if kind == "eval_shortlist":
        return {"roles": ctx.get("fallback") or {}}
    return {}

"""线索质量（v1 没有人工审核环节，PRD §1）。

- 抽查：数据师判为合格的线索，按比例（默认 10%）交给“裁判模型”复核等级；
  “这条不好”的比例上升时自动提高抽查比例（> 10% 翻倍，> 20% 三倍，最多 50%）。
  裁判不同意，或把握不足（< 70%）→ 不推送，进备用池（原因：抽查未通过）
  裁判模型：判断模型开着就用判断模型（便宜）；否则用销售主管的强模型做一次结构化判断
- 指标：近 7 天推送给销售的线索里“这条不好”的比例、成交比例 —— 取代“审核通过率”；按周画成曲线（AI 记忆页）
这些比例都是程序内部参数（主管可调），客户不需要设置。
"""
import random
import time

from . import db, llm, prompts, settings

DEFAULTS = {"spot_rate": 0.10, "max_rate": 0.5, "uncertain": 0.7, "window_days": 7}

# 提示词在提示词库：quality.judge（裁判）、decision.grade_check（判断模型选择题）

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["agree", "confidence", "reason"],
          "properties": {"agree": {"type": "boolean"}, "confidence": {"type": "number"}, "reason": {"type": "string"}}}


def cfg() -> dict:
    c = dict(DEFAULTS)
    c.update(settings.get("quality", {}) or {})
    return c


def rates(days: float | None = None) -> dict:
    """近 N 天推送给销售的线索：这条不好的比例、成交的比例。"""
    days = days or float(cfg()["window_days"])
    since = time.time() - days * 86400
    r = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(feedback=-1),0) bad, COALESCE(SUM(deal=1),0) won FROM leads "
                     "WHERE pushed_at IS NOT NULL AND pushed_at>=?", (since,))
    n = r["n"] or 0
    return {"pushed": n, "bad": r["bad"], "deals": r["won"],
            "bad_rate": round(r["bad"] / n, 3) if n else 0.0, "deal_rate": round(r["won"] / n, 3) if n else 0.0}


def current_rate() -> float:
    c = cfg()
    base, br = float(c["spot_rate"]), rates()["bad_rate"]
    mult = 3 if br > 0.2 else 2 if br > 0.1 else 1
    return min(float(c["max_rate"]), base * mult)


def should_check() -> bool:
    return random.random() < current_rate()


def judge(lead: dict, icp_brief: str, material: str, task_id=None) -> dict:
    """裁判复核一条线索。返回 {agree, confidence, by, reason}，并记录到 quality_checks。"""
    from .llm import decision
    grade = lead.get("grade")
    out = {"agree": True, "confidence": 1.0, "by": "", "reason": ""}
    if decision.enabled():
        q, opts = prompts.options("decision.grade_check")
        d = decision.decide(material[:12000], opts, f"{q} (grade {grade})\n" + icp_brief, agent="analyst",
                            task_id=task_id,
                            context={"mock_hint": "agree" if (lead.get("score") or 0) >= 60 else "disagree"})
        if d:
            out.update(agree=d.choice == "agree" and not d.escalate, confidence=d.confidence, by="decision")
    if not out["by"]:
        data, _ = llm.call_json("manager", "spot_check", prompts.get("quality.judge"),
                                f"## Customer's target-customer template\n{icp_brief}\n\n## Lead (graded {grade}, score "
                                f"{lead.get('score')})\n{material[:12000]}", SCHEMA, task_id=task_id, effort="low",
                                context={"grade": grade, "score": lead.get("score")})
        out.update(agree=bool(data.get("agree")), confidence=float(data.get("confidence") or 0),
                   reason=str(data.get("reason") or "")[:300], by="llm")
    ok = out["agree"] and out["confidence"] >= float(cfg()["uncertain"])
    out["passed"] = ok
    db.execute("INSERT INTO quality_checks(ts, task_id, company, grade, agree, confidence, passed, by_model, reason) "
               "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), task_id, lead.get("company_name", ""), grade,
                                              int(out["agree"]), out["confidence"], int(ok), out["by"], out["reason"]))
    return out


def curve(weeks: int = 12) -> list[dict]:
    """按周：推送给销售的线索数、这条不好比例、成交比例（AI 记忆 → 学到的经验）。"""
    rows = db.query("SELECT strftime('%Y-W%W', pushed_at, 'unixepoch', 'localtime') wk, COUNT(*) n, "
                    "COALESCE(SUM(feedback=-1),0) bad, COALESCE(SUM(deal=1),0) won FROM leads "
                    "WHERE pushed_at IS NOT NULL GROUP BY wk ORDER BY wk DESC LIMIT ?", (weeks,))
    return [{"period": r["wk"], "pushed": r["n"], "bad_rate": round(r["bad"] / r["n"], 3) if r["n"] else 0,
             "deal_rate": round(r["won"] / r["n"], 3) if r["n"] else 0} for r in reversed(rows)]


def spot_stats(days=7) -> dict:
    since = time.time() - days * 86400
    r = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(passed=0),0) failed FROM quality_checks WHERE ts>=?", (since,))
    return {"checked": r["n"], "failed": r["failed"], "rate": round(current_rate(), 3)}

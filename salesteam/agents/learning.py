"""AI 学习（PRD 第 12 节）—— 第 1 步只做“地基”：

- 样本库：每个模板的正/负样本（线索库 👍/👎、手动粘贴例子；第 2 步加审核台，第 3 步加结果反馈）
- 学到的知识：按模板分版本存储（关键词 / 例子 / 规则），关键词与例子自动生效，大规则变化待确认，可回退
- 示例参照：数据师打分时带上最相关的几个正/负样本（第 1 步按“关键词重合 + 新近程度 + 权重”排序；
  第 2 步换成本地语义相似模型）
"""
import re
import time

from .. import db
from ..errors import UserError

# 「这条不好」的原因：只用通用的几项 + 其他（自由填写）。不预设什么样的客户不好（PRD 2.4.2）
REASONS = ["false_info", "not_target", "unreachable", "other"]
# 旧版本的原因代码仍然接受（旧数据显示、同行 / 已合作客户仍进黑名单）
LEGACY_REASONS = ["competitor", "not_buyer", "too_small", "wrong_country", "retailer", "existing_customer",
                  "wrong_contact"]
ALL_REASONS = REASONS + LEGACY_REASONS
ORIGINS = ["manual_mark", "review_approve", "review_reject", "manual_paste", "outcome"]
KNOWLEDGE_KINDS = ["keywords", "examples", "rule", "message_type", "scoring_weights", "exclusions"]
# 自动生效（可一键回退）：关键词、例子、打分权重、排除项——打分权重/排除项按 PRD 5.2.1 由程序/销售主管根据客户的
# 大白话反馈自动调整（客户不需要配置）；其余“大的规则变化”需要客户确认
AUTO_APPLY_KINDS = {"keywords", "examples", "scoring_weights", "exclusions"}

# 打分权重：不再写死默认值（PRD 2.4.2）。版本 1 = 模板里 AI 根据客户资料生成的打分标准（grading_criteria）的权重；
# 之后销售主管 / 学习按反馈调整（按标准名称覆盖），都可回退。

_STOP = set("""the and for with from that this are was were you your our their they them have has had not but all any
can will would into about more most such than then also its it's per via www com http https html of to in on at by as is
be or an a we us 的 了 和 是 在 有 与 及 等 我们 他们 公司""".split())


def keywords_of(text: str, limit=40) -> list[str]:
    words = re.findall(r"[^\W\d_]{3,}", (text or "").lower())
    freq = {}
    for w in words:
        if w not in _STOP:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])][:limit]


def lead_text(lead: dict) -> str:
    p = lead.get("profile") or {}
    parts = [lead.get("company_name", ""), lead.get("country", ""), lead.get("customer_type", ""),
             lead.get("industry", ""), p.get("summary", ""), "; ".join(p.get("buying_signals") or []),
             "; ".join(p.get("fit_points") or []), "; ".join(p.get("risks") or [])]
    return "\n".join(x for x in parts if x)


def add_sample(icp_id: int, polarity: int, origin: str, text: str, reason="", note="", lead_id=None,
               snapshot=None, weight=1.0, agent="analyst", label="") -> int:
    if polarity not in (1, -1):
        raise UserError("invalid_value", field="polarity")
    if origin not in ORIGINS:
        raise UserError("invalid_value", field="origin")
    if reason and reason not in ALL_REASONS:
        raise UserError("invalid_value", field="reason")
    if not (text or "").strip():
        raise UserError("sample_text_missing")
    if not db.query_one("SELECT id FROM icp_templates WHERE id=?", (icp_id,)):
        raise UserError("icp_not_found", 404)
    return db.execute(
        "INSERT INTO icp_samples(icp_id, polarity, origin, agent, reason, note, text, lead_id, snapshot, keywords, "
        "weight, created_at, label) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (icp_id, polarity, origin, agent, reason, note.strip()[:500], text.strip()[:4000], lead_id,
         db.dumps(snapshot or {}), db.dumps(keywords_of(text)), float(weight), db.now(), label))


def mark_lead(lead_id: int, polarity: int, reason="", note="") -> dict:
    """线索库 👍 / 👎（含原因）。polarity=0 表示取消标记。同一条线索只保留一个手动标记样本。"""
    row = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not row:
        raise UserError("lead_not_found", 404)
    if polarity == -1 and not reason:
        raise UserError("feedback_reason_missing")
    if reason == "other" and not (note or "").strip():
        raise UserError("feedback_note_missing")
    with db.transaction():
        db.execute("DELETE FROM icp_samples WHERE lead_id=? AND origin='manual_mark'", (lead_id,))
        db.execute("UPDATE leads SET feedback=?, feedback_reason=?, feedback_note=?, updated_at=? WHERE id=?",
                   (polarity, reason if polarity == -1 else "", (note or "").strip() if polarity else "",
                    db.now(), lead_id))
        if polarity and row["icp_id"]:
            lead = dict(row)
            lead["profile"] = db.loads(row["profile"], {})
            snap = {k: lead.get(k) for k in ("company_name", "website", "country", "customer_type", "industry",
                                              "grade", "score", "first_source")}
            snap["profile_summary"] = lead["profile"].get("summary", "")
            add_sample(row["icp_id"], polarity, "manual_mark", lead_text(lead), reason=reason if polarity == -1 else "",
                       note=note or "", lead_id=lead_id, snapshot=snap)
    return {"lead_id": lead_id, "feedback": polarity, "reason": reason, "icp_linked": bool(row["icp_id"])}


def list_samples(icp_id: int, agent=None) -> list[dict]:
    sql, args = "SELECT * FROM icp_samples WHERE icp_id=?", [icp_id]
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    rows = db.query(sql + " ORDER BY id DESC", args)
    for r in rows:
        r["snapshot"] = db.loads(r["snapshot"], {})
        r["keywords"] = db.loads(r["keywords"], [])
    return rows


def delete_sample(sample_id: int):
    row = db.query_one("SELECT * FROM icp_samples WHERE id=?", (sample_id,))
    if not row:
        raise UserError("sample_not_found", 404)
    with db.transaction():
        db.execute("DELETE FROM icp_samples WHERE id=?", (sample_id,))
        db.execute("DELETE FROM embeddings WHERE kind='sample' AND ref_id=?", (sample_id,))
        if row["lead_id"] and row["origin"] == "manual_mark":
            db.execute("UPDATE leads SET feedback=0, feedback_reason='', feedback_note='' WHERE id=?",
                       (row["lead_id"],))


def relevant_samples(icp_id: int | None, text: str, per_polarity=2, agent="analyst") -> dict:
    """为打分挑选最相关的正/负样本（few-shot 参照）。"""
    if not icp_id:
        return {"positive": [], "negative": []}
    rows = db.query("SELECT * FROM icp_samples WHERE icp_id=? AND agent=? ORDER BY id DESC LIMIT 300",
                    (icp_id, agent))
    if not rows:
        return {"positive": [], "negative": []}
    target = set(keywords_of(text, 80))
    now = time.time()
    scored = []
    for r in rows:
        kw = set(db.loads(r["keywords"], []))
        overlap = len(target & kw) / (len(kw) ** 0.5 or 1)
        age_days = (now - r["created_at"]) / 86400
        score = overlap * 2 + r["weight"] + 1 / (1 + age_days / 30)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    out = {"positive": [], "negative": []}
    for _, r in scored:
        bucket = out["positive" if r["polarity"] > 0 else "negative"]
        if len(bucket) < per_polarity:
            bucket.append({"text": r["text"][:800], "reason": r["reason"], "note": r["note"], "origin": r["origin"]})
    return out


def few_shot_block(samples: dict) -> str:
    if not samples["positive"] and not samples["negative"]:
        return ""
    from .. import prompts
    items = []
    for s in samples["positive"]:
        items.append(f"GOOD example:\n{s['text']}")
    for s in samples["negative"]:
        why = s["reason"] + (f" — {s['note']}" if s["note"] else "")
        items.append(f"BAD example (reason: {why}):\n{s['text']}")
    return "\n" + prompts.render("layer.examples", items="\n\n".join(items))


# ------------------------------------------------------------------ 学到的知识（版本化，第 2 步 AI 学习中心使用）

def add_knowledge(icp_id: int, kind: str, content, origin="learning", note="") -> dict:
    if kind not in KNOWLEDGE_KINDS:
        raise UserError("invalid_value", field="kind")
    status = "auto_applied" if kind in AUTO_APPLY_KINDS else "pending_confirmation"
    with db.transaction():
        v = (db.query_one("SELECT COALESCE(MAX(version),0) v FROM icp_knowledge WHERE icp_id=?", (icp_id,))["v"]) + 1
        kid = db.execute("INSERT INTO icp_knowledge(icp_id, version, kind, content, status, origin, note, created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (icp_id, v, kind, db.dumps(content), status, origin, note,
                                                     db.now()))
    return {"id": kid, "version": v, "status": status}


def ensure_defaults(icp_id: int):
    """新模板：写入版本 1 的默认打分权重和空排除项，之后的调整都在此基础上加版本。"""
    have = {r["kind"] for r in db.query("SELECT DISTINCT kind FROM icp_knowledge WHERE icp_id=?", (icp_id,))}
    if "scoring_weights" not in have:
        r = db.query_one("SELECT data FROM icp_templates WHERE id=?", (icp_id,))
        seed_weights(icp_id, db.loads(r["data"], {}) if r else {})
    if "exclusions" not in have:
        add_knowledge(icp_id, "exclusions", {"keywords": [], "companies": []}, origin="system", note="default")


def seed_weights(icp_id: int, data: dict) -> bool:
    """打分权重版本 1：来自模板的打分标准（AI 根据客户资料生成）。模板还没有标准时不写，等生成后再写。"""
    w = {c["name"]: c.get("weight") or 0 for c in data.get("grading_criteria") or [] if c.get("name")}
    if not w or db.query_one("SELECT 1 FROM icp_knowledge WHERE icp_id=? AND kind='scoring_weights'", (icp_id,)):
        return False
    add_knowledge(icp_id, "scoring_weights", w, origin="system", note="from profile")
    return True


def effective(icp_id: int | None) -> dict:
    """数据师实际使用的知识：每种只取最新一个生效版本。"""
    out = {"weights": {}, "exclude_keywords": [], "exclude_companies": [], "keywords": []}
    if not icp_id:
        return out
    seen = set()
    for r in active_knowledge(icp_id):   # 已按版本倒序
        if r["kind"] in seen:
            continue
        seen.add(r["kind"])
        c = r["content"] or {}
        if r["kind"] == "scoring_weights" and isinstance(c, dict):
            out["weights"] = {k: float(v) for k, v in c.items() if isinstance(v, (int, float))} or out["weights"]
        elif r["kind"] == "exclusions" and isinstance(c, dict):
            out["exclude_keywords"] = list(c.get("keywords") or [])
            out["exclude_companies"] = list(c.get("companies") or [])
        elif r["kind"] == "keywords" and isinstance(c, (list, dict)):
            out["keywords"] = list(c if isinstance(c, list) else c.get("keywords") or [])
    return out


def decide_knowledge(knowledge_id: int, accept: bool):
    db.execute("UPDATE icp_knowledge SET status=?, decided_at=? WHERE id=? AND status='pending_confirmation'",
               ("confirmed" if accept else "rejected", db.now(), knowledge_id))


def knowledge_history(icp_id: int) -> list[dict]:
    rows = db.query("SELECT * FROM icp_knowledge WHERE icp_id=? ORDER BY version DESC", (icp_id,))
    for r in rows:
        r["content"] = db.loads(r["content"], None)
    return rows


def active_knowledge(icp_id: int) -> list[dict]:
    return [r for r in knowledge_history(icp_id) if r["status"] in ("auto_applied", "confirmed")]


def undo_latest(icp_id: int, kind: str) -> int:
    """撤销某一种知识最近一次生效的版本（回到上一版；保留记录）。返回撤销了几条（0 / 1）。"""
    r = db.query_one("SELECT id FROM icp_knowledge WHERE icp_id=? AND kind=? AND status IN ('auto_applied','confirmed') "
                     "ORDER BY version DESC LIMIT 1", (icp_id, kind))
    if not r:
        return 0
    db.execute("UPDATE icp_knowledge SET status='rolled_back', decided_at=? WHERE id=?", (db.now(), r["id"]))
    return 1


def rollback(icp_id: int, to_version: int) -> int:
    """回退：把高于 to_version 的生效知识标记为 rolled_back（保留记录，可审计）。"""
    with db.transaction():
        n = db.query_one("SELECT COUNT(*) n FROM icp_knowledge WHERE icp_id=? AND version>? AND status IN "
                         "('auto_applied','confirmed','pending_confirmation')", (icp_id, to_version))["n"]
        db.execute("UPDATE icp_knowledge SET status='rolled_back', decided_at=? WHERE icp_id=? AND version>? AND "
                   "status IN ('auto_applied','confirmed','pending_confirmation')", (db.now(), icp_id, to_version))
    return n

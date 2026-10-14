"""🙋 人工审核台（PRD 7）+「这条不好」（PRD 5.2.1 / 14）+ 已成交标记。

- 操作：✅ 通过 / ❌ 这条不好（拒绝 + 原因）/ ✏️ 修改后通过 / ⏸️ 稍后；都可以撤销
- 批量：一键通过所有 A 级；按国家/来源筛选后批量通过
- 通过时可指定负责的真人销售和开发方案
- 审核通过 = 授权销售 AI 发送（第 3 步；发送功能需客户另行开启，未开启时只生成草稿）—— 程序只把 approved 的线索交给销售员
- 每个决定都写进样本库（AI 学习）：通过 → 好例子；拒绝 → 坏例子 + 原因；TG 来源的线索同时给监控员一个样本
-「这条不好」：拒绝 + 负样本 + 按原因做程序化调整（同行 / 已合作客户 → 加入排除名单，新版本知识，可回退）+ 主管一句话回复 +【撤销】
- 审核通过率 = 通过 ÷（通过 + 拒绝），不计“稍后”
"""
import time

from . import db, i18n, leads
from .agents import learning
from .errors import UserError

ACTIONS = ("approve", "reject", "later", "edit_approve")
EDITABLE = ("company_name", "website", "country", "customer_type", "grade")


def _lead(lead_id):
    r = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not r:
        raise UserError("lead_not_found", 404)
    return r


def pending(f: dict) -> dict:
    cond = ["status='pending_review'"]
    args = []
    if not f.get("include_later"):
        cond.append("(review_later_until IS NULL OR review_later_until<?)")
        args.append(time.time())
    for k in ("country", "grade"):
        if f.get(k):
            cond.append(f"{k}=?")
            args.append(f[k])
    if f.get("source"):
        cond.append("first_source=?")
        args.append(f["source"])
    if f.get("icp_id"):
        cond.append("icp_id=?")
        args.append(int(f["icp_id"]))
    where = " WHERE " + " AND ".join(cond)
    rows = db.query(f"SELECT * FROM leads{where} ORDER BY CASE grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 WHEN 'C' THEN 2 "
                    f"ELSE 3 END, score DESC, id LIMIT 300", args)
    for r in rows:
        r["profile"] = db.loads(r["profile"], {})
        r["contacts"] = db.loads(r["contacts"], [])
        r["sources"] = db.loads(r["sources"], [])
    counts = db.query_one(
        "SELECT COALESCE(SUM(status='pending_review' AND (review_later_until IS NULL OR review_later_until<?)),0) p, "
        "COALESCE(SUM(status='pending_review' AND review_later_until>=?),0) l, "
        "COALESCE(SUM(status='pending_review' AND grade='A' AND (review_later_until IS NULL OR review_later_until<?)),0) a,"
        " COALESCE(SUM(reviewed_at>=?),0) today FROM leads",
        (time.time(), time.time(), time.time(), time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))))
    facets = {"countries": [r["country"] for r in db.query(
        "SELECT DISTINCT country FROM leads WHERE status='pending_review' AND country<>''")],
              "sources": [r["first_source"] for r in db.query(
                  "SELECT DISTINCT first_source FROM leads WHERE status='pending_review'")]}
    return {"items": rows, "counts": {"pending": counts["p"], "later": counts["l"], "grade_a": counts["a"],
                                      "done_today": counts["today"]}, "facets": facets}


def _samples_for(row, polarity, origin, reason="", note="", weight=1.0) -> list[int]:
    """给数据师（线索摘要）和监控员（TG 原文）各记一个样本。"""
    if not row["icp_id"]:
        return []
    lead = dict(row)
    lead["profile"] = db.loads(row["profile"], {})
    snap = {k: lead.get(k) for k in ("company_name", "website", "country", "customer_type", "grade", "first_source")}
    ids = [learning.add_sample(row["icp_id"], polarity, origin, learning.lead_text(lead) or row["company_name"],
                               reason=reason, note=note, lead_id=row["id"], snapshot=snap, weight=weight)]
    if row["first_source"] == "tg":
        src = db.loads(row["sources"], [])
        quote = next((s.get("quote") for s in src if s.get("quote")), "")
        if quote:
            ids.append(learning.add_sample(row["icp_id"], polarity, origin, quote, reason=reason, note=note,
                                           lead_id=row["id"], snapshot=snap, weight=weight, agent="monitor",
                                           label=row["message_type"] or ("buy_request" if polarity > 0 else "other")))
    return ids


def act(lead_id: int, action: str, reason="", reasons=None, note="", edits=None, assigned_to="", plan="") -> dict:
    if action not in ACTIONS:
        raise UserError("invalid_value", field="action")
    row = _lead(lead_id)
    undo = {"status": row["status"], "reviewed_at": row["reviewed_at"], "review_later_until": row["review_later_until"],
            "reject_reason": row["reject_reason"], "review_note": row["review_note"], "assigned_to": row["assigned_to"],
            "review_plan": row["review_plan"], "feedback": row["feedback"], "feedback_reason": row["feedback_reason"],
            "feedback_note": row["feedback_note"], "samples": [], "knowledge": [], "edits": {}}
    now = db.now()
    reply = ""
    if action == "later":
        db.execute("UPDATE leads SET review_later_until=?, updated_at=? WHERE id=?", (now + 86400, now, lead_id))
    elif action in ("approve", "edit_approve"):
        if action == "edit_approve" and edits:
            undo["edits"] = _apply_edits(row, edits)
            row = _lead(lead_id)
        db.execute("UPDATE leads SET status='approved', reviewed_at=?, review_later_until=NULL, assigned_to=?, "
                   "review_plan=?, review_note=?, updated_at=? WHERE id=?",
                   (now, assigned_to or "", plan or "", note or "", now, lead_id))
        undo["samples"] = _samples_for(row, 1, "review_approve", note=note)
    else:   # reject =「这条不好」
        reasons = [r for r in (reasons or ([reason] if reason else [])) if r in learning.ALL_REASONS]
        if not reasons and not (note or "").strip():
            raise UserError("feedback_reason_missing")
        main = reasons[0] if reasons else "other"
        db.execute("UPDATE leads SET status='rejected', reviewed_at=?, reject_reason=?, review_note=?, feedback=-1, "
                   "feedback_reason=?, feedback_note=?, updated_at=? WHERE id=?",
                   (now, ",".join(reasons) or "other", note or "", main, note or "", now, lead_id))
        # v1 没有审核台：线索库的「这条不好」记为线索库标记（与 👍 同一来源，每条线索只留一个），PRD §1
        origin = "review_reject" if leads.review_on() else "manual_mark"
        if origin == "manual_mark":
            db.execute("DELETE FROM icp_samples WHERE lead_id=? AND origin='manual_mark'", (lead_id,))
        undo["samples"] = _samples_for(row, -1, origin, reason=main, note=note)
        adj, kid = _adjust(row, reasons)
        if kid:
            undo["knowledge"].append(kid)
        # 同行 / 已合作客户：所有身份进黑名单（PRD 5.2.2），以后任何来源再出现都自动排除；撤销时移除
        bl_reason = "competitor" if "competitor" in reasons else "existing_customer" if "existing_customer" in reasons else ""
        if bl_reason:
            from . import leadpool
            undo["blacklist"] = leadpool.blacklist_from_lead(lead_id, bl_reason)
        reply = i18n.t("nf.reply", {"adj": adj})
        _tell_manager(row, reasons, note, reply)
    db.execute("UPDATE leads SET review_edits=? WHERE id=?",
               (db.dumps({**db.loads(row["review_edits"] if "review_edits" in row.keys() else "{}", {}),
                          "undo": undo}), lead_id))
    return {"lead_id": lead_id, "action": action, "reply": reply}


def _tell_manager(row, reasons, note, reply):
    """「这条不好」也写进与销售主管的对话（工作台主对话 / 各页面的主管抽屉都能看到），回复带 [撤销] [去审核台]。"""
    from .agents import chat
    why = "、".join(i18n.t("fbr." + r) for r in reasons) if reasons else ""
    user_text = i18n.t("chat.nf_user", {"name": row["company_name"], "reason": (why + (" — " if why and note else "") +
                                                                               (note or "")).strip()})
    try:
        chat.post("manager", reply, user_text=user_text,
                  actions=[{"type": "undo_review", "lead_id": row["id"]},
                           {"type": "goto", "route": "review" if leads.review_on() else "leads"}])
    except Exception:  # noqa: BLE001  对话记录失败不影响审核本身
        pass


def _apply_edits(row, edits: dict) -> dict:
    old, sets, args = {}, [], []
    for k in EDITABLE:
        if k in edits and edits[k] is not None and str(edits[k]) != str(row[k]):
            v = str(edits[k]).strip()
            if k == "grade" and v not in ("A", "B", "C"):
                raise UserError("invalid_value", field="grade")
            old[k] = row[k]
            sets.append(f"{k}=?")
            args.append(v)
    contacts = db.loads(row["contacts"], [])
    if edits.get("email") or edits.get("telegram") or edits.get("contact_name"):
        old["contacts"] = contacts
        contacts = [{"name": edits.get("contact_name", ""), "title": edits.get("contact_title", ""),
                     "email": (edits.get("email") or "").strip().lower(), "telegram": (edits.get("telegram") or "").strip(),
                     "source_url": "", "verified": True, "edited": True}] + contacts
        sets.append("contacts=?")
        args.append(db.dumps(contacts))
    if sets:
        db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", (*args, row["id"]))
    return old


def _adjust(row, reasons) -> tuple[str, int | None]:
    """按原因做程序化调整（只做有把握的：同行 / 已合作客户 → 加入排除名单）。其余原因先记为坏例子，
    第 3 步由销售主管理解原因后再调整打分规则（可撤销）。"""
    if row["icp_id"] and ("competitor" in reasons or "existing_customer" in reasons):
        eff = learning.effective(row["icp_id"])
        name = row["domain"] or row["company_name"]
        comps = list(dict.fromkeys(eff["exclude_companies"] + [name]))
        k = learning.add_knowledge(row["icp_id"], "exclusions", {"keywords": eff["exclude_keywords"],
                                                                 "companies": comps},
                                   origin="feedback", note=f"lead #{row['id']}")
        return i18n.t("nf.adj_excluded", {"name": name}), k["id"]
    key = reasons[0] if reasons else "other"
    return i18n.t("nf.adj_sample", {"reason": i18n.t("fbr." + key)}), None


def undo(lead_id: int) -> dict:
    row = _lead(lead_id)
    meta = db.loads(row["review_edits"], {})
    u = meta.get("undo")
    if not u:
        raise UserError("nothing_to_undo")
    for sid in u.get("samples") or []:
        try:
            learning.delete_sample(sid)
        except UserError:
            pass
    for kid in u.get("knowledge") or []:
        db.execute("UPDATE icp_knowledge SET status='rolled_back', decided_at=? WHERE id=?", (db.now(), kid))
    if u.get("blacklist"):
        from . import leadpool
        leadpool.blacklist_remove(u["blacklist"])
    edits = u.get("edits") or {}
    sets, args = [], []
    for k, v in edits.items():
        sets.append(f"{k}=?")
        args.append(db.dumps(v) if k == "contacts" else v)
    for k in ("status", "reviewed_at", "review_later_until", "reject_reason", "review_note", "assigned_to",
              "review_plan", "feedback", "feedback_reason", "feedback_note"):
        sets.append(f"{k}=?")
        args.append(u.get(k))
    meta.pop("undo", None)
    sets.append("review_edits=?")
    args.append(db.dumps(meta))
    db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", (*args, lead_id))
    from .agents import chat
    chat.mark_lead_undone(lead_id)
    return {"lead_id": lead_id, "status": u.get("status")}


def batch_approve(ids=None, grade=None, f: dict | None = None) -> int:
    if not ids:
        lst = pending({**(f or {}), "grade": grade or (f or {}).get("grade")})["items"]
        ids = [r["id"] for r in lst]
    n = 0
    for i in ids:
        r = db.query_one("SELECT status FROM leads WHERE id=?", (int(i),))
        if r and r["status"] == "pending_review":
            act(int(i), "approve")
            n += 1
    return n


def mark_deal(lead_id: int, on: bool) -> dict:
    """已成交（第 3 步做自动成交识别）：高权重好例子。"""
    row = _lead(lead_id)
    now = db.now()
    db.execute("UPDATE leads SET deal=?, deal_at=?, updated_at=? WHERE id=?", (int(on), now if on else None, now, lead_id))
    db.execute("DELETE FROM icp_samples WHERE lead_id=? AND origin='outcome'", (lead_id,))
    if on:
        _samples_for(row, 1, "outcome", note="deal", weight=3.0)
    return {"lead_id": lead_id, "deal": int(on)}


def update_lead(lead_id: int, data: dict) -> dict:
    """线索库里直接修改（如补联系方式）。"""
    row = _lead(lead_id)
    _apply_edits(row, data)
    return {"ok": True}


__all__ = ["pending", "act", "undo", "batch_approve", "mark_deal", "update_lead", "leads"]

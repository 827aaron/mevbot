"""AI 记忆中心（PRD 2.5）。原则：重要的规则不靠 AI 记，靠程序管。

记忆层（每次任务按需取用，只带与当前工作相关的部分）：
- 📜 岗位说明书：提示词库 salesteam/prompts（role.<AI>），客户可查看和修改，AI 不能改
- 🏢 客户档案：我方画像（settings.company_profile）+ 理想客户模板
- 🎯 客户定的数字：总预算、每日上限等（程序计数，AI 只读）
- 🧠 学到的经验：learning.py（关键词、样本、规则，带版本）
- 📋 工作记录：tasks / leads 表（程序）
- 📝 备忘录：客户在对话中交代的要求，AI 提炼 → 客户确认后长期生效（本文件）
"""
import time

from .. import db, settings
from ..errors import UserError
from .registry import AGENT_KEYS

def role(agent: str) -> str:
    """岗位说明书：提示词库 role.<AI>（只写职责和怎么干活，不含业务内容；客户可以查看和修改）。"""
    from .. import prompts
    try:
        return prompts.get(f"role.{agent}")
    except UserError:
        return ""


# ------------------------------------------------------------------ 备忘录

def add_memo(agent: str, text: str, origin="chat", source_ref="", status="pending") -> int:
    text = (text or "").strip()
    if not text:
        raise UserError("memo_empty")
    if agent not in AGENT_KEYS and agent != "all":
        raise UserError("unknown_agent", 404, agent=agent)
    dup = db.query_one("SELECT id FROM memos WHERE agent=? AND text=? AND status IN ('pending','confirmed')",
                       (agent, text[:500]))
    if dup:
        return dup["id"]
    return db.execute("INSERT INTO memos(agent, text, status, origin, source_ref, created_at) VALUES(?,?,?,?,?,?)",
                      (agent, text[:500], status, origin, source_ref, db.now()))


def decide_memo(memo_id: int, confirm: bool):
    r = db.query_one("SELECT * FROM memos WHERE id=?", (memo_id,))
    if not r:
        raise UserError("memo_not_found", 404)
    db.execute("UPDATE memos SET status=?, decided_at=? WHERE id=?",
               ("confirmed" if confirm else "rejected", db.now(), memo_id))


def edit_memo(memo_id: int, text: str):
    if not (text or "").strip():
        raise UserError("memo_empty")
    db.execute("UPDATE memos SET text=? WHERE id=?", (text.strip()[:500], memo_id))


def delete_memo(memo_id: int):
    db.execute("DELETE FROM memos WHERE id=?", (memo_id,))


def list_memos(agent=None, status=None):
    sql, args, cond = "SELECT * FROM memos", [], []
    if agent:
        cond.append("(agent=? OR agent='all')")
        args.append(agent)
    if status:
        cond.append("status=?")
        args.append(status)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    return db.query(sql + " ORDER BY status='pending' DESC, id DESC", args)


def memos_block(agent: str) -> str:
    """已确认的备忘录，注入该智能体的提示词（客户的长期要求）。"""
    rows = db.query("SELECT text FROM memos WHERE (agent=? OR agent='all') AND status='confirmed' ORDER BY id",
                    (agent,))
    if not rows:
        return ""
    from .. import prompts
    return "\n\n" + prompts.render("layer.memos", items="\n".join(f"- {r['text']}" for r in rows))


# ------------------------------------------------------------------ 工作数据摘要（单点对话时只带自己的）

def work_summary(agent: str) -> str:
    day0 = time.time() - 7 * 86400
    # Capability facts are available before the first task. They must not be
    # confused with the user's actual configuration or collected results.
    lines = ["Software capabilities (available workflows, not completed actions): web search and crawling; "
             "manual source URLs and feature words; scheduled source-pool/scout discovery; "
             "Telegram joining queue and incremental monitoring; Facebook, Instagram, YouTube, X and Reddit "
             "source entries with separate owner-operated browser logins; per-post/comment evidence and "
             "unchanged-content skipping; local diagnostic ZIP export. Configure accounts/API/ICP and "
             "enable the relevant schedule first. Pages: run=immediate tasks/Manager daily goals, "
             "channels=source pool/scout/TG queue, accounts=platform logins, settings=API/diagnostics. "
             "A listed feature is not proof that this user's account can access a particular source."]
    if agent in ("researcher", "manager"):
        for t in db.query("SELECT id, status, result, target, stats, created_at FROM tasks WHERE kind='research' "
                          "AND created_at>? ORDER BY created_at DESC LIMIT 5", (day0,)):
            s = db.loads(t["stats"], {})
            lines.append(f"research task {time.strftime('%m-%d %H:%M', time.localtime(t['created_at']))}: "
                         f"{t['status']}/{t['result']}, qualified {s.get('qualified', 0)}/{t['target']}, "
                         f"pool {s.get('pool', 0)}, pages {s.get('pages', 0)}")
        by_src = db.query("SELECT source, COUNT(*) n, SUM(status='qualified') q FROM raw_leads WHERE created_at>? "
                          "GROUP BY source", (day0,))
        if by_src:
            lines.append("last 7 days by source: " + "; ".join(f"{r['source']} found {r['n']}, qualified {r['q']}"
                                                                for r in by_src))
    if agent in ("monitor", "manager"):
        c = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(monitored),0) m, COALESCE(SUM(hits),0) h, "
                         "COALESCE(SUM(msgs_read),0) r FROM tg_chats")
        lines.append(f"telegram chats known {c['n']}, monitored {c['m']}, messages read {c['r']}, hits {c['h']}")
        for r in db.query("SELECT title, hits, msgs_read FROM tg_chats WHERE monitored=1 ORDER BY hits DESC LIMIT 8"):
            lines.append(f"  chat '{r['title']}': {r['hits']} hits / {r['msgs_read']} messages")
    if agent in ("analyst", "manager"):
        g = db.query("SELECT grade, COUNT(*) n FROM leads WHERE created_at>? GROUP BY grade", (day0,))
        lines.append("leads last 7 days by grade: " + ", ".join(f"{r['grade']}={r['n']}" for r in g))
        rej = db.query("SELECT reject_reason r, COUNT(*) n FROM leads WHERE status='rejected' AND reviewed_at>? "
                       "GROUP BY reject_reason", (day0,))
        if rej:
            lines.append("rejections by reason: " + ", ".join(f"{r['r']}={r['n']}" for r in rej))
        from .. import quality
        q = quality.rates()
        if q["pushed"]:
            lines.append(f"leads pushed to the customer's sales team last 7 days: {q['pushed']}; marked 'not a good fit': "
                         f"{q['bad']} ({q['bad_rate']:.0%}); deals: {q['deals']} ({q['deal_rate']:.0%})")
    if agent == "manager":
        b = settings.get("budget")
        lines.append(f"total budget (set by customer, read-only): daily ${b['daily']}, monthly ${b['monthly']}")
        # 今天的状态（工作台主对话里客户最常问“今天怎么样”）
        t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        td = db.query_one("SELECT COALESCE(SUM(qualified=1),0) q, COUNT(*) n FROM leads WHERE created_at>=?", (t0,))
        sent = db.query_one("SELECT COUNT(*) n FROM leads WHERE pushed_at>=?", (t0,))["n"]
        unsent = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND status IN ('qualified','pending_review')")["n"]
        lines.append(f"today: {td['q']} qualified leads of {td['n']} new; {sent} pushed to the sales team; "
                     f"{unsent} qualified not yet pushed (the evening push sends them; there is no human review step)")
        run = db.query("SELECT kind, status FROM tasks WHERE status='running'")
        if run:
            lines.append("running now: " + ", ".join(r["kind"] for r in run))
        al = db.query("SELECT level, category FROM alerts WHERE status='open' "
                      "ORDER BY id DESC LIMIT 5")
        if al:
            lines.append("open alerts: " + ", ".join(f"{a['level']}:{a['category']}" for a in al))
    if agent in ("scout", "manager"):
        # 渠道拓展员：渠道池和 AI 找群的情况（第 3 步完整上线）
        d = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(status='suggested'),0) s, COALESCE(SUM(status='joined'),0) j "
                         "FROM tg_discoveries WHERE created_at>?", (day0,))
        ch = db.query("SELECT c.name, c.status, COUNT(l.id) n, COALESCE(SUM(l.feedback=-1),0) bad FROM channels c "
                      "LEFT JOIN leads l ON l.channel_id=c.id GROUP BY c.id ORDER BY n DESC LIMIT 5")
        lines.append(f"group discovery (7 days): {d['n']} evaluated, {d['s']} waiting for the customer, {d['j']} joined")
        if ch:
            lines.append("top channels by leads: " + "; ".join(f"{r['name']} ({r['status']}): {r['n']} leads, "
                                                                f"{r['bad']} not a good fit" for r in ch))
    return "\n".join(lines) or "(no work data yet)"


def layers(agent: str) -> dict:
    """记忆中心页面：按 AI 查看各层记忆。"""
    from . import learning
    tpl = db.query("SELECT id, name FROM icp_templates ORDER BY updated_at DESC")
    return {
        "role": role(agent),
        "memos": list_memos(agent),
        "profile": settings.get("company_profile", {}) or {},
        "templates": tpl,
        "numbers": {"budget": settings.get("budget"), "crawler": settings.get("crawler"),
                    "monitor": settings.get("monitor"), "notify_cutoff": settings.get("notify")["cutoff"]},
        "knowledge": {t["id"]: learning.active_knowledge(t["id"]) for t in tpl} if agent in
        ("analyst", "monitor", "researcher", "manager") else {},
        "work": work_summary(agent),
    }

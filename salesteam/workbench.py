"""工作台（v2 设计：一行目标 · 6 个 AI 小卡 · 和主管的对话）需要的汇总数据。

- 目标：今天的找客户任务目标（客户在任务里设定，🔒）与今天新增的合格线索（A / B）
- 费用：今日花费 / 每日总预算（🔒）
- 每个 AI：开关、状态（working / idle / waiting / paused / error / off / stuck）、一句话短状态、正在做什么（最近一条事件）
- 实时动态：某个 AI 最近的事件（跨任务），工作台卡片展开时轮询
文案都返回 i18n key + 参数，前端翻译。
"""
import time

from . import db, leads, llm, settings, tasks
from .agents.registry import active_agents

LIVE_STATES = {"working", "paused", "stuck", "error", "done"}


def search_guide() -> bool:
    """免费搜索兜底不稳定：没有 Serper / Brave Key、调研员也不用带联网搜索的 Claude 时提示一条。"""
    from .sources import search as search_mod
    if search_mod.which_provider() != "free":
        return False
    cfg = llm.get_config("researcher")
    return not (cfg.provider == "claude" or (cfg.provider == "relay" and "claude" in (cfg.model or "")))


def _today0() -> float:
    return time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))


def goal() -> dict:
    t0 = _today0()
    rows = db.query("SELECT id, target, status, created_at FROM tasks WHERE kind='research' AND created_at>=? "
                    "ORDER BY created_at DESC", (t0,))
    live = tasks.running("research")
    target = sum(r["target"] for r in rows if r["status"] != "cancelled") if rows else 0
    q = db.query_one("SELECT COALESCE(SUM(grade='A'),0) a, COALESCE(SUM(grade='B'),0) b, COUNT(*) n FROM leads "
                     "WHERE qualified=1 AND created_at>=?", (t0,))
    st = "('approved')" if leads.review_on() else "('qualified','approved')"
    pending = db.query_one(f"SELECT COUNT(*) n FROM leads WHERE qualified=1 AND status IN {st} AND pushed_at IS NULL")["n"]
    return {"target": target, "done": q["n"], "A": q["a"], "B": q["b"], "pending_push": pending,
            "task_id": live.id if live else (rows[0]["id"] if rows else None), "running": bool(live)}


def _next_monitor() -> float | None:
    from .tg import pool
    cfg = settings.get("monitor")
    period = {"30m": 1800, "1h": 3600, "6h": 21600, "daily": 86400}.get(cfg.get("interval", "1h"), 3600)
    accs = [a for a in pool.monitor_accounts()
            if db.query_one("SELECT 1 FROM tg_chats WHERE account_id=? AND monitored=1 LIMIT 1", (a["id"],))]
    if not accs:
        return None
    nxt = [(a["last_monitor_at"] or time.time()) + period for a in accs]
    return max(time.time(), min(nxt))


def _latest_event(agent: str) -> dict | None:
    r = db.query_one("SELECT ts, key, params, level FROM task_events WHERE agent=? ORDER BY ts DESC LIMIT 1", (agent,))
    return {"ts": r["ts"], "key": r["key"], "params": db.loads(r["params"], {}), "level": r["level"]} if r else None


def agents() -> list[dict]:
    en = settings.get("agents_enabled") or {}
    live_runs = [r for r in tasks._RUNS.values() if not r.done]
    t0 = _today0()
    out = []
    for meta in active_agents():
        k = meta["key"]
        st = {"key": k, "step": meta["step"], "enabled": bool(en.get(k)), "status": "idle", "short": None, "now": None}
        if meta["step"] >= 3 and k in ("sales", "followup"):
            st.update(status="off", short={"key": "c.step", "params": {"n": 3}})
            out.append(st)
            continue
        if not st["enabled"]:
            st.update(status="off", short={"key": "st.off", "params": {}})
            out.append(st)
            continue
        if not llm.configured(k):            # 还没配置 API：不运行，卡片显示“未配置”
            st.update(status="off", short={"key": "wb.s.unconfigured", "params": {}}, unconfigured=True)
            out.append(st)
            continue
        for run in live_runs:
            a = run.agent_state.get(k)
            if a and a.get("state") in LIVE_STATES and a.get("state") != "done":
                st["status"] = a["state"]
                st["now"] = {"key": a.get("key"), "params": a.get("params") or {}, "ts": time.time()}
                break
        if not st["now"]:
            st["now"] = _latest_event(k)
        if k == "researcher":
            run = tasks.running("research")
            st["short"] = ({"key": "wb.s.found", "params": {"n": run.stats.get("pool", 0)}} if run
                           else {"key": "st.idle", "params": {}})
        elif k == "monitor":
            nxt = _next_monitor()
            if not tasks.tg_tasks():
                st["short"] = {"key": "wb.s.no_tg_task", "params": {}}   # 只在勾选了 TG 群的任务运行时读群
            elif tasks.running("monitor"):
                st["short"] = {"key": "wb.s.reading", "params": {}}
            elif nxt:
                st["short"] = {"key": "wb.s.next", "params": {"time": time.strftime("%H:%M", time.localtime(nxt))}}
            else:
                st["short"] = {"key": "wb.s.no_groups", "params": {}}
        elif k == "analyst":
            n = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND created_at>=?", (t0,))["n"]
            st["short"] = {"key": "wb.s.qualified", "params": {"n": n}}
        elif k == "manager":
            if leads.review_on():
                pend = _review_pending()
                st["short"] = ({"key": "wb.s.review", "params": {"n": pend}} if pend
                               else {"key": "wb.s.chat", "params": {}})
                if st["status"] == "idle" and pend:
                    st["status"] = "waiting"
            else:
                # v1 没有审核环节：显示今天推送给销售的数量 / 还没推送的合格线索
                sent = db.query_one("SELECT COUNT(*) n FROM leads WHERE pushed_at>=?", (t0,))["n"]
                unsent = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND status='qualified'")["n"]
                st["short"] = ({"key": "wb.s.pushed", "params": {"n": sent}} if sent else
                               {"key": "wb.s.to_push", "params": {"n": unsent}} if unsent else {"key": "wb.s.chat", "params": {}})
        elif k == "scout":
            # 渠道拓展员（第 3 步完整上线）：第 2 步由“AI 找群”作业驱动
            sug = db.query_one("SELECT COUNT(*) n FROM tg_discoveries WHERE status='suggested'")["n"]
            st["short"] = ({"key": "wb.s.suggested", "params": {"n": sug}} if sug else {"key": "c.step", "params": {"n": 3}})
        out.append(st)
    return out


def _review_pending() -> int:
    return db.query_one("SELECT COUNT(*) n FROM leads WHERE status='pending_review' AND "
                        "(review_later_until IS NULL OR review_later_until<?)", (time.time(),))["n"]


def overview() -> dict:
    b = llm.budget_state()
    from . import alerts
    return {
        "goal": goal(),
        "search_guide": search_guide(),       # U16：每次都现算（配好 Key 后马上消失，不用重启）
        "budget": {"day_used": round(b["spent"]["today"]["cost"], 4), "day_limit": b["budget"]["daily"],
                   "month_used": round(b["spent"]["month"]["cost"], 4), "month_limit": b["budget"]["monthly"],
                   "paused": b["paused"], "period": b["period"], "custom": bool(b["budget"].get("set_by_user"))},
        "agents": (ags := agents()),
        "unconfigured": sum(1 for a in ags if a.get("unconfigured")),
        "review_pending": _review_pending() if leads.review_on() else 0,
        "features": {"review": leads.review_on()},
        "alerts": alerts.unread_count(),
        "paused_all": settings.ai_paused(),       # 菜单栏 / 托盘「暂停所有 AI」
    }


def feed(agent: str | None = None, after: float = 0, limit: int = 30) -> list[dict]:
    """某个 AI（或全部）最近的事件，新的在前。after=时间戳 → 只取更新的。"""
    sql, args = "SELECT task_id, ts, agent, level, key, params FROM task_events WHERE ts>?", [after or 0]
    if agent:
        sql += " AND agent=?"
        args.append(agent)
    rows = db.query(sql + " ORDER BY ts DESC LIMIT ?", (*args, max(1, min(limit, 200))))
    return [{"task_id": r["task_id"], "ts": r["ts"], "agent": r["agent"], "level": r["level"], "key": r["key"],
             "params": db.loads(r["params"], {})} for r in rows]

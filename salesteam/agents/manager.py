"""Confirmed daily lead goals. Plans snapshot user settings; no sales or outreach.

One persistent plan per local calendar day. Task parent IDs reserve each attempt
so restart after task registration cannot dispatch the same attempt twice.
"""
import re
import threading
import time
from functools import wraps

from .. import db, llm, settings, tasks
from ..errors import UserError
from . import schemas

_lock = threading.RLock()
DEFAULT = {"enabled": False, "icp_id": None, "target": 10, "sources": ["google"],
           "time_of_day": "09:00", "max_retries": 1}
COMMAND_SCHEMA = schemas.obj({"goal": schemas.obj({"enabled": schemas.B, "icp_id": schemas.I,
    "target": schemas.I, "sources": schemas.SL, "time_of_day": schemas.S, "max_retries": schemas.I}),
    "explanation": schemas.S})


def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from .. import background
        with background.operation("manager:" + fn.__name__):
            return fn(*args, **kwargs)
    return wrapped


def day(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise UserError("schedule_time_invalid")
    h, m = map(int, value.split(":"))
    if h > 23 or m > 59:
        raise UserError("schedule_time_invalid")
    return value


def integer(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise UserError("invalid_value", field=field)
    return value


def boolean(value, field):
    if not isinstance(value, bool):
        raise UserError("invalid_value", field=field)
    return value


def goal():
    return {**DEFAULT, **(settings.get("manager_goal", {}) or {})}


def validate_goal(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULT):
        raise UserError("invalid_value", field="goal")
    g = {**goal(), **data}
    boolean(g["enabled"], "enabled")
    integer(g["target"], "target", 1, 500)
    integer(g["max_retries"], "max_retries", 0, 2)
    parse_time(g["time_of_day"])
    src = g["sources"]
    if not isinstance(src, list) or not src or any(s not in tasks.SOURCES for s in src):
        raise UserError("invalid_value", field="sources")
    g["sources"] = list(dict.fromkeys(src))
    if g["icp_id"] is None and not g["enabled"]:
        return g
    integer(g["icp_id"], "icp_id", 1, 2**31 - 1)
    tasks.validate_research(g, check_agents=False)
    return g


@protected
def set_goal(data):
    with _lock:
        g = validate_goal(data)
        settings.put("manager_goal", g)
        return g


def blocked(g=None):
    g = g or goal()
    if not g["enabled"]:
        return "disabled"
    if settings.ai_paused():
        return "ai_paused"
    if not settings.agent_enabled("manager"):
        return "agent_disabled"
    if not llm.configured("manager"):
        return "agent_not_configured"
    if llm.budget_state()["paused"]:
        return "budget_exceeded"
    try:
        need = ["manager"] + (["researcher"] if any(s in tasks.WEB_SOURCES for s in g["sources"]) else [])
        if "tg" in g["sources"]:
            need.append("monitor")
        if settings.agent_enabled("analyst"):
            need.append("analyst")
        for a in need:
            llm.check_budget(a, llm.get_config(a))
        tasks.validate_research(g)
    except UserError as exc:
        return exc.key
    return ""


def _task_rows(pid):
    return db.query("WITH RECURSIVE tree(id) AS (SELECT id FROM tasks WHERE created_by='manager' AND parent_id LIKE ? "
                    "UNION SELECT t.id FROM tasks t JOIN tree p ON t.parent_id=p.id) "
                    "SELECT t.* FROM tasks t JOIN tree ON tree.id=t.id ORDER BY t.created_at,t.id", (f"manager:{pid}:%",))


def _public(r):
    r = dict(r)
    r["goal"] = db.loads(r["goal"], {})
    rows = _task_rows(r["id"])
    r["task_ids"] = [t["id"] for t in rows]
    r["attempts"] = sum(t["created_by"] == "manager" for t in rows)
    r["tasks"] = [{"id": t["id"], "status": t["status"], "result": t["result"], "error": t["error"],
                   "stats": db.loads(t["stats"], {})} for t in rows]
    start = time.mktime(time.strptime(r["day"], "%Y-%m-%d"))
    next_date = time.localtime(start + 36 * 3600)[:3]
    end = time.mktime(next_date + (0, 0, 0, 0, 0, -1))
    ids = r["task_ids"]
    r["qualified"] = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND created_at>=? AND created_at<? "
        "AND task_id IN (" + ",".join("?" for _ in ids) + ")", (start, end, *ids))["n"] if ids else 0
    r["remaining"] = max(0, r["goal"].get("target", 0) - r["qualified"])
    return r


def plans(day=None):
    rows = db.query("SELECT * FROM manager_plans WHERE day=? ORDER BY id", (day,)) if day else db.query(
        "SELECT * FROM manager_plans ORDER BY day DESC LIMIT 30")
    return [_public(r) for r in rows]


def report(day=None):
    date = day or globals()["day"]()
    row = db.query_one("SELECT data FROM manager_reports WHERE day=?", (date,))
    return db.loads(row["data"], {}) if row else None


def _report(p, ts):
    counts = {"processed": 0, "qualified": 0, "backup": 0, "discarded": 0, "errors": 0}
    for t in p["tasks"]:
        for k in counts:
            counts[k] += int(t["stats"].get(k) or 0)
    counts["qualified"] = p["qualified"]
    data = {"day": p["day"], "target": p["goal"]["target"], "qualified": p["qualified"],
            "remaining": p["remaining"], "status": p["status"], "attempts": p["attempts"],
            "sources": p["goal"]["sources"], "icp_id": p["goal"]["icp_id"], "counts": counts,
            "tasks": p["tasks"], "last_error": p["last_error"], "updated_at": ts,
            "notification_sent": False}
    from . import scout
    data["tg_source_pool"] = scout.pool_report(p["day"])
    try:
        from .. import source_pool
        data["source_pool"] = source_pool.summary()
    except ImportError:
        pass
    db.execute("INSERT INTO manager_reports(day,data,updated_at) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE "
               "SET data=excluded.data,updated_at=excluded.updated_at", (p["day"], db.dumps(data), ts))
    return data


def state():
    return {"goal": goal(), "plans": plans(), "blocked": blocked(),
            "commands": [{**r, "proposal": db.loads(r["proposal"], {})} for r in db.query(
                "SELECT * FROM manager_commands WHERE status='pending' ORDER BY id DESC LIMIT 10")]}


@protected
def tick(ts=None):
    ts = time.time() if ts is None else ts
    today = day(ts)
    started = []
    with _lock:
        g = goal()
        # Reports remain available and truthful while disabled or paused.
        for old in db.query("SELECT * FROM manager_plans WHERE day<? AND status NOT IN ('met','exhausted','day_ended')", (today,)):
            db.execute("UPDATE manager_plans SET status='day_ended',updated_at=? WHERE id=?", (ts, old["id"]))
            _report(_public(db.query_one("SELECT * FROM manager_plans WHERE id=?", (old["id"],))), ts)
        r = db.query_one("SELECT * FROM manager_plans WHERE day=?", (today,))
        if not r:
            if blocked(g) or time.strftime("%H:%M", time.localtime(ts)) < g["time_of_day"]:
                return []
            db.execute("INSERT OR IGNORE INTO manager_plans(day,goal,created_at,updated_at) VALUES(?,?,?,?)",
                       (today, db.dumps(g), ts, ts))
            r = db.query_one("SELECT * FROM manager_plans WHERE day=?", (today,))
        p = _public(r)
        reason = blocked({**p["goal"], "enabled": g["enabled"]})
        if not reason and (g["sources"] != p["goal"]["sources"] or g["icp_id"] != p["goal"]["icp_id"]):
            reason = "goal_changed"
        active = any(t["status"] in ("running", "queued") for t in p["tasks"])
        if p["remaining"] == 0:
            status = "met"
        elif active:
            status = "running"
        elif reason:
            status = "blocked"
        elif p["attempts"] >= 1 + p["goal"]["max_retries"]:
            status = "exhausted"
        else:
            status = "pending"
        db.execute("UPDATE manager_plans SET status=?,last_error=?,attempts=?,updated_at=? WHERE id=?",
                   (status, reason, p["attempts"], ts, p["id"]))
        if status == "pending":
            attempt = p["attempts"] + 1
            parent = f"manager:{p['id']}:{attempt}"
            try:
                with tasks.registry_lock():
                    existing = db.query_one("SELECT id FROM tasks WHERE parent_id=? AND created_by='manager'", (parent,))
                    if not existing:
                        t = tasks.create_task({**p["goal"], "target": p["remaining"], "manager_query_offset": attempt - 1}, created_by="manager",
                                              parent_id=parent, goal_date=today)
                        started.append(t["id"])
                db.execute("UPDATE manager_plans SET status='running',attempts=?,last_error='',updated_at=? WHERE id=?",
                           (attempt, ts, p["id"]))
            except UserError as exc:
                db.execute("UPDATE manager_plans SET status='blocked',last_error=?,updated_at=? WHERE id=?",
                           (exc.key, ts, p["id"]))
        _report(_public(db.query_one("SELECT * FROM manager_plans WHERE id=?", (p["id"],))), ts)
    return started


@protected
def command(message):
    message = str(message or "").strip()
    if not message or len(message) > 4000:
        raise UserError("invalid_value", field="message")
    if settings.ai_paused():
        raise UserError("ai_paused")
    if not settings.agent_enabled("manager"):
        raise UserError("agent_disabled", agent="@agent.manager")
    if not llm.configured("manager"):
        raise UserError("agent_not_configured", agent="@agent.manager")
    system = ("Extract a daily lead research goal for explicit user confirmation. Do not execute it. "
              "Only modify fields the user requests. Preserve other fields in current goal. "
              "No sales, messages, email accounts, budgets, safety limits or filtering changes. "
              "Only google/web/youtube/tg sources. HH:MM uses the user's machine local time. "
              "Use a listed existing ICP id; never invent a template or promise unconfigured channels.")
    templates = db.query("SELECT id,name FROM icp_templates ORDER BY id")
    current = goal()
    data, _ = llm.call_json("manager", "manager_goal", system,
        db.dumps({"message": message, "current_goal": current, "templates": templates}), COMMAND_SCHEMA,
        context={"message": message, "current_goal": current, "templates": templates})
    proposal = {"goal": validate_goal(data.get("goal") or {}), "explanation": str(data.get("explanation") or "")[:2000]}
    cid = db.execute("INSERT INTO manager_commands(message,proposal,created_at) VALUES(?,?,?)",
                     (message, db.dumps(proposal), db.now()))
    return {"id": cid, "proposal": proposal, "status": "pending"}


@protected
def confirm_command(cid):
    with _lock:
        r = db.query_one("SELECT * FROM manager_commands WHERE id=?", (cid,))
        if not r:
            raise UserError("invalid_value", field="command")
        if r["status"] == "confirmed":
            return {"id": cid, "status": "confirmed", "goal": goal()}
        if r["status"] != "pending":
            raise UserError("invalid_value", field="command")
        proposal = db.loads(r["proposal"], {})
        g = validate_goal(proposal["goal"])
        with db.transaction():
            settings.put("manager_goal", g)
            db.execute("UPDATE manager_commands SET status='confirmed',confirmed_at=? WHERE id=?", (db.now(), cid))
        return {"id": cid, "status": "confirmed", "goal": g}


@protected
def reject_command(cid):
    with _lock:
        db.execute("UPDATE manager_commands SET status='rejected' WHERE id=? AND status='pending'", (cid,))
        return {"id": cid, "status": "rejected"}

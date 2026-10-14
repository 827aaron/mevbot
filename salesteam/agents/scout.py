"""Opt-in daily TG discovery and measured channel retirement.

Uses existing TG leases, account warmup/caps and assisted-join queue. No message
monitoring is enabled here; actual reads still require a running TG lead task.
"""
import re
import threading
import time
from functools import wraps

from .. import channels, db, edition, llm, settings, tasks
from ..errors import UserError
from ..tg import joining, pool
from . import manager

_lock = threading.RLock()
_active_days = set()
DEFAULT = {"enabled": False, "icp_id": None, "time_of_day": "10:00", "auto_queue": False,
           "auto_retire": False, "auto_monitor": False, "min_age_days": 7, "min_sample": 10, "max_bad_ratio": 0.8}


def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from .. import background
        with background.operation("scout:" + fn.__name__):
            return fn(*args, **kwargs)
    return wrapped


def get_settings():
    default = {**DEFAULT, "auto_monitor": edition.current()["id"] == "my_igaming"}
    return {**default, **(settings.get("scout_settings", {}) or {})}


@protected
def set_settings(data):
    with _lock:
        if not isinstance(data, dict) or set(data) - set(DEFAULT) - {"confirmed"}:
            raise UserError("invalid_value", field="scout")
        old = get_settings()
        cfg = {**old, **{k: v for k, v in data.items() if k in DEFAULT}}
        for k in ("enabled", "auto_queue", "auto_retire", "auto_monitor"):
            manager.boolean(cfg[k], k)
        manager.parse_time(cfg["time_of_day"])
        manager.integer(cfg["min_age_days"], "min_age_days", 1, 90)
        manager.integer(cfg["min_sample"], "min_sample", 5, 1000)
        value = cfg["max_bad_ratio"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 <= value <= 1:
            raise UserError("invalid_value", field="max_bad_ratio")
        if cfg["icp_id"] is not None:
            manager.integer(cfg["icp_id"], "icp_id", 1, 2**31 - 1)
            from . import icp
            icp.get(cfg["icp_id"])
        elif cfg["enabled"]:
            raise UserError("task_icp_missing")
        joining_cfg = settings.get("joining")
        needs_risk = cfg["auto_queue"] and (not old["auto_queue"] or not joining_cfg.get("assisted_join"))
        if needs_risk and data.get("confirmed") is not True:
            raise UserError("risk_confirm_required")
        with db.transaction():
            if needs_risk:
                joining_cfg["assisted_join"] = True
                settings.put("joining", joining_cfg)
                settings.log_risk("joining.assisted_join", True, "Automatic queue explicitly confirmed")
                settings.log_risk("scout.auto_queue", True, "Public business agent source signals in MY edition; general score>=70. Account limits and warmup apply")
            settings.put("scout_settings", cfg)
        if old["enabled"] and old["auto_monitor"] and (not cfg["enabled"] or not cfg["auto_monitor"] or cfg["icp_id"] != old["icp_id"]):
            for row in db.query("SELECT id FROM tasks WHERE created_by='scout' AND status='running'"):
                tasks.cancel(row["id"])
                settings.put("scout_collection_state", {"day": manager.day(), "task_id": row["id"], "blocked": "disabled", "toggle_cancelled": row["id"]})
        return cfg


def blocked(cfg=None):
    cfg = cfg or get_settings()
    if not cfg["enabled"]:
        return "disabled"
    if settings.ai_paused():
        return "ai_paused"
    if not settings.agent_enabled("scout"):
        return "agent_disabled"
    if not llm.configured("scout"):
        return "agent_not_configured"
    if llm.budget_state()["paused"]:
        return "budget_exceeded"
    if not pool.scout_accounts():
        return "tg_no_active_account"
    try:
        llm.check_budget("scout", llm.get_config("scout"))
        from . import icp
        icp.get(cfg["icp_id"])
    except UserError as exc:
        return exc.key
    return ""


def suggestions(cfg=None, ts=None):
    """Conservative: enough observed leads, old enough, no qualified outcomes."""
    cfg = cfg or get_settings()
    ts = time.time() if ts is None else ts
    rows = channels.list_all(status="monitored")
    out = []
    for c in rows:
        if cfg.get("icp_id"):
            counts = db.query_one("SELECT COUNT(*) leads,COALESCE(SUM(qualified=1),0) qualified, "
                                  "COALESCE(SUM(feedback=-1),0) bad FROM leads WHERE channel_id=? AND icp_id=?",
                                  (c["id"], cfg["icp_id"]))
            c.update(counts)
        if (c["kind"] in ("tg_group", "tg_channel") and ts - c["created_at"] >= cfg["min_age_days"] * 86400
                and c["leads"] >= cfg["min_sample"] and c["qualified"] == 0
                and c["bad"] / c["leads"] >= cfg["max_bad_ratio"]):
            out.append({"id": c["id"], "name": c["name"], "leads": c["leads"], "qualified": c["qualified"],
                        "bad": c["bad"], "reason": "low_output"})
    return out


@protected
def status():
    _recover_interrupted()
    return {"settings": get_settings(), "blocked": blocked(), "pool_report": pool_report(),
            "runs": [{**r, "result": db.loads(r["result"], {})} for r in
                     db.query("SELECT * FROM scout_runs ORDER BY day DESC LIMIT 30")],
            "suggestions": suggestions()}


@protected
def run_once(day, cfg=None):
    cfg = cfg or get_settings()
    reason = blocked(cfg)
    if reason:
        db.execute("UPDATE scout_runs SET status='blocked',error=?,updated_at=? WHERE day=?", (reason, db.now(), day))
        return {"blocked": reason}
    try:
        result = joining.discover(cfg["icp_id"], auto_queue=False)
        # Re-read user opt-ins: discovery may take minutes; toggles can change.
        current = get_settings()
        changed = current["icp_id"] != cfg["icp_id"]
        if settings.ai_paused() or not current["enabled"] or changed:
            reason = "goal_changed" if changed else "disabled" if not current["enabled"] else "ai_paused"
            db.execute("UPDATE scout_runs SET status='blocked',error=?,updated_at=? WHERE day=?", (reason, db.now(), day))
            return {**result, "queued": 0, "retired": [], "blocked": reason}
        queued = int(result.get("queued") or 0) + (joining.auto_queue_discoveries(cfg["icp_id"]) if current["auto_queue"] else 0)
        suggested = suggestions(current)
        retired = []
        if current["auto_retire"]:
            for c in suggested:
                channels.set_status(c["id"], "retired")
                retired.append(c["id"])
        result = {**result, "icp_id": cfg["icp_id"], "queued": queued, "retired": retired, "suggestions": suggested}
        result.update(_join_report(day, cfg["icp_id"]))
        result["collection"] = collection_state(day, cfg)
        db.execute("UPDATE scout_runs SET status='done',result=?,error='',updated_at=? WHERE day=?",
                   (db.dumps(result), db.now(), day))
        return result
    except UserError as exc:
        db.execute("UPDATE scout_runs SET status='failed',error=?,updated_at=? WHERE day=?", (exc.key, db.now(), day))
        return {"error": exc.key}
    except Exception:
        db.execute("UPDATE scout_runs SET status='failed',error='unexpected',updated_at=? WHERE day=?", (db.now(), day))
        raise


def _recover_interrupted():
    with _lock:
        for r in db.query("SELECT day FROM scout_runs WHERE status='running'"):
            if r["day"] not in _active_days:
                db.execute("UPDATE scout_runs SET status='interrupted',error='interrupted',updated_at=? WHERE day=?", (db.now(), r["day"]))


def _worker(date, cfg):
    try:
        return run_once(date, cfg)
    finally:
        with _lock:
            _active_days.discard(date)


@protected
def tick(ts=None):
    """Claim today's discovery durably before starting a worker (never rerun on restart)."""
    from .. import background
    ts = time.time() if ts is None else ts
    date = manager.day(ts)
    _recover_interrupted()
    cfg = get_settings()
    if cfg["enabled"] and time.strftime("%H:%M", time.localtime(ts)) >= cfg["time_of_day"]:
        ensure_collection(date, cfg)
    if blocked(cfg) or time.strftime("%H:%M", time.localtime(ts)) < cfg["time_of_day"]:
        return None
    with _lock:
        if db.query_one("SELECT day FROM scout_runs WHERE day=?", (date,)):
            return None
        db.execute("INSERT INTO scout_runs(day,status,created_at,updated_at) VALUES(?,'running',?,?)", (date, ts, ts))
        _active_days.add(date)
        try:
            background.spawn(_worker, date, cfg, name="scout-" + date)
        except Exception:
            _active_days.discard(date)
            db.execute("UPDATE scout_runs SET status='failed',error='unexpected',updated_at=? WHERE day=?", (ts, date))
            raise
    return date


def _join_report(date, icp_id):
    start = time.mktime(time.strptime(date, "%Y-%m-%d"))
    end = time.mktime(time.localtime(start + 36 * 3600)[:3] + (0, 0, 0, 0, 0, -1))
    rows = db.query("SELECT * FROM tg_join_queue WHERE icp_id=? AND created_at>=? AND created_at<?", (icp_id, start, end))
    monitored = 0
    for row in rows:
        if row["status"] != "joined":
            continue
        active = db.query_one("SELECT id FROM tg_chats WHERE id=? AND monitored=1 AND left_at IS NULL", (row.get("tg_chat_id"),))
        if not row.get("tg_chat_id") and not re.search(r"/(?:\+|joinchat/)", row["link"] or ""):
            username = (row["link"] or "").rstrip("/").split("/")[-1]
            active = db.query_one("SELECT id FROM tg_chats WHERE LOWER(username)=LOWER(?) AND monitored=1 AND left_at IS NULL", (username,))
        if active:
            monitored += 1
    return {"joined": sum(r["status"] == "joined" for r in rows), "monitored": monitored,
            "failed": sum(r["status"] == "failed" for r in rows),
            "join_pending": sum(r["status"] == "pending" for r in rows),
            "join_needs_approval": sum(r["status"] == "needs_approval" for r in rows),
            "join_errors": sum(bool(r["error"]) for r in rows)}


def collection_state(date=None, cfg=None):
    date, cfg = date or manager.day(), cfg or get_settings()
    row = db.query_one("SELECT id,status,error FROM tasks WHERE created_by='scout' AND goal_date=? AND icp_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (date, cfg["icp_id"]))
    if row:
        return {"task_id": row["id"], "status": row["status"], "blocked": row["error"] or ""}
    return {"task_id": None, "status": "blocked", "blocked": settings.get("scout_collection_state", {}).get("blocked", "not_started")}


@protected
def ensure_collection(date=None, cfg=None):
    """Independent of lead goals and discovery success, bounded by today's end."""
    cfg, date = cfg or get_settings(), date or manager.day()
    if edition.current()["id"] != "my_igaming" or not cfg["enabled"] or not cfg["auto_monitor"]:
        return None
    try:
        if settings.ai_paused():
            raise UserError("ai_paused")
        if not pool.monitor_accounts():
            raise UserError("tg_no_active_account")
        if llm.budget_state()["paused"]:
            raise UserError("budget_exceeded")
        llm.check_budget("monitor", llm.get_config("monitor"))
        with tasks.registry_lock():
            row = db.query_one("SELECT id,status FROM tasks WHERE created_by='scout' AND goal_date=? AND icp_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (date, cfg["icp_id"]))
            if row:
                state = settings.get("scout_collection_state", {}) or {}
                paused = db.query_one("SELECT 1 FROM task_events WHERE task_id=? AND key='ev.ai_paused_all' LIMIT 1", (row["id"],))
                resume = row["status"] == "interrupted" or (row["status"] == "cancelled" and (paused or state.get("toggle_cancelled") == row["id"]))
                if not resume:
                    return row["id"]
            params = {"icp_id": cfg["icp_id"], "sources": ["tg"], "target": 1, "continuous_monitor": True}
            if row:
                params["resume_from"] = row["id"]
            task = tasks.create_task(params,
                                     created_by="scout", parent_id=row["id"] if row else "scout:" + date, goal_date=date)
            settings.put("scout_collection_state", {"day": date, "task_id": task["id"], "blocked": ""})
            return task["id"]
    except UserError as exc:
        settings.put("scout_collection_state", {"day": date, "task_id": None, "blocked": exc.key})
        return None


def pool_report(date=None):
    cfg = get_settings()
    row = db.query_one("SELECT * FROM scout_runs WHERE day=?", (date,)) if date else db.query_one("SELECT * FROM scout_runs ORDER BY day DESC LIMIT 1")
    date = date or (row["day"] if row else manager.day())
    result = db.loads(row["result"], {}) if row else {}
    cfg = {**cfg, "icp_id": result.get("icp_id", cfg["icp_id"])}
    return {"day": date, "discovered": result.get("discovered", result.get("added", 0)),
            "deduplicated": result.get("deduplicated", 0), "queued": result.get("queued", 0),
            **_join_report(date, cfg["icp_id"]), "collection": collection_state(date, cfg),
            "discovery_status": row["status"] if row else "blocked", "blocked": row["error"] if row else blocked(cfg)}

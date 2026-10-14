"""Opt-in daily TG discovery and measured channel retirement.

Uses existing TG leases, account warmup/caps and assisted-join queue. No message
monitoring is enabled here; actual reads still require a running TG lead task.
"""
import threading
import time
from functools import wraps

from .. import channels, db, llm, settings
from ..errors import UserError
from ..tg import joining, pool
from . import manager

_lock = threading.RLock()
_active_days = set()
DEFAULT = {"enabled": False, "icp_id": None, "time_of_day": "10:00", "auto_queue": False,
           "auto_retire": False, "min_age_days": 7, "min_sample": 10, "max_bad_ratio": 0.8}


def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from .. import background
        with background.operation("scout:" + fn.__name__):
            return fn(*args, **kwargs)
    return wrapped


def get_settings():
    return {**DEFAULT, **(settings.get("scout_settings", {}) or {})}


@protected
def set_settings(data):
    with _lock:
        if not isinstance(data, dict) or set(data) - set(DEFAULT) - {"confirmed"}:
            raise UserError("invalid_value", field="scout")
        old = get_settings()
        cfg = {**old, **{k: v for k, v in data.items() if k in DEFAULT}}
        for k in ("enabled", "auto_queue", "auto_retire"):
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
                settings.log_risk("joining.assisted_join", True, "High-score automatic queue explicitly confirmed")
                settings.log_risk("scout.auto_queue", True, "Only score >=70; account limits and warmup apply")
            settings.put("scout_settings", cfg)
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
    return {"settings": get_settings(), "blocked": blocked(),
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
        result = {**result, "queued": queued, "retired": retired, "suggestions": suggested}
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

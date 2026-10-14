"""任务的运行方式（PRD 5.1.1）：立即开始 / 定时（一次）/ 每天。

- 定时：选日期 + 时间，到点开始一轮；每天：选时间，每天到点开始一轮，目标是每天的目标
- 每一轮跑到达到目标或当天 23:59（本机时间）为止（tasks.TaskRun.deadline）；勾了 TG 群的，TG 读取随这一轮开始和结束
- 到点时电脑在睡眠 / 关机：开机或唤醒后补跑这一轮（同一天只补一次，过了当天不补）。由调度器每次 tick 检查
- 新建 / 恢复 / 改时间时，今天的时间已经过了 → 今天不跑，从下一次开始（免得一保存就马上开跑）
- 模型简单、可复用：第 3 步销售主管也用 create(..., created_by="manager") 安排每天的任务
"""
import re
import threading
import time
from contextlib import contextmanager
from functools import wraps

from . import alerts, db, settings, tasks
from .errors import UserError

now = time.time          # 测试可以换成假时钟
_lock = threading.RLock()


@contextmanager
def execution_lock():
    """Reset and schedule operations share this lock, before the task/DB locks."""
    with _lock:
        yield


def _serialized(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapped


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _slot(ts: float, hhmm: str) -> float:
    """ts 那一天的 HH:MM（本机时间）。"""
    h, m = (int(x) for x in hhmm.split(":"))
    y, mo, d = time.localtime(ts)[:3]
    return time.mktime((y, mo, d, h, m, 0, 0, 0, -1))


def _parse_time(v) -> str:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise UserError("schedule_time_invalid")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_at(v) -> float:
    """「2026-10-15 09:00」/「2026-10-15T09:00」（本机时间）或时间戳。"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::\d{2})?\s*", str(v or ""))
    if not m:
        raise UserError("schedule_time_invalid")
    y, mo, d, h, mi = (int(x) for x in m.groups())
    try:
        return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        raise UserError("schedule_time_invalid")


def _row(sid: int) -> dict:
    r = db.query_one("SELECT * FROM task_schedules WHERE id=?", (sid,))
    if not r:
        raise UserError("schedule_not_found", 404)
    return r


def next_run(r: dict, ts: float | None = None) -> float | None:
    """下一次开始的时间（暂停 / 已结束的没有）。"""
    ts = ts or now()
    if r["status"] != "active":
        return None
    if r["mode"] == "once":
        return r["run_at"] if not r["last_day"] else None
    slot = _slot(ts, r["time_of_day"])
    if r["last_day"] != _day(ts):
        return slot if slot >= ts else ts           # 今天还没跑：到点（或错过了马上补）
    return _slot(ts + 86400, r["time_of_day"])      # 今天跑过了：明天


def public(r: dict) -> dict:
    p = db.loads(r["params"], {})
    icp = db.query_one("SELECT name FROM icp_templates WHERE id=?", (p.get("icp_id"),)) if p.get("icp_id") else None
    runs = db.query_one("SELECT COUNT(*) n FROM tasks WHERE schedule_id=?", (r["id"],))["n"]
    last = db.query_one("SELECT id, status, result FROM tasks WHERE id=?", (r["last_task_id"],)) if r["last_task_id"] else None
    return {"id": r["id"], "mode": r["mode"], "run_at": r["run_at"], "time_of_day": r["time_of_day"], "params": p,
            "icp_name": icp["name"] if icp else "", "status": r["status"], "next_run_at": next_run(r),
            "last_day": r["last_day"], "last_task": dict(last) if last else None, "last_error": r["last_error"],
            "runs": runs, "created_by": r["created_by"], "created_at": r["created_at"]}


def list_schedules() -> list[dict]:
    return [public(r) for r in db.query("SELECT * FROM task_schedules WHERE status<>'deleted' ORDER BY id DESC")]


def _when(mode: str, data: dict, ts: float) -> tuple[float | None, str, str]:
    """返回 (run_at, time_of_day, last_day)。今天的时间已经过了的每天任务 → last_day=今天（今天不跑）。"""
    if mode == "once":
        at = _parse_at(data.get("at"))
        if at < ts - 60:
            raise UserError("schedule_time_past")
        return at, "", ""
    if mode == "daily":
        tod = _parse_time(data.get("time"))
        return None, tod, _day(ts) if _slot(ts, tod) < ts - 60 else ""
    raise UserError("invalid_value", field="mode")


@_serialized
def create(params: dict, mode: str, data: dict, created_by: str = "user") -> dict:
    """安排一个定时 / 每天的找客户任务。params 和立即开始一样（模板、数据源、目标、网址）。"""
    clean = tasks.validate_research(params, check_agents=False)    # AI 有没有配置到点时再检查（可能那时才配好）
    ts = now()
    run_at, tod, last_day = _when(mode, data, ts)
    sid = db.execute("INSERT INTO task_schedules(mode, run_at, time_of_day, params, status, created_by, last_day, "
                     "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (mode, run_at, tod, db.dumps(clean), "active", created_by, last_day, ts, ts))
    return public(_row(sid))


@_serialized
def update(sid: int, data: dict) -> dict:
    """「修改」（参数 / 时间）和「暂停」/「恢复」（paused: true / false）。"""
    r = _row(sid)
    ts = now()
    sets, args = [], []
    if "params" in data:
        sets.append("params=?"); args.append(db.dumps(tasks.validate_research(data["params"], check_agents=False)))
    if "at" in data or "time" in data:
        run_at, tod, last_day = _when(r["mode"], data, ts)
        sets += ["run_at=?", "time_of_day=?"]; args += [run_at, tod]
        if r["mode"] == "once":
            sets += ["last_day=?", "status=?"]; args += ["", "active" if r["status"] != "paused" else "paused"]
        elif r["last_day"] != _day(ts):
            sets.append("last_day=?"); args.append(last_day)
    if "paused" in data:
        if data["paused"]:
            if r["status"] == "active":
                sets.append("status=?"); args.append("paused")
        elif r["status"] == "paused":
            st = "active"
            if r["mode"] == "once" and not r["last_day"] and (r["run_at"] or 0) < ts and _day(r["run_at"] or 0) != _day(ts):
                st = "missed"                                         # 定时的那天已经过去
            sets.append("status=?"); args.append(st)
            if r["mode"] == "daily" and r["last_day"] != _day(ts) and _slot(ts, r["time_of_day"]) < ts - 60:
                sets.append("last_day=?"); args.append(_day(ts))      # 恢复时今天的时间已过：从明天开始
    if sets:
        sets.append("updated_at=?"); args.append(ts)
        db.execute(f"UPDATE task_schedules SET {', '.join(sets)} WHERE id=?", (*args, sid))
    return public(_row(sid))


@_serialized
def delete(sid: int):
    """删除安排：以后不再开新的一轮；已经跑过的记录留在任务列表里；正在跑的这一轮不受影响（可以单独停止）。"""
    _row(sid)
    db.execute("DELETE FROM task_schedules WHERE id=?", (sid,))


@_serialized
def due(ts: float | None = None) -> list[dict]:
    """现在该开始一轮的安排。定时的那天整天没开机 → 标「错过」（过了当天不补）。"""
    ts = ts or now()
    today = _day(ts)
    out = []
    for r in db.query("SELECT * FROM task_schedules WHERE status='active' ORDER BY id"):
        if r["mode"] == "once":
            if r["last_day"] or ts < (r["run_at"] or 0):
                continue
            if _day(r["run_at"]) != today:
                db.execute("UPDATE task_schedules SET status='missed', updated_at=? WHERE id=?", (ts, r["id"]))
                continue
            out.append(r)
        elif r["last_day"] != today and ts >= _slot(ts, r["time_of_day"]):
            out.append(r)
    return out


@_serialized
def tick(ts: float | None = None) -> list[str]:
    """调度器每次 tick 调用：到点（或错过了、同一天补一次）的安排开一轮。返回新开的任务 ID。"""
    ts = ts or now()
    started = []
    for r in due(ts):
        if settings.ai_paused():
            break                                  # 「暂停所有 AI」：先不开，恢复后当天还会补
        today = _day(ts)
        try:
            t = tasks.create_task(db.loads(r["params"], {}), created_by="schedule", goal_date=today, schedule_id=r["id"])
        except UserError as e:
            if e.key == "ai_paused":
                break
            # 配置问题（模板没写完、AI 没配置 / 关了、同时运行的任务太多）：今天不再重试，发提醒
            db.execute("UPDATE task_schedules SET last_day=?, last_error=?, status=CASE WHEN mode='once' THEN 'done' "
                       "ELSE status END, updated_at=? WHERE id=?", (today, e.key, ts, r["id"]))
            alerts.raise_alert(f"schedule:{r['id']}:{today}", "general", "schedule_failed", link="#/run",
                               error=f"@errors.{e.key}")
            continue
        db.execute("UPDATE task_schedules SET last_day=?, last_task_id=?, last_error='', status=CASE WHEN mode='once' "
                   "THEN 'done' ELSE status END, updated_at=? WHERE id=?", (today, t["id"], ts, r["id"]))
        started.append(t["id"])
    return started

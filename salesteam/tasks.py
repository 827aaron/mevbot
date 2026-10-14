"""任务运行器。任务是唯一的“工作单位”（PRD 6.4「所有渠道规矩一样：开始任务才工作」）：
- research   找客户任务 = 调研员（Google / 网页 / YouTube）+ 监控员（勾选了「TG 群」时）→ 数据师（筛选/去重/打分/画像）。
             勾选了 TG 群的任务一直开着等群里的新求购，直到达到合格目标或客户点停止；可以同时运行多个任务。
- monitor    监控一轮（内部）= 监控员把已开启监控的群读一遍（每条消息只读一次），分别按每个正在运行、勾选了 TG 群的
             任务的模板判断，命中的交给那个任务（线索归那个任务，由它的数据师处理）。没有这样的任务就不读。
- onboarding 入职向导 = 通读官网 + PDF → 提取要点 → 汇总画像（PRD 3.0）
除 research 外，每种任务同一时间最多运行一个。

- 实时事件：每条事件 = {seq, ts, agent, level, key, params}，key 是 i18n 文案键，前端翻译成中/英文
- 事件同时存入 task_events（刷新页面/重开软件后仍可回看）
- 取消：设置 cancel 标志，正在进行的单次 AI 调用结束后停止
- 第 3 步销售主管通过 create_task(..., created_by="manager", parent_id=..., goal_date=...) 下任务
"""
import queue
import threading
import time
import traceback
import uuid
from contextlib import contextmanager

from . import background, db, settings
from .errors import BudgetExceeded, UserError

SOURCES = ["google", "web", "youtube", "tg"]   # 以后加 facebook（第 4 步）/ instagram / customs
WEB_SOURCES = ("google", "web", "youtube")      # 调研员负责的来源；tg 由监控员负责
MAX_RUNNING_RESEARCH = 5                        # 程序安全上限：同时运行的找客户任务
_RUNS: dict[str, "TaskRun"] = {}
_lock = threading.RLock()


@contextmanager
def registry_lock():
    """Serialize task creation, pause and reset; acquire before the database lock."""
    with _lock:
        yield


def set_paused(on: bool):
    """Persist pause and signal every active AI task before another can register."""
    with _lock:
        settings.put("pause_all", {"on": on, "at": time.time()})
        if on:
            for run in _RUNS.values():
                if not run.done and run.kind in ("research", "monitor", "pool"):
                    run.cancel_flag.set()
                    run.emit("system", "ev.ai_paused_all", level="warn")


class TaskRun:
    def __init__(self, row: dict):
        self.id = row["id"]
        self.kind = row["kind"]
        self.params = db.loads(row["params"], {})
        self.icp_id = row["icp_id"]
        self.target = row["target"]
        self.cancel_flag = threading.Event()
        self.target_reached = threading.Event()
        # 每一轮最多跑到当天结束（本机 23:59，PRD 5.1.1）；找客户任务才有
        self.day_over = threading.Event()
        self.deadline = day_end(row["created_at"]) if self.kind == "research" else None
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.seq = 0
        self.stats = {"pool": 0, "processed": 0, "qualified": 0, "backup": 0, "discarded": 0, "duplicate": 0,
                      "filtered": 0, "errors": 0, "pages": 0, "queries": 0, "messages": 0, "chats": 0,
                      "keyword_hits": 0, "prescreened": 0, "search_results": 0, "discovered": 0,
                      "source_status": {}, "search_diagnostics": [], "source_new_items": 0,
                      "source_unchanged_items": 0, "source_changed_items": 0}
        self.agent_state: dict[str, dict] = {}
        self.source_notes: dict[str, str] = {}     # 每个来源为什么没出结果（google: search_blocked …），任务结束时写进 stats
        self.done = False
        self.status = "running"
        self.lead_queue: queue.Queue = queue.Queue()
        self.stats_lock = threading.Lock()
        # 自愈（PRD 9.0）：心跳 + 步数/费用/重复动作守卫
        from .heal import StepGuard
        self.last_beat = time.time()
        self.guard = StepGuard(self.id, self.kind)
        self.restarted = False
        self.limit_hit = False

    def step(self, action: str, agent: str = "system") -> bool:
        """每做一步调用一次。重复动作 → 返回 False（调用方跳过、换方法）；到步数/费用上限 → 停止任务。"""
        self.last_beat = time.time()
        r = self.guard.step(action)
        if r == "repeat":
            self.emit(agent, "ev.loop_switched", level="warn", action=action[:120])
            return False
        if r == "limit":
            if not self.limit_hit:
                self.limit_hit = True
                self.emit("system", "ev.task_limit", level="error", steps=self.guard.steps,
                          cost=f"{self.guard.cost():.2f}")
            self.cancel_flag.set()
            return False
        return True

    # ---------------------------------------------------- 事件
    def emit(self, _agent: str, _key: str, level="info", state=None, **params):
        agent, key = _agent, _key
        self.last_beat = time.time()
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "ts": time.time(), "agent": agent, "level": level, "key": key,
                  "params": {k: v for k, v in params.items()}}
            self.events.append(ev)
            if state or agent not in self.agent_state or level in ("info", "warn", "error", "success"):
                st = self.agent_state.setdefault(agent, {})
                if state:
                    st["state"] = state
                st.setdefault("state", "working")
                st["key"], st["params"], st["level"] = key, ev["params"], level
            self.cond.notify_all()
        try:
            db.execute("INSERT INTO task_events(task_id, seq, ts, agent, level, key, params) VALUES(?,?,?,?,?,?,?)",
                       (self.id, ev["seq"], ev["ts"], agent, level, key, db.dumps(ev["params"])))
        except Exception:  # noqa: BLE001  事件写库失败不影响任务
            pass

    def bump(self, **delta):
        self.last_beat = time.time()
        with self.stats_lock:
            for k, v in delta.items():
                self.stats[k] = self.stats.get(k, 0) + v
            snap = dict(self.stats)
        db.execute("UPDATE tasks SET stats=? WHERE id=?", (db.dumps(snap), self.id))
        with self.cond:
            self.cond.notify_all()
        if snap["qualified"] >= self.target > 0 and not self.params.get("continuous_monitor"):
            self.target_reached.set()
        return snap

    def should_stop(self) -> bool:
        if self.deadline and time.time() >= self.deadline and not self.day_over.is_set():
            self.day_over.set()
            self.emit("system", "ev.task_day_end", level="warn")
        return self.cancel_flag.is_set() or self.target_reached.is_set() or self.day_over.is_set()

    def snapshot(self):
        return {"id": self.id, "status": self.status, "target": self.target, "stats": dict(self.stats),
                "agents": self.agent_state, "seq": self.seq}


# ------------------------------------------------------------------ 公共接口

def day_end(ts: float) -> float:
    """ts 那一天的本机 23:59:59。"""
    y, m, d = time.localtime(ts)[:3]
    return time.mktime((y, m, d, 23, 59, 59, 0, 0, -1))


def validate_research(params: dict, check_agents: bool = True) -> dict:
    """找客户任务的参数检查（立即开始和定时 / 每天共用，PRD 5.1.1）。返回整理好的参数。"""
    from .agents import icp as icp_mod
    icp_id = params.get("icp_id")
    try:
        icp_id = int(icp_id)
    except (TypeError, ValueError):
        raise UserError("task_icp_missing")
    icp_row = icp_mod.get(icp_id)
    # 画像关键信息未完成不能开始任务（PRD 3.0）
    missing = icp_mod.required_missing(icp_row["data"])
    if missing:
        raise UserError("profile_incomplete", fields=",".join(f"@icp.{m}" for m in missing))
    sources = [s for s in SOURCES if s in (params.get("sources") or [])]
    if not sources:
        raise UserError("task_sources_missing")
    need = (["researcher"] if any(s in WEB_SOURCES for s in sources) else []) + (["monitor"] if "tg" in sources else [])
    for a in (need if check_agents else []):
        if not settings.agent_enabled(a):
            raise UserError("agent_disabled", agent=f"@agent.{a}")
    from . import llm as _llm
    for a in (need + ["analyst"] if check_agents else []):   # 没配置 API 的 AI 不运行（数据师关闭时不用配置）
        if (a != "analyst" or settings.agent_enabled(a)) and not _llm.configured(a):
            raise UserError("agent_not_configured", agent=f"@agent.{a}")
    try:
        target = int(params.get("target") or 0)
    except (TypeError, ValueError):
        raise UserError("invalid_value", field="target")
    if not 1 <= target <= 500:
        raise UserError("task_target_range")
    urls = [u.strip() for u in (params.get("urls") or []) if u and u.strip()][:50]
    offset = params.get("manager_query_offset", 0)
    if type(offset) is not int or not 0 <= offset <= 2:
        raise UserError("invalid_value", field="manager_query_offset")
    return {"icp_id": icp_id, "sources": sources, "target": target, "urls": urls,
            "follow_links": bool(params.get("follow_links", True)), "manager_query_offset": offset}


def create_task(params: dict, created_by="user", parent_id=None, goal_date=None, kind="research",
                schedule_id=None) -> dict:
    # Validation must share the registration lock: a reset may delete its ICP,
    # or a pause may otherwise land between the check and registering the run.
    with _lock:
        return _create_task(params, created_by, parent_id, goal_date, kind, schedule_id)


def _create_task(params: dict, created_by, parent_id, goal_date, kind, schedule_id) -> dict:
    if kind in ("research", "monitor", "pool") and settings.ai_paused():
        raise UserError("ai_paused")
    if kind == "research":
        clean = validate_research(params)
        # Only internal daily source monitoring can run through the day without
        # a lead quota. HTTP/user research still follows its requested target.
        if created_by == "scout" and params.get("continuous_monitor") is True:
            from . import edition
            if edition.current()["id"] == "my_igaming" and clean["sources"] == ["tg"]:
                clean["continuous_monitor"] = True
                if parent_id and params.get("resume_from") == parent_id:
                    previous = db.query_one("SELECT created_by FROM tasks WHERE id=?", (parent_id,))
                    if previous and previous["created_by"] == "scout":
                        clean["resume_from"] = parent_id
        if created_by == "source_pool":
            # A bounded source-reading round has no customer lead quota.
            clean["target"] = 0
        if created_by != "manager":
            clean.pop("manager_query_offset", None)
        icp_id, target = clean["icp_id"], clean["target"]
    elif kind == "monitor":
        from . import llm as _llm
        if not _llm.configured("monitor"):
            raise UserError("agent_not_configured", agent="@agent.monitor")
        icp_id = params.get("icp_id")
        target = 0
        clean = {"icp_id": icp_id, "account_ids": params.get("account_ids") or [], "trigger": params.get("trigger", "")}
    elif kind == "pool":
        icp_id = None
        target = 0
        clean = {"trigger": params.get("trigger", "")}
    elif kind in ("onboarding", "model_eval"):
        icp_id = params.get("icp_id")
        target = 0
        clean = dict(params)
    else:
        raise UserError("invalid_value", field="kind")
    live = [r for r in _RUNS.values() if not r.done and r.kind == kind]
    if kind == "research":
        if len(live) >= MAX_RUNNING_RESEARCH:
            raise UserError("task_limit_running", n=MAX_RUNNING_RESEARCH)
    elif live:
        raise UserError("task_already_running")
    task_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    db.execute("INSERT INTO tasks(id, kind, icp_id, params, status, target, created_by, parent_id, goal_date, "
               "schedule_id, created_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (task_id, kind, icp_id, db.dumps(clean), "running", target, created_by, parent_id,
                goal_date, schedule_id, db.now(), db.now()))
    run = TaskRun(db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,)))
    _RUNS[task_id] = run
    background.spawn(_execute, run, name=f"task-{task_id}")
    return get_task(task_id)


def running(kind: str) -> "TaskRun | None":
    with _lock:
        return next((r for r in _RUNS.values() if not r.done and r.kind == kind), None)


def tg_tasks() -> list["TaskRun"]:
    """正在运行、勾选了「TG 群」的任务（还没达标、没被停止）。监控员只为它们读群。"""
    return [r for r in list(_RUNS.values()) if not r.done and r.kind == "research"
            and "tg" in (r.params.get("sources") or []) and not r.should_stop()]


def cancel(task_id: str):
    with _lock:
        run = _RUNS.get(task_id)
        if not run or run.done:
            raise UserError("task_not_running")
        run.cancel_flag.set()
        run.emit("system", "ev.cancel_requested", level="warn")


def get_run(task_id):
    return _RUNS.get(task_id)


def get_task(task_id: str) -> dict:
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise UserError("task_not_found", 404)
    d = dict(row)
    d["params"] = db.loads(row["params"], {})
    d["stats"] = db.loads(row["stats"], {})
    run = _RUNS.get(task_id)
    d["live"] = bool(run and not run.done)
    d["agents"] = run.agent_state if run else {}
    icp = db.query_one("SELECT name FROM icp_templates WHERE id=?", (row["icp_id"],)) if row["icp_id"] else None
    d["icp_name"] = icp["name"] if icp else ""
    d["cost"] = db.query_one("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE task_id=?", (task_id,))["c"]
    from . import llm as _llm
    d["cost_src"] = _llm.approx_src("task_id=?", (task_id,))     # 有按参考价 / 估算算的 → 界面显示「约 $x（…）」
    return d


def list_tasks(limit=30, kind=None):
    if kind:
        rows = db.query("SELECT id FROM tasks WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit))
    else:
        rows = db.query("SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
    return [get_task(r["id"]) for r in rows]


def events_since(task_id: str, after: int) -> list[dict]:
    run = _RUNS.get(task_id)
    if run:
        with run.cond:
            return [e for e in run.events if e["seq"] > after]
    rows = db.query("SELECT * FROM task_events WHERE task_id=? AND seq>? ORDER BY seq", (task_id, after))
    return [{"seq": r["seq"], "ts": r["ts"], "agent": r["agent"], "level": r["level"], "key": r["key"],
             "params": db.loads(r["params"], {})} for r in rows]


def mark_interrupted():
    """软件重启时，把上次没跑完的任务标记为“已中断”。"""
    db.execute("UPDATE tasks SET status='interrupted', result='interrupted', finished_at=? WHERE status='running'",
               (db.now(),))


# ------------------------------------------------------------------ 执行

_budget_lock = threading.Lock()


def _budget_stop(run: TaskRun, e: BudgetExceeded, agent: str):
    """总预算到达上限 → 全部智能体暂停；（第 3 步）单个智能体分配额度用完 → 只停该智能体。"""
    with _budget_lock:
        if e.key == "budget_exceeded":
            if not run.cancel_flag.is_set():
                run.cancel_flag.set()
                for ag in ("researcher", "analyst"):
                    run.agent_state.setdefault(ag, {})["state"] = "paused"
                run.emit("system", "ev.budget_paused_all", level="error", **e.params)
            from . import alerts
            alerts.raise_alert(f"budget:{e.params.get('period')}:{time.strftime('%Y-%m-%d')}", "urgent", "budget",
                               link="#/settings/budget",
                               **{**e.params, "period": f"@period.{e.params.get('period')}"})
        else:
            run.emit(agent, "ev.agent_paused_budget", level="error", state="paused", **e.params)
            run.cancel_flag.set()


@background.tracked("task")
def _execute(run: TaskRun):
    from . import heal
    heal.set_current(f"task:{run.id}")
    if run.kind in ("onboarding", "model_eval", "pool"):
        return _execute_onboarding(run)
    if run.kind == "monitor":
        return _execute_monitor(run)
    return _execute_pipeline(run)


def _execute_monitor(run: TaskRun):
    """监控一轮：只读群、按各个任务的模板判断，命中的放进那个任务的线索队列（由那个任务的数据师处理）。"""
    from .agents import monitor
    status, result, error = "done", "monitor_done", ""
    run.emit("system", "ev.monitor_started")
    try:
        monitor.Monitor(run).execute()
    except BudgetExceeded as e:
        _budget_stop(run, e, "monitor")
        status, result = "stopped", "budget"
    except UserError as e:
        status, result, error = "failed", "error", e.key
        run.emit("monitor", "ev.agent_error", level="error", state="error", error=e.key, **e.params)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status, result, error = "failed", "error", "unexpected"
        run.emit("monitor", "ev.unexpected", level="error", state="error", detail=str(e)[:200])
    finally:
        if not run.restarted:
            if run.limit_hit:
                status, result = "stopped", "limit"
            elif run.cancel_flag.is_set() and result == "monitor_done":
                result = "cancelled"
            s = run.stats
            run.emit("system", "ev.monitor_finished", level="success", chats=s["chats"], messages=s["messages"],
                     hits=s["pool"], skipped=s.get("lang_skipped", 0), tasks=s.get("tasks", 0))
            run.status = status
            db.execute("UPDATE tasks SET status=?, result=?, error=?, stats=?, finished_at=? WHERE id=?",
                       (status, result, error, db.dumps(run.stats), db.now(), run.id))
            run.emit("system", "ev.task_closed", status=status)
            run.done = True


def _wait_for_tg(run: TaskRun):
    """勾选了 TG 群的任务：网页来源做完后不结束，等监控员每一轮送来的群消息线索，直到达标或客户点停止。"""
    from .tg import pool
    n = db.query_one("SELECT COUNT(*) n FROM tg_chats WHERE monitored=1 AND left_at IS NULL")["n"]
    run.emit("monitor", "ev.task_tg_waiting", level="info" if n else "warn", state="working", n=n,
             iv=f"@iv.{settings.get('monitor').get('interval', '1h')}")
    if not pool.monitor_accounts():
        run.emit("monitor", "ev.monitor_no_accounts", level="warn")
    while not run.should_stop():
        run.last_beat = time.time()            # 看门狗：等待中也算活着
        time.sleep(1)


def _execute_onboarding(run: TaskRun):
    from .agents import onboarding
    from .llm import model_eval
    status, result, error = "done", "done", ""
    try:
        if run.kind == "model_eval":
            model_eval.run(run)
        elif run.kind == "pool":
            from . import leadpool
            leadpool.run(run)
        elif run.params.get("member_url"):
            from .agents import site_login
            onboarding.member_read(run, run.params["onboarding_id"], run.params["member_url"],
                                   site_login.cookies_for(run.params.get("cookie_host", "")))
        else:
            onboarding.run_reading(run)
    except UserError as e:
        status, result, error = "failed", "error", e.key
        run.emit("system", "ev.agent_error", level="error", error=e.key, **e.params)
        if run.kind == "model_eval":
            model_eval._put(status="failed", error=e.key)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status, result, error = "failed", "error", "unexpected"
        run.emit("system", "ev.unexpected", level="error", detail=str(e)[:200])
        if run.kind == "model_eval":
            model_eval._put(status="failed", error="unexpected")
    finally:
        if not run.restarted:
            if run.cancel_flag.is_set() and status == "done":
                status, result = "cancelled", "cancelled"
            run.status = status
            db.execute("UPDATE tasks SET status=?, result=?, error=?, stats=?, finished_at=? WHERE id=?",
                       (status, result, error, db.dumps(run.stats), db.now(), run.id))
            if run.kind == "onboarding":
                try:
                    onboarding.task_ended(run, status, error)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            run.emit("system", "ev.task_closed", status=status)
            run.done = True


def _after_finish(run: TaskRun, result: str):
    """当天任务完成后，自动把线索推送到人工 TG（PRD 7.1）。"""
    if run.kind == "research" and result == "target_reached":
        try:
            from . import push
            background.spawn(push.auto_push, "task_done", name="task-push")
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _execute_pipeline(run: TaskRun):
    from .agents import analyst, icp as icp_mod, monitor, researcher
    result, status, error = "", "done", ""
    run.emit("system", "ev.task_started", target=run.target,
             sources=",".join(f"@source.{s}" for s in run.params["sources"]))
    srcs = run.params["sources"]
    producer_agent = "researcher" if any(s in WEB_SOURCES for s in srcs) else "monitor"
    try:
        icp_id = run.icp_id or monitor.default_icp_id()
        icp = icp_mod.effective_data(icp_id) if icp_id else {}
        run.icp_id = icp_id
        workers = []
        res_err: list = []

        def research():
            from . import heal
            heal.set_current(f"task:{run.id}")
            try:
                if any(s in WEB_SOURCES for s in srcs):
                    researcher.Researcher(run, icp).execute()
                if "tg" in srcs and not run.should_stop():
                    _wait_for_tg(run)
            except BudgetExceeded as e:
                res_err.append(e)
                _budget_stop(run, e, producer_agent)
            except UserError as e:
                res_err.append(e)
                run.emit(producer_agent, "ev.agent_error", level="error", state="error", error=e.key, **e.params)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                res_err.append(e)
                run.emit(producer_agent, "ev.unexpected", level="error", state="error", detail=str(e)[:200])
            finally:
                run.lead_queue.put(None)

        rt = background.spawn(research, name=f"research-{run.id}")
        n_workers = max(1, min(4, int(settings.get("analyst")["concurrency"])))
        # 数据师关闭（PRD 2.1.2）：线索不筛选打分，程序去重后直接进入审核台
        a = analyst.Analyst(run, icp) if settings.agent_enabled("analyst") else analyst.Passthrough(run, icp)
        stop_all = threading.Event()

        # 断点续跑：被看门狗重启的任务，把上一个任务没处理完的线索重新排队
        if run.params.get("resume_from"):
            for r in db.query("SELECT id FROM raw_leads WHERE task_id=? AND status='new'", (run.params["resume_from"],)):
                db.execute("UPDATE raw_leads SET task_id=? WHERE id=?", (run.id, r["id"]))
                run.lead_queue.put(r["id"])

        def analyse():
            from . import heal
            heal.set_current(f"task:{run.id}")
            while True:
                try:
                    item = run.lead_queue.get(timeout=0.5)
                except queue.Empty:
                    if stop_all.is_set():
                        return
                    continue
                if item is None:
                    stop_all.set()
                    run.lead_queue.put(None)   # 让其他 worker 也退出
                    return
                if run.cancel_flag.is_set():
                    continue
                if run.target_reached.is_set():
                    db.execute("UPDATE raw_leads SET reason='target_reached' WHERE id=? AND status='new'", (item,))
                    continue
                try:
                    a.process(item)
                except BudgetExceeded as e:
                    res_err.append(e)
                    _budget_stop(run, e, "analyst")
                except UserError as e:
                    run.bump(errors=1)
                    run.emit("analyst", "ev.agent_error", level="error", error=e.key, **e.params)
                    db.execute("UPDATE raw_leads SET status='error', reason=? WHERE id=?", (e.key, item))
                    if e.key in ("key_missing", "key_invalid", "model_not_found", "base_url_missing",
                                 "model_missing", "ollama_unreachable"):
                        res_err.append(e)
                        run.cancel_flag.set()
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    run.bump(errors=1)
                    run.emit("analyst", "ev.unexpected", level="error", detail=str(e)[:200])
                    db.execute("UPDATE raw_leads SET status='error', reason='unexpected' WHERE id=?", (item,))

        for _ in range(n_workers):
            t = background.spawn(analyse, name=f"analyse-{run.id}")
            workers.append(t)
        for t in workers:
            t.join()
        rt.join(timeout=5)

        s = run.stats
        budget = any(isinstance(e, BudgetExceeded) for e in res_err)
        config_err = next((e for e in res_err if isinstance(e, UserError) and not isinstance(e, BudgetExceeded)), None)
        if run.restarted:
            return
        if run.limit_hit:
            status, result = "stopped", "limit"
        elif run.target_reached.is_set():
            result = "target_reached"
        elif run.day_over.is_set() and not run.cancel_flag.is_set():
            result = "day_end"
        elif budget:
            status, result = "stopped", "budget"
        elif run.cancel_flag.is_set() and not config_err:
            status, result = "cancelled", "cancelled"
        elif config_err and s["qualified"] == 0 and s["processed"] == 0:
            status, result, error = "failed", "error", config_err.key
        elif not s.get("pool") and srcs and all(
                s.get("source_status", {}).get(src, {}).get("status") == "failed" for src in srcs):
            status, result = "failed", "error"
            error = next((s["source_status"][src].get("error") for src in srcs
                          if s["source_status"][src].get("error")), "search_failed")
        else:
            # 一个候选都没拿到 → “没搜到任何结果”（不是“数据源已用完”），并按来源说明原因（PRD §4）
            result = "no_results" if not s.get("pool") else "sources_exhausted"
        if run.source_notes:
            with run.stats_lock:
                run.stats["source_notes"] = dict(run.source_notes)
        run.emit("system", "ev.task_finished", level="success" if result == "target_reached" else "warn",
                 result=f"@result.{result}", qualified=s["qualified"], target=run.target, backup=s["backup"],
                 discarded=s["discarded"] + s["filtered"], duplicate=s["duplicate"])
        for ag in (producer_agent, "monitor", "analyst"):
            st = run.agent_state.get(ag)
            if st and st.get("state") not in ("paused", "error"):
                st["state"] = "done"
    except UserError as e:
        status, result, error = "failed", "error", e.key
        run.emit("system", "ev.agent_error", level="error", error=e.key, **e.params)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status, result, error = "failed", "error", "unexpected"
        run.emit("system", "ev.unexpected", level="error", detail=str(e)[:200])
    finally:
        if not run.restarted:
            run.status = status
            db.execute("UPDATE tasks SET status=?, result=?, error=?, stats=?, finished_at=? WHERE id=?",
                       (status, result, error, db.dumps(run.stats), db.now(), run.id))
            run.emit("system", "ev.task_closed", status=status)
            _after_finish(run, result)
            run.done = True

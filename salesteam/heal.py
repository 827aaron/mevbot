"""AI 卡住了怎么办——自愈（PRD 9.0）。原则：一个卡住不影响全队；每做完一小步保存进度；逐级上报。

- 超时：每次大模型调用在独立线程里执行，超时就放弃这次调用（重试一次 → 换备用模型 / 中转站）；等待期间持续写心跳
- 防原地打转：每个任务有最大步数和费用上限；同一个动作（同样的搜索词 / 网址）重复 N 次即停止这条路并记录“已换方法”
- 反复失败：某网站连续失败 / 要求人机验证 → 这个网站暂停 30 分钟后再试，其他工作照常
- 看门狗：所有任务和后台作业写心跳；看门狗（纯程序，不依赖任何模型）每分钟检查，发现没心跳 → 标记“卡住了”、
  用通知号直接发 TG（失败则桌面弹窗）、重启该作业并从保存的进度继续
- 看门狗对智能体是通用的：第 3 步的销售主管作业用 register_job() 注册即可（副主管接管见 agents/future.py）

这些都是程序内部参数（可由第 3 步销售主管在安全范围内调整），客户无需设置。
"""
import queue
import threading
import time
import traceback
from . import background

from . import db, settings

DEFAULTS = {
    "call_timeout_s": 300,       # 单次大模型调用最长等待
    "stuck_after_s": 180,        # 这么久没有心跳就算卡住
    "repeat_limit": 3,           # 同一个动作最多重复几次
    "domain_fail_limit": 3,      # 同一网站连续失败几次就暂停
    "domain_cooldown_min": 30,   # 暂停多久后再试
    "max_steps": {"research": 3000, "monitor": 20000, "onboarding": 2000, "model_eval": 500, "pool": 2000},
    "max_cost_usd": {"research": 8.0, "monitor": 5.0, "onboarding": 5.0, "model_eval": 3.0, "pool": 2.0},
}

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_beats: dict[str, float] = {}


def cfg() -> dict:
    c = dict(DEFAULTS)
    c.update(settings.get("heal", {}) or {})
    return c


# ------------------------------------------------------------------ 心跳

_local = threading.local()


def set_current(name: str | None):
    _local.name = name


def beat(name: str | None = None):
    n = name or getattr(_local, "name", None)
    if n:
        _beats[n] = time.time()


def last_beat(name: str) -> float:
    return _beats.get(name, 0.0)


# ------------------------------------------------------------------ 超时调用

class CallTimeout(Exception):
    pass


def call_with_timeout(fn, timeout: float | None = None, heartbeat: str | None = None):
    """在独立的后台线程里执行 fn()；等待期间每 5 秒写一次心跳；超时则放弃这次调用。
    用守护线程（不是线程池）：卡死的调用不会挡住软件退出。"""
    timeout = float(timeout or cfg()["call_timeout_s"])
    hb = heartbeat or getattr(_local, "name", None)
    q: queue.Queue = queue.Queue(maxsize=1)
    parent = getattr(_local, "name", None)

    def run():
        set_current(parent)
        try:
            q.put((True, fn()))
        except BaseException as e:  # noqa: BLE001
            q.put((False, e))
    background.spawn(run, name="timed-call")
    end = time.time() + timeout
    while True:
        try:
            ok, val = q.get(timeout=min(5.0, max(0.05, end - time.time())))
            if ok:
                return val
            raise val
        except queue.Empty:
            if hb:
                beat(hb)
            if time.time() >= end:
                raise CallTimeout(f"no response in {int(timeout)}s")


# ------------------------------------------------------------------ 防原地打转

class StepGuard:
    """每个任务一个：步数上限、费用上限、重复动作检测。"""

    def __init__(self, task_id: str, kind: str):
        c = cfg()
        self.task_id = task_id
        self.max_steps = int(c["max_steps"].get(kind, 3000))
        self.max_cost = float(c["max_cost_usd"].get(kind, 5.0))
        self.repeat_limit = int(c["repeat_limit"])
        self.steps = 0
        self.seen: dict[str, int] = {}
        self._last_cost_check = 0

    def step(self, action: str) -> str:
        """返回 "ok" / "repeat"（这个动作重复太多次，换方法）/ "limit"（步数或费用到上限，停止任务）。"""
        self.steps += 1
        if self.steps > self.max_steps:
            return "limit"
        n = self.seen.get(action, 0) + 1
        self.seen[action] = n
        if n > self.repeat_limit:
            return "repeat"
        if self.steps - self._last_cost_check >= 20:
            self._last_cost_check = self.steps
            if self.cost() > self.max_cost:
                return "limit"
        return "ok"

    def cost(self) -> float:
        return db.query_one("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE task_id=?", (self.task_id,))["c"]


# ------------------------------------------------------------------ 反复失败：网站冷却

def domain_failed(domain: str, reason: str, kind: str = "") -> int | None:
    """记录一次失败；连续失败达到上限返回冷却分钟数（并暂停该网站）。kind = 被拦的类型（验证码 / 浏览器检查 …）。"""
    c = cfg()
    st = settings.get("domain_health", {}) or {}
    d = st.get(domain, {"fails": 0})
    d["fails"] = d.get("fails", 0) + 1
    d["reason"] = reason
    d["kind"] = kind
    mins = None
    if d["fails"] >= int(c["domain_fail_limit"]) or reason == "challenge":
        mins = int(c["domain_cooldown_min"])
        d["until"] = time.time() + mins * 60
        d["fails"] = 0
    st[domain] = d
    settings.put("domain_health", st)
    return mins


def domain_ok(domain: str):
    st = settings.get("domain_health", {}) or {}
    if domain in st:
        st.pop(domain)
        settings.put("domain_health", st)


def domain_kind(domain: str) -> str:
    return ((settings.get("domain_health", {}) or {}).get(domain) or {}).get("kind") or ""


def domain_paused(domain: str) -> float:
    d = (settings.get("domain_health", {}) or {}).get(domain) or {}
    until = float(d.get("until") or 0)
    return until if until > time.time() else 0.0


# ------------------------------------------------------------------ 看门狗（通用：任务 + 注册的后台作业）

def register_job(name: str, max_silence_s: float, restart):
    """注册一个需要看门狗照看的后台作业（如调度器；第 3 步的销售主管作业）。"""
    with _jobs_lock:
        _jobs[name] = {"max": max_silence_s, "restart": restart, "restarts": 0}
    beat(name)


@background.tracked("watchdog")
def check(now=None) -> list[dict]:
    """看门狗检查一次。返回本次处理的卡住项目。"""
    from . import tasks
    now = now or time.time()
    c = cfg()
    handled = []
    for run in list(tasks._RUNS.values()):
        if run.done or getattr(run, "restarted", False):
            continue
        last = max(run.last_beat, last_beat(f"task:{run.id}"))
        if now - last > float(c["stuck_after_s"]):
            handled.append(_restart_task(run, now - last))
    with _jobs_lock:
        jobs = dict(_jobs)
    for name, j in jobs.items():
        if now - last_beat(name) > j["max"]:
            _stuck_alert(name, int(now - last_beat(name)), "job")
            try:
                j["restart"]()
                j["restarts"] += 1
                beat(name)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            handled.append({"job": name})
    return handled


def _stuck_alert(name: str, silent_s: int, what: str, agent: str = ""):
    """纯程序报警：不调用任何大模型；通知号发不出去时 alerts 自动改用桌面弹窗。"""
    from . import alerts
    alerts.raise_alert(f"stuck:{name}", "urgent", "agent_stuck", link="#/tasks",
                       agent=f"@agent.{agent}" if agent else name, minutes=max(1, silent_s // 60))


@background.tracked("watchdog-restart")
def _restart_task(run, silent: float) -> dict:
    from . import tasks
    agent = next((a for a, s in run.agent_state.items() if s.get("state") == "working"), "system")
    run.agent_state.setdefault(agent, {})
    run.agent_state[agent].update(state="stuck", key="ev.agent_stuck", params={"minutes": max(1, int(silent // 60))})
    run.emit("system", "ev.agent_stuck_restart", level="error", agent=f"@agent.{agent}",
             minutes=max(1, int(silent // 60)))
    _stuck_alert(f"task:{run.id}", int(silent), "task", agent)
    run.restarted = True
    run.cancel_flag.set()                 # 旧线程如果还活着，下一步就会停下
    run.done = True
    run.status = "restarted"
    db.execute("UPDATE tasks SET status='restarted', result='stuck', finished_at=? WHERE id=?", (db.now(), run.id))
    if run.params.get("continuous_monitor"):
        # The daily scout owns this durable collector. Keeping its ownership
        # lets disabling monitoring cancel the resumed task as well.
        db.execute("UPDATE tasks SET status='interrupted' WHERE id=?", (run.id,))
        run.status = "interrupted"
        return {"task": run.id, "retry_owner": "scout"}
    # Manager's persisted plan owns the retry budget. A watchdog restart must
    # not create another uncounted lineage (and another paid model invocation).
    ident, seen = run.id, set()
    while ident and ident not in seen:
        seen.add(ident)
        row = db.query_one("SELECT created_by,parent_id FROM tasks WHERE id=?", (ident,))
        if not row:
            break
        parent = row.get("parent_id") or ""
        if row["created_by"] == "manager" or parent.startswith("manager:"):
            return {"task": run.id, "retry_owner": "manager"}
        ident = parent
    # 从保存的进度继续：新任务带上 resume_from，研究任务跳过已读网址、重新排队未处理的线索；
    # 监控按读取位置继续；快速开始跳过已提取的页面
    params = dict(run.params)
    params["resume_from"] = run.id
    db.execute("UPDATE raw_leads SET status='new' WHERE task_id=? AND status='processing'", (run.id,))
    try:
        new = tasks.create_task(params, created_by="watchdog", parent_id=run.id, kind=run.kind)
    except Exception as e:  # noqa: BLE001
        return {"task": run.id, "restart_error": str(e)[:200]}
    if run.kind == "onboarding" and params.get("onboarding_id") and not params.get("member_url"):
        # 快速开始页面跟着新任务走（否则界面还盯着旧任务，看起来像卡住）
        db.execute("UPDATE onboarding SET task_id=? WHERE id=? AND task_id=?", (new["id"], params["onboarding_id"], run.id))
    return {"task": run.id, "restarted_as": new["id"]}


def start_watchdog():
    """看门狗自己是一个独立线程（不依赖调度器）；它也会照看调度器。"""
    def loop():
        while True:
            time.sleep(60)
            try:
                check()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True, name="watchdog").start()

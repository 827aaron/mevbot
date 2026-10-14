"""后台调度器（每 20 秒检查一次）：

- 监控员读 TG 群：只在有任务勾选了「TG 群」并在运行时（PRD 6.4）；频率 30 分钟 / 1 小时 / 6 小时 / 每天固定时间
  （默认 1 小时）；多账号错峰（每个账号在周期内的不同时刻轮到）；按读取位置补读，不重不漏
- 辅助进群排队（开启后才执行，随机间隔慢速加入）
- TG 账号健康检查（每 30 分钟）
- TG 群列表自动更新：每 5 分钟快速检查最新对话，每天一次完整同步（标出「已退出」的群）
- 定时 / 每天的找客户任务：到点开一轮；错过的（睡眠 / 关机）当天补一次（PRD 5.1.1）
- 线索推送：到截止时间（默认 20:00）推送当天线索
- 异常提醒：补发失败的紧急提醒；一般提醒每 2 小时汇总
- 预算恢复后关闭“预算用完”提醒
- AI 学习：关键词进化（每天）、判断模型自检（每周）
每个作业单独 try，一个失败不影响其他。
"""
import threading
import time
import traceback

from . import alerts, db, settings

TICK = 20
_started = False
_last: dict[str, float] = {}


def _every(name, seconds) -> bool:
    now = time.time()
    if now - _last.get(name, 0) >= seconds:
        _last[name] = now
        return True
    return False


def due_monitor_accounts(now=None) -> list[int]:
    from .tg import pool
    now = now or time.time()
    cfg = settings.get("monitor")
    iv = cfg.get("interval", "1h")
    accs = [a for a in pool.monitor_accounts()
            if db.query_one("SELECT 1 FROM tg_chats WHERE account_id=? AND monitored=1 LIMIT 1", (a["id"],))]
    if not accs:
        return []
    st = settings.get("monitor_state", {}) or {}
    if not st.get("epoch"):
        st["epoch"] = now
        settings.put("monitor_state", st)
    n = len(accs)
    due = []
    if iv == "daily":
        hh, mm = (cfg.get("daily_time") or "09:00").split(":")
        base = time.mktime(time.localtime(now)[:3] + (int(hh), int(mm), 0, 0, 0, -1))
        for i, a in enumerate(accs):
            slot = base + i * 600          # 每个账号间隔 10 分钟
            if now >= slot and (a["last_monitor_at"] or 0) < slot:
                due.append(a["id"])
        return due
    period = {"30m": 1800, "1h": 3600, "6h": 21600}.get(iv, 3600)
    for i, a in enumerate(accs):
        last = a["last_monitor_at"]
        if last is None:
            if now >= float(st["epoch"]) + period * i / n:
                due.append(a["id"])
        elif now - last >= period:
            due.append(a["id"])
    return due


_tg_info_busy = [False]


def tick():
    from . import background
    with background.operation("scheduler"):
        return _tick()


def _tick():
    from . import llm, push, tasks
    from .tg import joining, pool
    jobs = []

    def monitor():
        from . import llm
        if not tasks.tg_tasks():
            return                       # 没有勾选了 TG 群、正在运行的任务 → 不读群（PRD 6.4）
        if not settings.agent_enabled("monitor") or tasks.running("monitor") or not llm.configured("monitor"):
            return
        due = due_monitor_accounts()
        if due:
            tasks.create_task({"account_ids": due, "trigger": "schedule"}, created_by="scheduler", kind="monitor")
    jobs.append(("monitor", monitor))
    jobs.append(("join", joining.process_one))

    def health():
        if _every("health", 1800):
            for a in db.query("SELECT id FROM tg_accounts WHERE status IN ('active','flood_wait') AND session_enc<>''"):
                pool.check_health(a["id"])
    jobs.append(("health", health))

    def cutoff():
        if push.cutoff_due():
            push.auto_push("cutoff")
    jobs.append(("push", cutoff))
    jobs.append(("alerts", alerts.flush))

    def budget():
        if not llm.budget_state()["paused"]:
            for r in db.query("SELECT DISTINCT key FROM alerts WHERE category='budget' AND status='open'"):
                alerts.resolve(r["key"])
    jobs.append(("budget", budget))

    def learning():
        from .learn import evolve
        if _every("evolve", 600):
            evolve.evolve_if_due()
            evolve.retest_if_due()
    jobs.append(("learning", learning))

    def lead_pool():
        # 线索池管理（PRD 5.2.2）：每天一次（主管可调）—— 清理黑名单/广告、求购过期、复查到期的备用线索
        from . import leadpool
        from . import llm
        if settings.agent_enabled("analyst") and llm.configured("analyst") and leadpool.due() and not tasks.running("pool"):
            tasks.create_task({"trigger": "schedule"}, created_by="scheduler", kind="pool")
    jobs.append(("lead_pool", lead_pool))

    def awake():
        from . import power
        power.keep_awake(power.should_be_awake())
    jobs.append(("awake", awake))

    def tg_info():
        # TG 群的真实人数 / 简介 / 讨论区：每 10 分钟补几个（账号空闲时、随机停顿、每天有上限），不在同步时一次性请求
        if _every("tg_info", 600) and not _tg_info_busy[0]:
            def run():
                _tg_info_busy[0] = True
                try:
                    pool.refresh_chat_info(4)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                finally:
                    _tg_info_busy[0] = False
            from . import background
            background.spawn(run, name="tg-info")
    jobs.append(("tg_info", tg_info))

    def tg_upkeep():
        # 群列表自动更新（PRD 6.3）：每 5 分钟快速检查最新对话、每天一次完整同步（不受任务 / 暂停 AI 影响：不是 AI 作业）
        if _every("tg_upkeep", 60):
            from .tg import upkeep
            upkeep.tick()
    jobs.append(("tg_upkeep", tg_upkeep))

    def task_schedules():
        # 定时 / 每天的任务（PRD 5.1.1）：到点开一轮；睡眠 / 关机错过的，开机或唤醒后当天补一次
        from . import schedules
        schedules.tick()
    jobs.append(("schedules", task_schedules))

    from .agents import manager, scout
    from .agents import preparation
    jobs.append(("manager_preparation", preparation.tick))
    from .agents import remediation
    jobs.append(("manager_remediation", remediation.tick))
    jobs.append(("manager", manager.tick))
    jobs.append(("scout", scout.tick))
    from . import source_pool
    jobs.append(("source_pool", source_pool.tick))

    paused = settings.ai_paused()
    for name, fn in jobs:
        if paused and name in ("monitor", "join", "learning", "lead_pool", "tg_info"):
            continue                     # 「暂停所有 AI」：不跑 AI 作业；推送已合格的线索、提醒、防休眠照常
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _loop(gen):
    from . import heal
    heal.beat("scheduler")
    time.sleep(5)
    while gen == _gen[0]:
        heal.beat("scheduler")
        heal.set_current("scheduler")
        tick()
        heal.beat("scheduler")
        time.sleep(TICK)


_gen = [0]


def _spawn():
    _gen[0] += 1
    threading.Thread(target=_loop, args=(_gen[0],), daemon=True, name=f"scheduler-{_gen[0]}").start()


def start():
    """启动调度器，并交给看门狗照看（调度器卡住 5 分钟会被重启）。"""
    global _started
    if _started:
        return
    _started = True
    from . import heal
    _spawn()
    heal.register_job("scheduler", 300, _spawn)
    heal.start_watchdog()

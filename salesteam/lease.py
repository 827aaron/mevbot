"""账号调度（PRD 6.2.1）：程序层面的“借账号”。所有 AI 用平台账号（TG，以后 FB）之前都要先借到账号。

1. 一个账号同一时间只给一个 AI 用（每个账号一把锁 + 排队）；等的时候也可以换一个空闲的、有对应用途的账号。
2. 每个账号每天的上限是“这个账号的”，不分哪个 AI —— 所有调用都走 use() 这一个计数（tg_counters 表）：
   进群 join、搜索 search、预览 preview、读消息页数 read、发消息 send（以后私信也走这里）。
3. 优先级：监控员读消息（高）> 渠道拓展员搜索 / 预览（中）> 进群（低，只在账号空闲时）。销售主管可调（settings）。
4. TG 账号用途（可以重叠，调度照样一个一个来）：scout 只搜索 / 预览，不加群、不读消息；monitor 读群，
   也负责加入它要监控的群（新群分给群最少的监控账号，由它加入）；notify 只发推送 / 提醒。
5. FB（第 4 步，只有一个账号 / 浏览器配置）：严格一个一个来，两次操作之间有时间间隔，所有动作合并计每日上限。
6. 账号被限制 / 封号 / 掉线：立刻不再借给任何 AI；它监控的群转给其他账号；发紧急提醒（reassign_groups）。
"""
import itertools
import threading
import time

from . import db, settings
from .errors import UserError

DEFAULTS = {
    # 数字越小越优先；销售主管可调（PRD 6.2.1 第 3 条）
    "priorities": {"monitor": 0, "notify": 0, "scout": 1, "maintenance": 1, "join": 2},
    # 每个账号每天的上限（与哪个 AI 无关）。join / send 用账号自己的设置（daily_join_limit / daily_send_limit）
    # info：每天补全几个群的人数 / 简介；dialogs：群列表自动更新的快速检查（每 5 分钟 1 次 = 288 次 / 天，不占 read）
    "caps": {"search": 30, "preview": 80, "read": 800, "info": 40, "dialogs": 300, "fb_action": 120},
    "fb_min_gap_s": 90,              # FB：两次操作之间至少隔多久（时间片）
    "wait_timeout_s": 600,
}


class CapReached(UserError):
    def __init__(self, account_id, action, cap):
        super().__init__("account_cap_reached", 429, action=action, cap=cap)
        self.account_id = account_id
        self.action = action


def cfg() -> dict:
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    s = settings.get("account_sched", {}) or {}
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


_cv = threading.Condition()
_held: dict[str, dict] = {}          # 账号键 → {purpose, agent, since}
_waiting: list[dict] = []            # 正在等的请求
_last_release: dict[str, float] = {}
_seq = itertools.count()
_count_lock = threading.Lock()
log: list[dict] = []                 # 最近的借还记录（测试 / 排查用，最多 2000 条）


# ------------------------------------------------------------------ 账号与用途

def roles_of(r) -> set:
    """账号用途。旧数据只有 role：监控号默认也可以给渠道拓展员搜索 / 预览。"""
    raw = (r["roles"] if "roles" in r.keys() else "") or ""
    rs = {x.strip() for x in raw.split(",") if x.strip()}
    if not rs:
        rs = {"notify"} if r["role"] == "notify" else {"monitor", "scout"}
    return rs


def _key(platform, account_id) -> str:
    return f"{platform}:{account_id}"


def _candidates(platform: str, role: str, account_id=None) -> list:
    """现在可以用的账号（健康、有这个用途）。被限制 / 掉线 / 等待 FloodWait 的不在里面。"""
    if platform == "tg":
        from .tg import pool
        out = []
        for r in db.query("SELECT * FROM tg_accounts ORDER BY id"):
            if account_id is not None and r["id"] != account_id:
                continue
            if role in roles_of(r) and pool.usable(r):
                out.append(r["id"])
        return out
    if platform == "fb":
        fb = settings.get("fb_profile", {}) or {}
        return ["main"] if fb.get("ready") and (account_id in (None, "main")) else []
    return []


def _prio(purpose: str) -> int:
    return int(cfg()["priorities"].get(purpose, 1))


def _blocked_by_better(entry, key) -> bool:
    """有优先级更高（或同级但先来）的请求也在等这个账号 → 让给它。"""
    for w in _waiting:
        if w is entry or w["platform"] != entry["platform"] or key not in w["keys"]:
            continue
        if (w["prio"], w["seq"]) < (entry["prio"], entry["seq"]):
            return True
    return False


class Lease:
    def __init__(self, platform, account_id, purpose, agent):
        self.platform, self.account_id, self.purpose, self.agent = platform, account_id, purpose, agent
        self.key = _key(platform, account_id)
        self.released = False

    def use(self, action: str, n: int = 1):
        use(self.platform, self.account_id, action, n)

    def release(self):
        if self.released:
            return
        self.released = True
        with _cv:
            h = _held.pop(self.key, None)
            _last_release[self.key] = time.time()
            _log("release", self.platform, self.account_id, self.purpose, self.agent,
                 held=time.time() - (h or {}).get("since", time.time()))
            _cv.notify_all()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


stats: dict[str, dict] = {}         # 按用途汇总：借了几次、最长等待、最长占用（排查“谁饿着了”）


def _log(ev, platform, account_id, purpose, agent, **kw):
    st = stats.setdefault(purpose, {"acquires": 0, "timeouts": 0, "max_wait": 0.0, "max_hold": 0.0})
    if ev == "acquire":
        st["acquires"] += 1
        st["max_wait"] = max(st["max_wait"], kw.get("waited", 0))
    elif ev == "release":
        st["max_hold"] = max(st["max_hold"], kw.get("held", 0))
    elif ev == "timeout":
        st["timeouts"] += 1
    log.append({"ev": ev, "platform": platform, "account": account_id, "purpose": purpose, "agent": agent,
                "ts": time.time(), **kw})
    if len(log) > 2000:
        del log[:500]


def acquire(platform: str = "tg", role: str = "monitor", purpose: str | None = None, agent: str = "",
            account_id=None, timeout: float | None = None, idle_only: bool = False, skip=()) -> Lease | None:
    """借一个账号。account_id 指定时只借这一个（例如监控员读这个账号加入的群）；否则借任意一个空闲的、
    有这个用途的账号。idle_only：只在有空闲账号时借（进群用），借不到立即返回 None。超时返回 None。"""
    purpose = purpose or role
    c = cfg()
    timeout = float(c["wait_timeout_s"]) if timeout is None else timeout
    deadline = time.time() + timeout
    entry = {"seq": next(_seq), "prio": _prio(purpose), "platform": platform, "keys": set(), "purpose": purpose}
    t0 = time.time()
    with _cv:
        _waiting.append(entry)
        try:
            while True:
                keys = [_key(platform, a) for a in _candidates(platform, role, account_id) if a not in skip]
                entry["keys"] = set(keys)
                if not keys:
                    return None                         # 没有可用账号（被限制 / 掉线 / 没有这个用途）
                for k in keys:
                    if k in _held or _blocked_by_better(entry, k):
                        continue
                    if platform == "fb":                # FB：严格的时间片
                        gap = float(c["fb_min_gap_s"]) - (time.time() - _last_release.get(k, 0))
                        if gap > 0:
                            continue
                    aid = k.split(":", 1)[1]
                    aid = int(aid) if platform == "tg" else aid
                    _held[k] = {"purpose": purpose, "agent": agent, "since": time.time()}
                    _log("acquire", platform, aid, purpose, agent, waited=time.time() - t0)
                    return Lease(platform, aid, purpose, agent)
                if idle_only:
                    return None
                left = deadline - time.time()
                if left <= 0:
                    _log("timeout", platform, account_id, purpose, agent, waited=time.time() - t0)
                    return None
                _cv.wait(min(left, 1.0))
        finally:
            if entry in _waiting:
                _waiting.remove(entry)


def holding() -> dict:
    with _cv:
        return {k: dict(v) for k, v in _held.items()}


# ------------------------------------------------------------------ 每个账号每天的上限（唯一的计数入口）

def cap(platform: str, account_id, action: str) -> int:
    c = cfg()["caps"]
    if platform == "tg":
        r = db.query_one("SELECT * FROM tg_accounts WHERE id=?", (account_id,))
        if not r:
            return 0
        if action == "join":
            from .tg import pool
            return min(1, r["daily_join_limit"]) if pool.in_warmup(r) else int(r["daily_join_limit"])
        if action == "send":
            return int(r["daily_send_limit"])
    if platform == "fb":
        return int(c["fb_action"])
    return int(c.get(action, 10 ** 9))


def used(platform: str, account_id, action: str) -> int:
    from .tg import pool
    kind = action if platform == "tg" else f"fb:{action}"
    return pool.count(account_id if platform == "tg" else 0, kind)


def use(platform: str, account_id, action: str, n: int = 1):
    """记一次（或 n 次）动作；超过这个账号今天的上限就报 CapReached（不记）。FB 所有动作合并计数。"""
    from .tg import pool
    with _count_lock:
        if platform == "fb":
            act = "fb_action"
            if pool.count(0, "fb:all") + n > cap("fb", account_id, act):
                raise CapReached(account_id, act, cap("fb", account_id, act))
            pool.incr(0, "fb:all", n)
            pool.incr(0, f"fb:{action}", n)
            return
        limit = cap(platform, account_id, action)
        if pool.count(account_id, action) + n > limit:
            raise CapReached(account_id, action, limit)
        pool.incr(account_id, action, n)


def remaining(platform: str, account_id, action: str) -> int:
    return max(0, cap(platform, account_id, action) - used(platform, account_id, action))


# ------------------------------------------------------------------ 账号出问题：转移它的群 + 紧急提醒

def account_down(acc_id: int, reason: str):
    """账号被限制 / 封号 / 掉线：调度立刻不再借出（_candidates 按状态过滤）；叫醒正在等它的请求去借别的账号；
    它监控的群转给其他监控账号：别的账号已经在群里 → 直接由那个账号监控；否则（开了辅助进群）排队由群最少的
    监控账号加入，没开就列为“需要人工加入”。发一条紧急提醒。"""
    from . import alerts
    from .tg import pool
    with _cv:
        _cv.notify_all()
    moved, queued, orphan = 0, 0, []
    chats = db.query("SELECT * FROM tg_chats WHERE account_id=? AND monitored=1", (acc_id,))
    others = [r for r in pool.monitor_accounts() if r["id"] != acc_id]
    assisted = bool(settings.get("joining").get("assisted_join"))
    for c in chats:
        alt = None
        for o in sorted(others, key=lambda r: db.query_one("SELECT COUNT(*) n FROM tg_chats WHERE account_id=? AND "
                                                             "monitored=1", (r["id"],))["n"]):
            alt = db.query_one("SELECT * FROM tg_chats WHERE account_id=? AND chat_id=? AND left_at IS NULL",
                               (o["id"], c["chat_id"]))
            if alt:
                break
        db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (c["id"],))
        if alt:
            db.execute("UPDATE tg_chats SET monitored=1, icp_id=COALESCE(icp_id, ?), cursor=MAX(cursor, ?) WHERE id=?",
                       (c["icp_id"], c["cursor"], alt["id"]))
            moved += 1
        elif assisted and c["username"] and others:
            from .tg import joining
            link = f"https://t.me/{c['username']}"
            # 旧的“已由这个账号加入”记录不再算数，重新排队，由群最少的监控账号加入
            db.execute("UPDATE tg_join_queue SET status='reassigned' WHERE link=? AND status IN ('joined','needs_approval')",
                       (link,))
            try:
                n = joining.enqueue(link, source="reassign", icp_id=c["icp_id"])
            except UserError:
                n = 0
            if n:
                queued += n
            else:
                orphan.append(c["title"])
        else:
            orphan.append(c["title"])
    r = db.query_one("SELECT label, phone FROM tg_accounts WHERE id=?", (acc_id,))
    label = (r["label"] or r["phone"]) if r else str(acc_id)
    if chats:
        alerts.raise_alert(f"tg_reassign:{acc_id}", "urgent", "tg_reassign", link="#/channels", account=label,
                           n=len(chats), moved=moved, queued=queued, orphan=len(orphan),
                           reason=f"@tgs.{reason}")
    return {"moved": moved, "queued": queued, "orphan": orphan}

"""群列表自动更新（PRD 6.3）：账号加入的群（包括在手机上手动加的）自动出现在列表里，不用点「同步群列表」。

- 快速检查：每个账号每 5 分钟一次（账号之间错开），一次 GetDialogs 读最新 30 个对话（pool.check_new_chats）
- 完整同步：每天一次，在安静时段（凌晨 3–6 点）；那时电脑关着就在超过 26 小时后补做。不在群列表里的群标「已退出」
- 不开 TG 的实时更新推送（receive_updates=False 不变），不在后台一直连着收消息
- 只用正常、没暂停、没在等待限流的账号；都在一个后台线程里一个一个来，账号之间随机停顿
"""
import random
import threading
import time
import traceback

from .. import background, db
from ..errors import UserError
from . import pool

QUICK_EVERY = 300
QUIET_HOURS = (3, 6)
_next_quick: dict[int, float] = {}
_busy = [False]


def _accounts() -> list[dict]:
    """正常、没暂停、不在限流的监控号（通知号不监控群，不用更新群列表）。"""
    from ..lease import roles_of
    return [r for r in db.query("SELECT * FROM tg_accounts WHERE session_enc<>'' ORDER BY id")
            if pool.usable(r) and "monitor" in roles_of(r)]


def due_quick(now: float | None = None) -> list[int]:
    now = now or time.time()
    due = []
    for r in _accounts():
        nxt = _next_quick.get(r["id"])
        if nxt is None:                 # 第一次见到这个账号：在接下来 5 分钟里随机错开
            _next_quick[r["id"]] = nxt = now + random.uniform(0, QUICK_EVERY)
        if now >= nxt:
            due.append(r["id"])
    return due


def due_full(now: float | None = None) -> list[int]:
    now = now or time.time()
    quiet = QUIET_HOURS[0] <= time.localtime(now).tm_hour < QUIET_HOURS[1]
    out = []
    for r in _accounts():
        age = now - (r["dialogs_synced_at"] or 0)
        if (quiet and age > 20 * 3600) or age > 26 * 3600:
            out.append(r["id"])
    return out


def background_sync(acc_id: int) -> dict | None:
    """后台完整同步：账号正忙就下次再说（不提醒）；其他失败照常显示「群列表同步失败」。"""
    try:
        return pool.sync_dialogs(acc_id)
    except UserError as e:
        if e.key == "tg_account_busy":
            return None
        pool.sync_failed(acc_id, e)
    except Exception as e:  # noqa: BLE001
        pool.sync_failed(acc_id, e)
    return None


@background.tracked("tg-upkeep")
def run_once(now: float | None = None, gap=(3.0, 10.0), full_gap=(30.0, 90.0)) -> dict:
    """做一轮：先快速检查到点的账号，再做到点的完整同步。"""
    res: dict = {"quick": {}, "full": {}}
    for i, aid in enumerate(due_quick(now)):
        if i:
            time.sleep(random.uniform(*gap))
        _next_quick[aid] = time.time() + QUICK_EVERY + random.uniform(-30, 30)
        try:
            res["quick"][aid] = pool.check_new_chats(aid)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    for i, aid in enumerate(due_full(now)):
        if i or res["quick"]:
            time.sleep(random.uniform(*full_gap))
        res["full"][aid] = background_sync(aid)
    return res


def tick():
    """调度器每次 tick 调用：有到点的账号就在后台线程里做一轮（上一轮没做完就不开新的）。"""
    if _busy[0] or not (due_quick() or due_full()):
        return

    def run():
        _busy[0] = True
        try:
            run_once()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            _busy[0] = False
    background.spawn(run, name="tg-upkeep")

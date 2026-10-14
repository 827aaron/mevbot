"""防休眠（PRD 9.1）：有任务在运行或有定时监控时，阻止系统自动休眠（允许关屏）；空闲时释放。

- macOS：启动 `caffeinate -i -w <本程序 PID>`（程序退出时自动失效）
- Windows：SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)，在专用线程里保持
- 检测系统是否设置了自动休眠（尽力而为），在界面提示“合上盖子 / 手动睡眠仍会中断定时监控”
- 唤醒后错过的定时工作：调度器按“上次执行时间”判断，会立即补做；监控按读取位置补读，不重不漏
"""
import os
import platform
import re
import subprocess
import threading
import time

_lock = threading.Lock()
_proc = None
_win_thread = None
_win_stop = None
_state = {"active": False, "since": None, "method": ""}


def _mac_on():
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)


def _mac_off():
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        _proc = None


def _win_on():
    global _win_thread, _win_stop
    if _win_thread and _win_thread.is_alive():
        return
    import ctypes
    _win_stop = threading.Event()

    def hold():
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        _win_stop.wait()
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    _win_thread = threading.Thread(target=hold, daemon=True, name="keep-awake")
    _win_thread.start()


def _win_off():
    if _win_stop is not None:
        _win_stop.set()


def keep_awake(active: bool):
    sysname = platform.system()
    with _lock:
        if active == _state["active"]:
            return
        try:
            if sysname == "Darwin":
                _mac_on() if active else _mac_off()
                _state["method"] = "caffeinate"
            elif sysname == "Windows":
                _win_on() if active else _win_off()
                _state["method"] = "SetThreadExecutionState"
            else:
                _state["method"] = "unsupported"
        except Exception:  # noqa: BLE001
            _state["method"] = "failed"
        _state["active"] = active
        _state["since"] = time.time() if active else None


def auto_sleep_minutes() -> int | None:
    """系统空闲多少分钟后自动睡眠（0 = 从不；None = 无法判断）。"""
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"^\s*sleep\s+(\d+)", out, re.M)
            return int(m.group(1)) if m else None
        if sysname == "Windows":
            out = subprocess.run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
                                 capture_output=True, text=True, timeout=8, creationflags=0x08000000).stdout
            m = re.search(r"(?:Current AC Power Setting Index|当前交流电源设置索引):\s*0x([0-9a-f]+)", out, re.I)
            return int(m.group(1), 16) // 60 if m else None
    except Exception:  # noqa: BLE001
        return None
    return None


def status() -> dict:
    mins = auto_sleep_minutes()
    return {**_state, "auto_sleep_minutes": mins, "platform": platform.system(),
            "warn": bool(mins) and not _state["active"]}


def should_be_awake() -> bool:
    """有任务在运行，或有需要按时执行的定时工作（监控 / 辅助进群 / 当天还没推送）时保持唤醒。"""
    from . import db, settings, tasks
    if any(not r.done for r in tasks._RUNS.values()):
        return True
    if settings.get("joining").get("assisted_join") and \
            db.query_one("SELECT 1 FROM tg_join_queue WHERE status='pending' LIMIT 1"):
        return True
    return False

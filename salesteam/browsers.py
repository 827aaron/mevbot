"""浏览器组件（Chromium）：安装包里不带，第一次打开软件时在后台下载（第 5a 步）。

- 存放位置：数据目录/ms-playwright（环境变量 PLAYWRIGHT_BROWSERS_PATH，Playwright 和 Patchright 共用）
- 下载：用安装包里自带的 Playwright / Patchright 驱动执行 `install chromium`（来源：Playwright 官方 CDN），
  解析进度（百分比）；失败自动重试（最多 3 次，间隔递增），之后可以在界面上点“重试”。
  Playwright 的下载器不支持断点续传：重试会重新下载没完成的那个组件（已完成的组件不会重复下载）。
- 需要浏览器的功能（登录官网后读取、隐身模式）在下载完成前会提示“浏览器组件还在下载”，不会报错退出。
- 开发环境（不是安装包）默认不自动下载，沿用电脑上已有的 Playwright 浏览器。
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths

_lock = threading.Lock()
_state = {"state": "unknown", "progress": 0, "component": "", "error": "", "attempt": 0, "updated_at": 0.0}
_PROGRESS = re.compile(r"(\d{1,3})%\s+of\s+([\d.]+)\s*(KiB|MiB|GiB)")
_START = re.compile(r"Downloading\s+(.+?)\s+(?:\(|from|v\d)", re.I)


def browsers_dir() -> Path:
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (paths.DATA_DIR / "ms-playwright"))


def configure_env():
    """安装包启动时调用（在导入 playwright / patchright 之前）：浏览器放在数据目录里。"""
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(paths.DATA_DIR / "ms-playwright"))


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _installed() -> bool:
    d = browsers_dir()
    if not d.is_dir():
        return False
    names = [p.name for p in d.iterdir() if p.is_dir()]
    return any(n.startswith("chromium-") for n in names) and any(n.startswith("chromium_headless_shell-")
                                                                 for n in names)


def status() -> dict:
    with _lock:
        st = dict(_state)
    if st["state"] in ("unknown", "missing") or (st["state"] == "ready" and not _installed()):
        st["state"] = "ready" if _installed() else "missing"
        with _lock:
            _state["state"] = st["state"]
    st["managed"] = frozen() or bool(os.environ.get("SALESTEAM_MANAGE_BROWSERS"))
    st["dir"] = str(browsers_dir())
    return st


def ready() -> bool:
    """需要浏览器的功能调用：安装包里要等下载完成；开发环境按电脑上已有的为准（直接放行）。"""
    if not (frozen() or os.environ.get("SALESTEAM_MANAGE_BROWSERS")):
        return True
    return status()["state"] == "ready"


def _drivers() -> list[tuple[str, list[str], dict]]:
    """要执行的安装命令：Playwright（登录官网用）+ Patchright（隐身模式用；版本相同时不会重复下载）。"""
    out = []
    for mod in ("playwright", "patchright"):
        try:
            drv = __import__(f"{mod}._impl._driver", fromlist=["compute_driver_executable", "get_driver_env"])
        except Exception:  # noqa: BLE001
            continue
        node, cli = drv.compute_driver_executable()
        env = drv.get_driver_env()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir())
        out.append((mod, [node, cli, "install", "chromium"], env))
    return out


def _set(**kw):
    with _lock:
        _state.update(kw, updated_at=time.time())


def _run_once() -> bool:
    browsers_dir().mkdir(parents=True, exist_ok=True)
    for mod, cmd, env in _drivers():
        _set(component=mod, progress=0)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                             encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        tail = []
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            tail = (tail + [line])[-8:]
            m = _PROGRESS.search(line)
            if m:
                _set(progress=int(m.group(1)))
            s = _START.search(line)
            if s:
                _set(component=s.group(1)[:60], progress=0)
        if p.wait() != 0:
            _set(error=" | ".join(tail)[-400:])
            return False
    return _installed()


def _worker():
    for attempt in range(1, 4):
        _set(state="downloading", attempt=attempt, error="")
        try:
            if _run_once():
                _set(state="ready", progress=100, error="")
                return
        except Exception as e:  # noqa: BLE001
            _set(error=str(e)[:400])
        time.sleep(min(60, 5 * attempt * attempt))
    _set(state="failed")
    try:
        from . import alerts
        alerts.raise_alert("browser_download", "general", "browser_download", link="#/settings")
    except Exception:  # noqa: BLE001
        pass


def ensure_background(force: bool = False) -> dict:
    """没装就在后台下载（已经在下载就不重复开）。"""
    st = status()
    if st["state"] == "ready" and not force:
        return st
    with _lock:
        if _state["state"] == "downloading":
            return dict(_state)
        _state.update(state="downloading", progress=0, error="")
    threading.Thread(target=_worker, name="browser-download", daemon=True).start()
    return status()

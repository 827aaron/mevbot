"""桌面版入口（第 5a 步安装包）：软件在自己的窗口里打开（pywebview，BSD 许可），服务只监听 127.0.0.1。

两个进程（0.2.2 起）：
- 窗口进程：显示窗口（pywebview），并照看后台服务进程 —— 后台意外退出（例如内存不够被系统结束）就自动重启，
  界面显示“连接中断，正在重新连接…”，连上后自动恢复，不会白屏
- 后台服务进程（同一个可执行文件加 --server）：FastAPI + 任务；崩溃信息（faulthandler、未捕获的异常、线程异常）
  和内存占用都写进 数据目录/app.log

- 数据目录按电脑用户：Mac ~/Library/Application Support/JamesAgent，Windows %APPDATA%\\JamesAgent
  （环境变量 SALESTEAM_DATA 可以覆盖，测试用）
- 浏览器组件（Chromium）不打进安装包：第一次打开时后台下载（salesteam/browsers.py）
- 已经开着时再双击：显示已有的窗口（软件可能只在菜单栏 / 托盘里），不会启动第二个服务
- 关闭窗口 = 隐藏到菜单栏 / 托盘，后台继续工作（PRD 9.0.1）；从图标「退出」（Mac 上 Cmd+Q / 程序坞「退出」也一样）
  先确认“退出后定时任务会停止”再退出；系统注销 / 关机时直接干净退出，不挡住关机
- 窗口只显示本软件的页面：外部链接一律用系统浏览器打开
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request

HOST = "127.0.0.1"
APP_DIR_NAME = "JamesAgent"
MAX_RESTARTS = 5                 # 10 分钟内最多自动重启几次（再多说明有持续性问题，停止重启并在界面提示）


def default_data_dir() -> str:
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_DIR_NAME)
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), APP_DIR_NAME)


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/meta", timeout=1.5) as r:
            return "agents" in json.load(r)
    except Exception:  # noqa: BLE001
        return False


def _free_port(start: int = 8765) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port")


# ------------------------------------------------------------------ 日志 / 崩溃信息

def _log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _mem_mb() -> float:
    """当前进程的最大常驻内存（MB）。"""
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
    except Exception:  # noqa: BLE001
        return 0.0


def _children_mem_mb() -> float:
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024
    except Exception:  # noqa: BLE001
        return 0.0


def _free_mem_mb() -> float:
    """系统可用内存（MB，尽量估算；拿不到返回 0）。"""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
            page = 16384 if "page size of 16384" in out else 4096
            # 空闲 + 可以马上回收的（非活跃 / 预读 / 可清除）≈ 活动监视器里“可用”的量
            free = sum(int(l.split(":")[1].strip().rstrip(".")) for l in out.splitlines()
                       if l.startswith(("Pages free", "Pages speculative", "Pages inactive", "Pages purgeable")))
            return free * page / (1024 * 1024)
        if hasattr(os, "sysconf") and "SC_AVPHYS_PAGES" in os.sysconf_names:
            return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


LOG_KEEP_BYTES = 5 * 1024 * 1024


def _log_setup(data_dir: str, append: bool):
    """窗口程序没有控制台：输出写到 数据目录/app.log。软件每次打开（窗口进程，append=False）时，
    把上一次的日志改名为 app.prev.log 保留下来（出问题后重新打开也能看到上次崩溃前的记录），再在 app.log 里追加；
    后台进程（含自动重启）一直追加。app.log 超过 5 MB 时同样轮换，不会无限变大。
    打开 faulthandler（程序被信号结束 / 原生库崩溃时也能留下线程栈），记录未捕获的异常和线程异常。"""
    import faulthandler
    path = os.path.join(data_dir, "app.log")
    try:
        if os.path.exists(path) and (not append or os.path.getsize(path) > LOG_KEEP_BYTES):
            os.replace(path, os.path.join(data_dir, "app.prev.log"))
    except OSError:
        pass
    try:
        f = open(path, "a", encoding="utf-8", buffering=1)
        f.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] {'server' if append else 'app'} start, "
                f"pid {os.getpid()}, version {_version()} =====\n")
    except OSError:
        return
    if getattr(sys, "frozen", False) and not os.environ.get("SALESTEAM_CONSOLE"):
        sys.stdout = sys.stderr = f
    try:
        faulthandler.enable(file=f, all_threads=True)
    except Exception:  # noqa: BLE001
        pass

    def hook(et, ev, tb):
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] UNCAUGHT {et.__name__}: {ev}\n")
        traceback.print_exception(et, ev, tb, file=f)
        f.flush()

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def _version() -> str:
    try:
        from . import VERSION as __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "?"


# ------------------------------------------------------------------ 后台服务进程

def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102     # WAIT_TIMEOUT = 还在运行
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_main():
    data_dir = os.environ["SALESTEAM_DATA"]
    _log_setup(data_dir, append=True)
    port = int(os.environ["SALESTEAM_PORT"])
    from . import browsers
    browsers.configure_env()           # 必须在导入 playwright / patchright 之前
    import uvicorn
    from . import APP_NAME, server
    server.startup()
    _log(f"{APP_NAME['en']} server pid {os.getpid()} on http://{HOST}:{port}/  data: {data_dir}")
    if not os.environ.get("SALESTEAM_NO_BROWSER_DOWNLOAD"):    # 只给构建机上的冒烟测试用
        browsers.ensure_background()   # 第一次打开：后台下载浏览器组件

    def mem_watch():                   # 每 5 分钟记一次内存，崩溃后能看到之前的占用
        while True:
            time.sleep(300)
            _log(f"memory: server max RSS {_mem_mb():.0f} MB, system free ~{_free_mem_mb():.0f} MB")
    threading.Thread(target=mem_watch, name="mem-watch", daemon=True).start()
    parent = int(os.environ.get("SALESTEAM_PARENT_PID") or 0)

    def parent_watch():                # 窗口进程没了（被强制结束），后台服务也退出，不留孤儿进程
        while True:
            time.sleep(2)
            if not _parent_alive(parent):
                _log("window process is gone — server exiting")
                os._exit(0)
    threading.Thread(target=parent_watch, name="parent-watch", daemon=True).start()
    uvicorn.run(server.app, host=HOST, port=port, log_level="warning", access_log=False)


# ------------------------------------------------------------------ 窗口进程（照看后台服务）

class Supervisor:
    def __init__(self, data_dir: str, port: int):
        self.data_dir, self.port = data_dir, port
        self.proc = None
        self.quitting = False
        self.restarts: list[float] = []
        self.gave_up = False
        self.on_give_up = None           # 窗口进程设置：不再自动重启时在窗口里说明（否则页面一直显示“正在重新连接…”）

    def _cmd(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--server"]
        return [sys.executable, "-m", "salesteam.desktop", "--server"]

    def start(self):
        env = dict(os.environ, SALESTEAM_DATA=self.data_dir, SALESTEAM_PORT=str(self.port),
                   SALESTEAM_PARENT_PID=str(os.getpid()))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self._cmd(), env=env, cwd=None if getattr(sys, "frozen", False) else root,
                                     creationflags=flags)
        for _ in range(300):
            if _alive(self.port) or self.proc.poll() is not None:
                break
            time.sleep(0.1)
        _log(f"server process started (pid {self.proc.pid}, port {self.port})")

    def watch(self):
        while not self.quitting:
            time.sleep(1)
            if self.quitting or self.proc is None or self.proc.poll() is None:
                continue
            code = self.proc.returncode
            _log(f"server process exited unexpectedly (code {code}); children max RSS {_children_mem_mb():.0f} MB, "
                 f"system free ~{_free_mem_mb():.0f} MB")
            now = time.time()
            self.restarts = [t for t in self.restarts if now - t < 600] + [now]
            if len(self.restarts) > MAX_RESTARTS:
                _log("too many restarts in 10 minutes — not restarting again")
                self.gave_up = True
                if self.on_give_up:
                    try:
                        self.on_give_up()
                    except Exception as e:  # noqa: BLE001
                        _log(f"give-up notice failed: {e}")
                return
            time.sleep(1.5)
            self.start()

    def stop(self):
        self.quitting = True
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                p.kill()


class Bridge:
    """给页面用的极少数本机功能（pywebview js_api）：只有“用系统浏览器打开外部链接”。"""

    def open_external(self, url: str):
        import webbrowser
        if isinstance(url, str) and url.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)
        return True


def main():
    if "--server" in sys.argv:
        return server_main()
    data_dir = os.environ.get("SALESTEAM_DATA") or default_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    os.environ["SALESTEAM_DATA"] = data_dir
    os.environ.setdefault("SALESTEAM_MANAGE_BROWSERS", "1")
    lock = os.path.join(data_dir, ".running.json")
    port, owner = None, 0
    try:
        info = json.load(open(lock, encoding="utf-8"))
        port, owner = info.get("port"), int(info.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        pass
    if port and _alive(port) and owner and owner != os.getpid() and _parent_alive(owner):
        # 已经在运行（可能只在菜单栏 / 托盘里）：让已有的窗口显示出来，这个新进程直接退出
        try:
            urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/api/desktop/show", data=b"{}",
                                                          headers={"Content-Type": "application/json"}), timeout=3)
        except Exception:  # noqa: BLE001
            pass
        return
    # 上次被强制结束：留下的后台服务几秒内会自己退出（它发现窗口进程没了）；这里另选端口重新启动，不连旧服务
    _log_setup(data_dir, append=False)
    port = _free_port(int(os.environ.get("SALESTEAM_PORT", "8765")))
    sup = Supervisor(data_dir, port)
    sup.start()
    with open(lock, "w", encoding="utf-8") as f:
        json.dump({"port": port, "pid": os.getpid()}, f)
    threading.Thread(target=sup.watch, name="supervisor", daemon=True).start()

    def cleanup():                     # 干净退出：停掉后台服务、删除“正在运行”标记（可以重复调用）
        sup.stop()
        try:
            os.remove(lock)
        except OSError:
            pass
    try:
        _window(port, sup, cleanup)
    finally:
        cleanup()
        _log("quit")


def _reload_on_web_crash():
    """Mac：WKWebView 的网页进程被系统结束（内存紧张时）会白屏 —— 自动重新加载页面。"""
    try:
        import objc
        from webview.platforms import cocoa

        def webViewWebContentProcessDidTerminate_(self, webview):
            _log("web content process terminated — reloading the window")
            webview.reload()
        objc.classAddMethods(cocoa.BrowserView.BrowserDelegate, [objc.selector(
            webViewWebContentProcessDidTerminate_, selector=b"webViewWebContentProcessDidTerminate:",
            signature=b"v@:@")])
    except Exception as e:  # noqa: BLE001
        _log(f"web-crash reload hook not installed: {e}")


_REOPEN = [None]
_TERMINATE = [None]
_SYSTEM_QUIT = {b"logo", b"rlgo", b"rrst", b"rsdn", b"rest", b"shut"}     # 注销 / 重启 / 关机（kAEQuitReason）


def _mac_system_quit() -> bool:
    """Mac：这次退出是不是系统注销 / 重启 / 关机发起的（这时不能拦，否则会挡住关机）。"""
    try:
        import Foundation
        ev = Foundation.NSAppleEventManager.sharedAppleEventManager().currentAppleEvent()
        why = ev.attributeDescriptorForKeyword_(int.from_bytes(b"why?", "big")) if ev is not None else None
        return why is not None and why.enumCodeValue() in {int.from_bytes(c, "big") for c in _SYSTEM_QUIT}
    except Exception:  # noqa: BLE001
        return False


def _mac_terminate_hook():
    """Mac：Cmd+Q、程序坞「退出」、系统注销 / 关机都会走到 applicationShouldTerminate。pywebview 默认把它当成
    “关闭窗口”处理（我们的关闭 = 隐藏），那样 Cmd+Q 没反应、还会挡住关机 —— 换成自己的处理（Controller.should_terminate）。"""
    try:
        import objc
        from webview.platforms import cocoa
        orig = cocoa.BrowserView.AppDelegate.applicationShouldTerminate_

        def applicationShouldTerminate_(self, app):
            fn = _TERMINATE[0]
            return 1 if fn is None or fn() else 0          # NSTerminateNow / NSTerminateCancel
        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [objc.selector(
            applicationShouldTerminate_, selector=b"applicationShouldTerminate:", signature=orig.signature)])
    except Exception as e:  # noqa: BLE001
        _log(f"terminate hook not installed: {e}")


def _show_on_reopen():
    """Mac：窗口关掉（隐藏）后，点程序坞图标或再次打开 App → 显示窗口。"""
    try:
        import objc
        from webview.platforms import cocoa

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            if _REOPEN[0]:
                _REOPEN[0]()
            return True
        objc.classAddMethods(cocoa.BrowserView.AppDelegate, [objc.selector(
            applicationShouldHandleReopen_hasVisibleWindows_,
            selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception as e:  # noqa: BLE001
        _log(f"reopen hook not installed: {e}")


class Controller:
    """窗口 + 菜单栏 / 托盘（PRD 9.0.1）：关闭窗口 = 隐藏到菜单栏 / 托盘，后台服务继续工作；
    从图标「退出」（先确认）才真正退出。图标建不起来时，关闭窗口照旧退出（不会出现“在后台却找不到”的情况）。"""

    def __init__(self, port: int, window, sup, cleanup=None):
        from . import tray as tray_mod
        self.port, self.win, self.sup = port, window, sup
        self.cleanup = cleanup or (lambda: None)
        self.quitting = False
        self.hinted = False
        self.tray = tray_mod.Tray(self.show, self.toggle_pause, self.quit, log=_log)

    def _api(self, method: str, path: str, data: dict | None = None):
        req = urllib.request.Request(f"http://{HOST}:{self.port}{path}", method=method,
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.load(r)

    def started(self):
        """pywebview 窗口起来以后（在后台线程里）：建图标、开始刷新状态。"""
        if self.tray.start():
            _log("menu-bar / tray icon ready — closing the window keeps the app running")
        else:
            _log("no menu-bar / tray icon — closing the window quits the app")
        threading.Thread(target=self.poll, name="tray-poll", daemon=True).start()
        if sys.platform == "win32":
            self._win_allow_shutdown()

    def _win_allow_shutdown(self):
        """Windows：注销 / 关机（或任务管理器结束）时关闭窗口不改成隐藏，直接退出，不挡住关机。
        pywebview 先处理关闭事件（我们返回“取消” = 隐藏），这里排在它后面，把“取消”改回来。"""
        try:
            from System.Windows.Forms import CloseReason
            form = self.win.native

            def closing(sender, args):
                if args.CloseReason in (CloseReason.WindowsShutDown, CloseReason.TaskManagerClosing):
                    self.quitting = True
                    args.Cancel = False
            form.FormClosing += closing
        except Exception as e:  # noqa: BLE001
            _log(f"shutdown hook not installed: {e}")

    def should_terminate(self) -> bool:
        """Mac：Cmd+Q / 程序坞「退出」/ 系统注销关机（主线程）。返回 True = 现在退出。"""
        if not self.quitting and self.tray.ok:
            if _mac_system_quit():
                _log("system logout / restart / shutdown — quitting")
            elif not self.tray.confirm_quit():     # 和菜单栏「退出」一样先确认“退出后定时任务会停止”
                return False
            else:
                _log("quit (Cmd+Q / Dock)")
        self.quitting = True
        self.tray.stop()
        self.cleanup()          # 系统马上结束进程（不会回到 webview.start 之后），所以在这里停后台服务、删标记
        return True

    def on_closing(self):
        if self.tray.ok and not self.quitting:
            self.win.hide()
            if not self.hinted:
                self.hinted = True
                self.tray.hint_background()
            _log("window closed — still running in the menu bar / tray")
            return False
        return True

    def show(self):
        try:
            self.win.show()
            self.win.restore()
            _log("main window shown")
        except Exception as e:  # noqa: BLE001
            _log(f"show window failed: {e}")

    def toggle_pause(self, paused_now: bool):
        try:
            self._api("POST", "/api/ai/pause", {"on": not paused_now})
            self.tray.update(self._api("GET", "/api/desktop/state"))
        except Exception as e:  # noqa: BLE001
            _log(f"pause / resume failed: {e}")

    def quit(self):
        _log("quit from the menu-bar / tray icon")
        self.quitting = True
        self.tray.stop()
        try:
            self.win.destroy()
        except Exception:  # noqa: BLE001
            os._exit(0)

    def poll(self):
        last = None
        while not self.quitting:
            try:
                st = self._api("GET", "/api/desktop/state")
            except Exception:  # noqa: BLE001
                st = None
            self.tray.update(st)
            if st:
                seq = int(st.get("show_seq") or 0)
                if last is not None and seq > last:
                    self.show()             # 软件在后台时又被打开了一次
                last = seq
            time.sleep(2)


_GAVE_UP = {
    "zh": "<html><body style='font:15px -apple-system,Segoe UI,sans-serif;padding:48px;line-height:1.7'>"
          "<h2>后台服务多次意外退出</h2><p>10 分钟内自动重启了 5 次还是不行，已停止自动重启。请从菜单栏 / 托盘图标退出软件，"
          "再重新打开；如果还是这样，请把日志文件发给我们：<br><code>{log}</code>（以及同目录的 app.prev.log）</p></body></html>",
    "en": "<html><body style='font:15px -apple-system,Segoe UI,sans-serif;padding:48px;line-height:1.7'>"
          "<h2>The background service keeps stopping</h2><p>It was restarted 5 times in 10 minutes, so automatic restarts "
          "have stopped. Quit the app from the menu-bar / tray icon and open it again. If it happens again, please send "
          "us the log file:<br><code>{log}</code> (and app.prev.log in the same folder)</p></body></html>",
}


def _window(port: int, sup, cleanup=None):
    url = f"http://{HOST}:{port}/"
    if os.environ.get("SALESTEAM_HEADLESS"):           # 自动化测试：不开窗口，只照看服务
        while sup is None or not sup.quitting:
            time.sleep(1)
        return
    import webview
    from . import APP_NAME
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True                  # 导出 Excel
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True   # 新窗口链接在系统浏览器里打开
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "darwin":
        _reload_on_web_crash()
        _show_on_reopen()
        _mac_terminate_hook()
    lang = "zh"
    try:
        with urllib.request.urlopen(f"{url}api/meta", timeout=3) as r:
            lang = json.load(r).get("ui_lang") or "zh"
    except Exception:  # noqa: BLE001
        pass
    win = webview.create_window(APP_NAME.get(lang, APP_NAME["zh"]), url, width=1440, height=900, min_size=(1100, 700),
                                js_api=Bridge())
    if sup is not None:
        log_path = os.path.join(os.environ["SALESTEAM_DATA"], "app.log")
        sup.on_give_up = lambda: win.load_html(_GAVE_UP.get(lang, _GAVE_UP["zh"]).format(log=log_path))
    ctl = Controller(port, win, sup, cleanup)
    _REOPEN[0] = ctl.show
    _TERMINATE[0] = ctl.should_terminate
    win.events.closing += ctl.on_closing
    webview.start(ctl.started, private_mode=False, storage_path=os.path.join(os.environ["SALESTEAM_DATA"], "webview"))


if __name__ == "__main__":
    main()

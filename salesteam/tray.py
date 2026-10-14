"""菜单栏（Mac）/ 系统托盘（Windows）图标（PRD 9.0.1）。

关闭窗口不退出软件：后台服务继续工作（监控、找客户、定时推送），从这个图标重新打开窗口或退出。
菜单：状态一行字（如“3 个 AI 正在工作 · 今日合格 12”）、打开主窗口、暂停所有 AI / 继续、退出（先确认）。
有未处理的紧急提醒时图标带红点。

实现只用安装包里已经有的、可商用协议的库：
- Mac：PyObjC（MIT）的 NSStatusItem，在主线程上（和 pywebview 的窗口同一个 Cocoa 运行循环）
- Windows：pythonnet（MIT）调用 .NET 自带的 System.Windows.Forms.NotifyIcon，在单独的 STA 线程里跑自己的消息循环
（没有用 pystray：它是 LGPL-3.0）
图标文件：web/tray/（packaging/make_icons.py --tray 生成）。
"""
import sys
import threading

from . import paths

TEXT = {
    "zh": {"open": "打开主窗口", "pause": "暂停所有 AI", "resume": "继续所有 AI", "quit": "退出",
           "working": "{n} 个 AI 正在工作 · 今日合格 {q}", "idle": "AI 空闲 · 今日合格 {q}",
           "paused": "所有 AI 已暂停 · 今日合格 {q}", "urgent": "{n} 条紧急提醒未处理",
           "connecting": "正在连接后台服务…",
           "quit_title": "退出詹姆斯智能体？", "quit_body": "退出后定时任务会停止（监控、找客户、定时推送）。关闭窗口不会退出，软件会在后台继续工作。",
           "quit_ok": "退出", "cancel": "取消", "app": "詹姆斯智能体",
           "bg_title": "詹姆斯智能体仍在后台运行", "bg_body": "监控和定时推送会继续。点这个图标可以重新打开窗口或退出。"},
    "en": {"open": "Open main window", "pause": "Pause all AIs", "resume": "Resume all AIs", "quit": "Quit",
           "working": "{n} AIs working · {q} qualified today", "idle": "AIs idle · {q} qualified today",
           "paused": "All AIs paused · {q} qualified today", "urgent": "{n} urgent alerts open",
           "connecting": "Connecting to the background service…",
           "quit_title": "Quit James Agent?", "quit_body": "Scheduled work stops after quitting (monitoring, prospecting, scheduled push). Closing the window does not quit — the app keeps working in the background.",
           "quit_ok": "Quit", "cancel": "Cancel", "app": "James Agent",
           "bg_title": "James Agent is still running", "bg_body": "Monitoring and scheduled push continue. Click this icon to reopen the window or quit."},
}


def status_line(st: dict | None, lang: str) -> str:
    tx = TEXT.get(lang, TEXT["zh"])
    if not st:
        return tx["connecting"]
    if st.get("paused"):
        return tx["paused"].format(q=st.get("qualified_today", 0))
    if st.get("working"):
        return tx["working"].format(n=st["working"], q=st.get("qualified_today", 0))
    return tx["idle"].format(q=st.get("qualified_today", 0))


class Tray:
    """on_open()：显示窗口；on_toggle_pause(paused_now)：暂停 / 继续；on_quit()：确认后退出。"""

    def __init__(self, on_open, on_toggle_pause, on_quit, log=print):
        self.on_open, self.on_toggle_pause, self.on_quit, self.log = on_open, on_toggle_pause, on_quit, log
        self.state: dict | None = None
        self.lang = "zh"
        self.ok = False
        self._impl = None

    def start(self) -> bool:
        """创建图标；失败返回 False（这时关闭窗口照旧退出软件，免得软件在后台却没有入口）。"""
        try:
            if sys.platform == "darwin":
                self._impl = _MacTray(self)
            elif sys.platform == "win32":
                self._impl = _WinTray(self)
            else:
                return False
            self.ok = self._impl.start()
        except Exception as e:  # noqa: BLE001
            self.log(f"tray icon not available: {type(e).__name__}: {e}")
            self.ok = False
        return self.ok

    def update(self, st: dict | None):
        self.state = st
        if st and st.get("lang") in TEXT:
            self.lang = st["lang"]
        if self.ok:
            try:
                self._impl.update()
            except Exception as e:  # noqa: BLE001
                self.log(f"tray update failed: {e}")

    def hint_background(self):
        """第一次关闭窗口时提示“仍在后台运行”（Windows 托盘气泡；Mac 菜单栏图标本身就在，不弹窗）。"""
        if self.ok and hasattr(self._impl, "hint"):
            try:
                self._impl.hint()
            except Exception:  # noqa: BLE001
                pass

    def confirm_quit(self) -> bool:
        """Mac：Cmd+Q / 程序坞「退出」时，和菜单栏「退出」一样先确认（在主线程调用）。"""
        if sys.platform == "darwin" and self.ok:
            return _mac_confirm_quit(self)
        return True

    def stop(self):
        if self.ok:
            try:
                self._impl.stop()
            except Exception:  # noqa: BLE001
                pass

    def t(self, key: str) -> str:
        return TEXT.get(self.lang, TEXT["zh"])[key]


def _icon(name: str) -> str:
    return str(paths.WEB_DIR / "tray" / name)


# ------------------------------------------------------------------ Mac：菜单栏

_MAC: dict = {"tray": None, "cls": None}


def _mac_target_class():
    """菜单项的点击目标（Objective-C 类，全进程只定义一次）。"""
    if _MAC["cls"] is None:
        import AppKit

        class JamesTrayTarget(AppKit.NSObject):
            def openWindow_(self, sender):
                _MAC["tray"].on_open()

            def togglePause_(self, sender):
                t = _MAC["tray"]
                threading.Thread(target=t.on_toggle_pause, args=(bool((t.state or {}).get("paused")),),
                                 daemon=True).start()

            def quitApp_(self, sender):
                t = _MAC["tray"]
                if _mac_confirm_quit(t):
                    t.on_quit()
        _MAC["cls"] = JamesTrayTarget
    return _MAC["cls"]

def _mac_confirm_quit(tray: Tray) -> bool:
    """“退出后定时任务会停止”确认框（主线程）。菜单栏「退出」和 Cmd+Q / 程序坞「退出」共用。"""
    import AppKit
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    alert = AppKit.NSAlert.alloc().init()
    alert.setMessageText_(tray.t("quit_title"))
    alert.setInformativeText_(tray.t("quit_body"))
    alert.addButtonWithTitle_(tray.t("quit_ok"))
    alert.addButtonWithTitle_(tray.t("cancel"))
    return alert.runModal() == AppKit.NSAlertFirstButtonReturn


class _MacTray:
    def __init__(self, tray: Tray):
        self.tray = tray
        self.item = None

    def start(self) -> bool:
        import AppKit
        from PyObjCTools import AppHelper
        tray = self.tray
        _MAC["tray"] = tray
        self.target = _mac_target_class().alloc().init()
        done = threading.Event()
        err = []

        def create():
            try:
                bar = AppKit.NSStatusBar.systemStatusBar()
                self.item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
                img = AppKit.NSImage.alloc().initWithContentsOfFile_(_icon("tray-mac.png"))
                if img is not None:
                    img.setSize_((18, 18))
                    img.setTemplate_(True)          # 系统按深色 / 浅色菜单栏自动着色
                    self.item.button().setImage_(img)
                else:
                    self.item.button().setTitle_("JA")
                self.item.button().setToolTip_(tray.t("app"))
                menu = AppKit.NSMenu.alloc().init()
                menu.setAutoenablesItems_(False)
                self.mi_status = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    status_line(None, tray.lang), None, "")
                self.mi_status.setEnabled_(False)
                self.mi_urgent = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
                self.mi_urgent.setEnabled_(False)
                self.mi_urgent.setHidden_(True)
                self.mi_open = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    tray.t("open"), "openWindow:", "")
                self.mi_pause = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    tray.t("pause"), "togglePause:", "")
                self.mi_quit = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    tray.t("quit"), "quitApp:", "")
                for mi in (self.mi_open, self.mi_pause, self.mi_quit):
                    mi.setTarget_(self.target)
                for mi in (self.mi_status, self.mi_urgent, AppKit.NSMenuItem.separatorItem(), self.mi_open,
                           self.mi_pause, AppKit.NSMenuItem.separatorItem(), self.mi_quit):
                    menu.addItem_(mi)
                self.menu = menu
                self.item.setMenu_(menu)
            except Exception as e:  # noqa: BLE001
                err.append(e)
            finally:
                done.set()

        if threading.current_thread() is threading.main_thread():
            create()
        else:
            AppHelper.callAfter(create)
            done.wait(10)
        if err:
            raise err[0]
        return self.item is not None

    def update(self):
        import AppKit
        from PyObjCTools import AppHelper
        tray, st = self.tray, self.tray.state

        def apply():
            if self.item is None:
                return
            self.mi_status.setTitle_(status_line(st, tray.lang))
            self.mi_open.setTitle_(tray.t("open"))
            self.mi_pause.setTitle_(tray.t("resume") if st and st.get("paused") else tray.t("pause"))
            self.mi_pause.setEnabled_(bool(st))
            self.mi_quit.setTitle_(tray.t("quit"))
            n = int((st or {}).get("urgent") or 0)
            self.mi_urgent.setHidden_(n == 0)
            self.mi_urgent.setTitle_(tray.t("urgent").format(n=n) if n else "")
            btn = self.item.button()
            if n:      # 红点：有未处理的紧急提醒
                red = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    "●", {AppKit.NSForegroundColorAttributeName: AppKit.NSColor.systemRedColor(),
                          AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(9)})
                btn.setAttributedTitle_(red)
            else:
                btn.setTitle_("")
            btn.setToolTip_(tray.t("app") + " · " + status_line(st, tray.lang))
        AppHelper.callAfter(apply)

    def stop(self):
        import AppKit
        from PyObjCTools import AppHelper

        def rm():
            if self.item is not None:
                AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
                self.item = None
        AppHelper.callAfter(rm)


# ------------------------------------------------------------------ Windows：系统托盘

class _WinTray:
    def __init__(self, tray: Tray):
        self.tray = tray
        self.icon = None
        self._dirty = threading.Event()
        self._hint = threading.Event()
        self._quit = threading.Event()

    def start(self) -> bool:
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Threading import ApartmentState, Thread, ThreadStart
        ready = threading.Event()
        err = []

        def run():
            try:
                from System.Drawing import Icon
                from System.Windows.Forms import (Application, ContextMenuStrip, MessageBox, MessageBoxButtons,
                                                  MessageBoxIcon, DialogResult, MouseButtons, NotifyIcon,
                                                  ToolStripMenuItem, ToolStripSeparator, Timer, ToolTipIcon)
                tray = self.tray
                self.ico_normal = Icon(_icon("tray.ico"))
                self.ico_alert = Icon(_icon("tray-alert.ico"))
                icon = NotifyIcon()
                icon.Icon = self.ico_normal
                icon.Text = tray.t("app")
                menu = ContextMenuStrip()
                self.mi_status = ToolStripMenuItem(status_line(None, tray.lang))
                self.mi_status.Enabled = False
                self.mi_urgent = ToolStripMenuItem("")
                self.mi_urgent.Enabled = False
                self.mi_urgent.Visible = False
                self.mi_open = ToolStripMenuItem(tray.t("open"))
                self.mi_pause = ToolStripMenuItem(tray.t("pause"))
                self.mi_quit = ToolStripMenuItem(tray.t("quit"))
                self.mi_open.Click += lambda s, e: tray.on_open()
                self.mi_pause.Click += lambda s, e: threading.Thread(
                    target=tray.on_toggle_pause, args=(bool((tray.state or {}).get("paused")),), daemon=True).start()

                def ask_quit(s, e):
                    r = MessageBox.Show(tray.t("quit_body"), tray.t("quit_title"), MessageBoxButtons.OKCancel,
                                        MessageBoxIcon.Question)
                    if r == DialogResult.OK:
                        tray.on_quit()
                self.mi_quit.Click += ask_quit
                for mi in (self.mi_status, self.mi_urgent, ToolStripSeparator(), self.mi_open, self.mi_pause,
                           ToolStripSeparator(), self.mi_quit):
                    menu.Items.Add(mi)
                icon.ContextMenuStrip = menu

                def click(s, e):
                    if e.Button == MouseButtons.Left:
                        tray.on_open()
                icon.MouseClick += click
                icon.Visible = True
                self.icon = icon
                # 所有界面更新都在这个线程里做：定时器每 0.5 秒看一下有没有新状态
                timer = Timer()
                timer.Interval = 500

                def tick(s, e):
                    if self._quit.is_set():
                        timer.Stop()
                        icon.Visible = False
                        icon.Dispose()
                        Application.ExitThread()
                        return
                    if self._hint.is_set():
                        self._hint.clear()
                        icon.ShowBalloonTip(5000, tray.t("bg_title"), tray.t("bg_body"), ToolTipIcon.Info)
                    if self._dirty.is_set():
                        self._dirty.clear()
                        self._apply()
                timer.Tick += tick
                timer.Start()
                ready.set()
                Application.Run()
            except Exception as e:  # noqa: BLE001
                err.append(e)
                ready.set()

        th = Thread(ThreadStart(run))
        th.SetApartmentState(ApartmentState.STA)
        th.IsBackground = True
        th.Start()
        ready.wait(15)
        if err:
            raise err[0]
        return self.icon is not None

    def _apply(self):
        tray, st = self.tray, self.tray.state
        line = status_line(st, tray.lang)
        self.mi_status.Text = line
        self.mi_open.Text = tray.t("open")
        self.mi_pause.Text = tray.t("resume") if st and st.get("paused") else tray.t("pause")
        self.mi_pause.Enabled = bool(st)
        self.mi_quit.Text = tray.t("quit")
        n = int((st or {}).get("urgent") or 0)
        self.mi_urgent.Visible = n > 0
        self.mi_urgent.Text = tray.t("urgent").format(n=n) if n else ""
        self.icon.Icon = self.ico_alert if n else self.ico_normal
        self.icon.Text = (tray.t("app") + " · " + line)[:63]      # NotifyIcon 提示最多 63 个字符

    def update(self):
        self._dirty.set()

    def hint(self):
        self._hint.set()

    def stop(self):
        self._quit.set()


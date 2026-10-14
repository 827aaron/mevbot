"""电脑桌面弹窗（PRD 6.4 高意向提醒；7.2 通知号故障时的备用通道）。失败时静默（界面内提醒中心仍会显示）。"""
import platform
import subprocess
import threading
from . import background


def desktop(title: str, body: str):
    def run():
        try:
            sysname = platform.system()
            if sysname == "Darwin":
                script = f'display notification {_q(body[:220])} with title {_q(title[:80])}'
                subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
            elif sysname == "Windows":
                ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
                      "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                      "$x=$t.GetElementsByTagName('text');"
                      f"$x.Item(0).AppendChild($t.CreateTextNode({_ps(title[:80])}))|Out-Null;"
                      f"$x.Item(1).AppendChild($t.CreateTextNode({_ps(body[:220])}))|Out-Null;"
                      "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('James Agent').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=15, capture_output=True,
                               creationflags=0x08000000)
            else:
                subprocess.run(["notify-send", title[:80], body[:220]], timeout=10, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
    background.spawn(run, name="desktop-notification")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"

"""启动入口：启动本地服务（只监听 127.0.0.1）并自动打开浏览器。

    python run.py              # 默认端口 8765（被占用时自动换一个）
    python run.py --no-browser # 不自动打开浏览器
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("SALESTEAM_PORT", "8765"))


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/meta", timeout=1.5) as r:
            return "agents" in json.load(r)
    except Exception:  # noqa: BLE001
        return False


def free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit("找不到可用端口 / no free port")


ROOT = os.path.dirname(os.path.abspath(__file__))


def ensure_deps():
    """requirements.txt 变化时自动安装依赖（只用标准库，在导入第三方包之前运行）。"""
    import hashlib
    import subprocess
    req = os.path.join(ROOT, "requirements.txt")
    stamp = os.path.join(sys.prefix, ".salesteam-req-hash")
    digest = hashlib.sha256(open(req, "rb").read()).hexdigest()
    try:
        if open(stamp).read().strip() == digest:
            return
    except OSError:
        pass
    print("\n  正在安装/更新依赖，首次需要几分钟… / Installing dependencies, the first time takes a few minutes…\n")
    code = subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", req])
    if code != 0:
        print("\n  ❌ 依赖安装失败，请检查网络后重试。/ Dependency installation failed — check your internet and retry.\n")
        raise SystemExit(1)
    with open(stamp, "w") as f:
        f.write(digest)


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("需要 Python 3.12 或更高版本 / Python 3.12+ is required")
    if "--skip-deps" not in sys.argv:
        ensure_deps()
    open_browser = "--no-browser" not in sys.argv
    if already_running(DEFAULT_PORT):
        url = f"http://{HOST}:{DEFAULT_PORT}/"
        print(f"\n  软件已经在运行：{url}\n  Already running: {url}\n")
        if open_browser:
            webbrowser.open(url)
        return
    port = free_port(DEFAULT_PORT)
    url = f"http://{HOST}:{port}/"

    import uvicorn
    from salesteam import server

    server.startup()

    def opener():
        for _ in range(60):
            time.sleep(0.25)
            if already_running(port):
                if open_browser:
                    webbrowser.open(url)
                return

    threading.Thread(target=opener, daemon=True).start()
    from salesteam import APP_NAME
    print(f"\n  ✅ {APP_NAME['zh']} 已启动 / {APP_NAME['en']} is running:  {url}\n"
          f"  数据目录 / Data folder: {server.paths.DATA_DIR}\n"
          f"  关闭这个窗口即可退出 / Close this window to quit.\n")
    uvicorn.run(server.app, host=HOST, port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

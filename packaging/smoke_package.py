"""Verify the packaged backend and UI assets without real services or a GUI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/packaged-smoke.json"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "salesteam" / "__init__.py"
    expected = re.search(r'^VERSION = "([^"]+)"', source.read_text(encoding="utf-8"), re.M).group(1)
    with tempfile.TemporaryDirectory(prefix="jamesagent-packaged-") as data:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, SALESTEAM_DATA=data, SALESTEAM_PORT=str(port),
                   SALESTEAM_NO_SCHEDULER="1", SALESTEAM_HEADLESS="1",
                   SALESTEAM_NO_BROWSER_DOWNLOAD="1", SALESTEAM_PARENT_PID="0", SALESTEAM_QA_FIXTURES="")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen([str(args.executable.resolve()), "--server"], env=env)
        try:
            for _ in range(120):
                if proc.poll() is not None:
                    raise RuntimeError(f"packaged server exited: {proc.returncode}")
                try:
                    with opener.open(base + "/api/meta", timeout=2) as response:
                        meta = json.load(response)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("packaged service startup timed out")
            if meta.get("version") != expected:
                raise RuntimeError(f"wrong packaged version: {meta.get('version')} != {expected}")
            checks = []
            for path in ("/", "/api/manager/state", "/api/scout/status", "/static/js/operations.js"):
                with opener.open(base + path, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"packaged endpoint failed: {path}")
                checks.append(path)
            installers = list(Path("dist").glob("*.dmg")) + list(Path("dist").glob("JamesAgent-Setup-*.exe"))
            result = {"version": expected, "commit": os.environ.get("GITHUB_SHA", ""), "checks": checks,
                      "sha256": {p.name: hashlib.file_digest(p.open("rb"), "sha256").hexdigest() for p in installers}}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log = Path(data) / "app.log"
            if log.exists():
                print(log.read_text(encoding="utf-8")[-4000:])


if __name__ == "__main__":
    main()

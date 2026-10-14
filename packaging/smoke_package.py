"""Verify the packaged backend and UI assets without real services or a GUI."""
import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from tls_fixture import TLSFixture


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/packaged-smoke.json"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "salesteam" / "__init__.py"
    expected = re.search(r'^VERSION = "([^"]+)"', source.read_text(encoding="utf-8"), re.M).group(1)
    with tempfile.TemporaryDirectory(prefix="jamesagent-packaged-") as data, \
            TLSFixture() as fixture, TLSFixture() as untrusted:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, SALESTEAM_DATA=data, SALESTEAM_PORT=str(port),
                   SALESTEAM_NO_SCHEDULER="1", SALESTEAM_HEADLESS="1",
                   SALESTEAM_NO_BROWSER_DOWNLOAD="1", SALESTEAM_PARENT_PID="0", SALESTEAM_QA_FIXTURES="",
                   SSL_CERT_FILE=str(fixture.ca_file))
        for name in ("NO_PROXY", "no_proxy"):
            env[name] = ",".join(filter(None, (env.get(name, ""), "localhost", "127.0.0.1")))
        # Verify the embedded product selection, without an environment override.
        env.pop("SALESTEAM_EDITION", None)
        embedded = json.loads((source.parent / "edition.json").read_text(encoding="utf-8"))["edition"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"

        def request(path, data):
            req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=45) as response:
                return json.load(response)

        def reject_certificate(url):
            try:
                request("/api/relay/models", {"protocol": "openai", "base_url": url, "api_key": "tls-test-key"})
            except urllib.error.HTTPError as error:
                body = json.load(error)
                detail = str(body.get("params", {}).get("detail", "")).lower()
                if error.code == 400 and body.get("error") == "relay_unreachable" and "certificate" in detail:
                    return True
                raise RuntimeError(f"unexpected certificate rejection: {body}") from error
            raise RuntimeError("invalid certificate was accepted")

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
            if meta.get("edition", {}).get("id") != embedded:
                raise RuntimeError("wrong embedded packaged edition")
            checks = []
            for path in ("/", "/api/manager/state", "/api/scout/status", "/api/source-pool", "/api/edition",
                         "/static/js/operations.js", "/static/js/pages-config.js"):
                with opener.open(base + path, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"packaged endpoint failed: {path}")
                checks.append(path)
            with opener.open(base + "/api/source-pool", timeout=5) as response:
                pool = json.load(response)
            if {p["id"] for p in pool.get("platforms", [])} != {"x", "reddit", "facebook", "instagram", "youtube"}:
                raise RuntimeError("missing packaged platform configuration")
            with opener.open(base + "/api/diagnostics/export", timeout=10) as response:
                with zipfile.ZipFile(BytesIO(response.read())) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    if manifest.get("version") != expected:
                        raise RuntimeError("wrong packaged diagnostic version")
            checks.append("/api/diagnostics/export")
            models = request("/api/relay/models", {"protocol": "openai", "base_url": fixture.base_url,
                                                  "api_key": "tls-test-key"})
            if fixture.model not in models.get("models", []):
                raise RuntimeError("packaged HTTPS model listing failed")
            configured = request("/api/relay", {"role": "decision", "protocol": "openai",
                                                 "base_url": fixture.base_url, "api_key": "tls-test-key",
                                                 "models": fixture.model})
            if configured.get("configured", {}).get("model") != fixture.model:
                raise RuntimeError("packaged decision configuration failed")
            answer = request("/api/agents/decision/test", {})
            if answer.get("choice") != "buyer":
                raise RuntimeError("packaged HTTPS decision SDK call failed")
            tls_checks = {"models_read": True, "decision_saved": True, "decision_test": True,
                          "untrusted_ca_rejected": reject_certificate(untrusted.base_url),
                          "wrong_hostname_rejected": reject_certificate(fixture.wrong_host_url)}
            # No Key or model completion: only the public catalogue on the reported provider.
            try:
                public = request("/api/relay/models", {"protocol": "openai",
                                 "base_url": "https://nano-gpt.com/api/v1", "api_key": ""})
                if not public.get("models"):
                    raise RuntimeError("public HTTPS catalogue returned no models")
                tls_checks["nano_gpt_public_models"] = len(public["models"])
            except urllib.error.HTTPError as error:
                body = json.load(error)
                if body.get("error") != "key_invalid":
                    raise RuntimeError(f"public provider HTTPS check failed: {body}") from error
                tls_checks["nano_gpt_public_tls"] = "verified; authentication required"
            installers = list(Path("dist").glob("*.dmg")) + list(Path("dist").glob("JamesAgent-Setup-*.exe"))
            result = {"version": expected, "edition": embedded, "commit": os.environ.get("GITHUB_SHA", ""), "checks": checks,
                      "tls": tls_checks, "sha256": {p.name: digest(p) for p in installers}}
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

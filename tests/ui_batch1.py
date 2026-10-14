"""Controlled browser regression for batch 1; no external services or real data.

Run: .venv/bin/python tests/ui_batch1.py --output /tmp/jamesagent-batch1-validation
Requires the already-installed Playwright Python package and a local Chromium.
"""
import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", default="/usr/bin/chromium")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jamesagent-ui-batch1-") as data:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, SALESTEAM_DATA=data, SALESTEAM_PORT=str(port), SALESTEAM_NO_SCHEDULER="1",
                   SALESTEAM_NO_BROWSER_DOWNLOAD="1", SALESTEAM_QA_FIXTURES="", PYTHONDONTWRITEBYTECODE="1")
        server_log = (args.output / "browser-server.log").open("w")
        process = subprocess.Popen([sys.executable, "run.py", "--skip-deps", "--no-browser"],
                                   cwd=root, env=env, stdout=server_log, stderr=subprocess.STDOUT)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, payload=None):
            body = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=body,
                                         headers={"Content-Type": "application/json"} if body else {})
            with opener.open(req, timeout=5) as response:
                return json.load(response)

        def seed_setting(key, value):
            with sqlite3.connect(Path(data) / "salesteam.db") as conn:
                conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, json.dumps(value)))

        def checkpoint(name, **details):
            print(json.dumps({"passed": name, **details}, ensure_ascii=False), flush=True)

        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"server exited: {process.returncode}")
                try:
                    request("/api/meta")
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError("server did not start")
            print("CONTROLLED_SERVER", base, "TEMP_DATA", data, flush=True)
            seed_setting("onboarding", {"done": True})
            seed_setting("agreement", {"accepted_at": time.time()})
            seed_setting("onboarding_skips", {"steps": ["ai"]})
            seed_setting("company_profile", {
                "name": {"text": "QA Company", "sources": ["https://qa.example/about"]},
                "summary": {"text": "QA initial summary", "sources": ["pdf:qa.pdf", "interview", "input:intro"]},
                "product_lines": [{"name": "QA product", "detail": "Keep detail", "sources": ["https://qa.example/products"]}],
            })
            with sqlite3.connect(Path(data) / "salesteam.db") as conn:
                now = time.time()
                conn.execute("INSERT INTO agent_chats(agent,role,content,meta,ts) VALUES(?,?,?,?,?)",
                             ("manager", "assistant", "QA feedback remains visible", "{}", now))
                conn.execute("INSERT INTO onboarding(status,profile,created_at,updated_at) VALUES(?,?,?,?)",
                             ("done", "{}", now, now))

            blocked, errors = [], []
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                                             args=["--no-sandbox", "--disable-background-networking"])
                try:
                    for lang in ("zh", "en"):
                        for width, height in ((1100, 700), (1280, 800)):
                            context = browser.new_context(viewport={"width": width, "height": height})

                            def local_only(route):
                                if route.request.url.startswith(base + "/"):
                                    route.continue_()
                                else:
                                    blocked.append(route.request.url)
                                    route.abort()
                            context.route("**/*", local_only)
                            context.add_init_script("localStorage.setItem('ja-prefs', JSON.stringify({mgrOpen:true, theme:'light'}))")
                            seed_setting("ui_lang", lang)
                            page = context.new_page()
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            try:
                                page.goto(base + "/#/learning/playbook")
                                page.locator("#company-editor").wait_for()
                                assert not page.evaluate("STATE.mgrOpen"), "narrow viewport must ignore stored open preference"
                                closed = page.locator("#view").bounding_box()
                                page.locator("#mgr-toggle").click()
                                page.locator("#mgr-panel").wait_for(state="visible")
                                opened = page.locator("#view").bounding_box()
                                assert abs(opened["width"] - closed["width"]) < 1, (closed, opened)
                                page.screenshot(path=str(args.output / f"panel-{lang}-{width}-light.png"))
                                page.locator("#theme-btn").click()
                                page.screenshot(path=str(args.output / f"panel-{lang}-{width}-dark.png"))
                                page.locator("#mgr-toggle").click()
                                page.evaluate("MGR.managerUpdated()")
                                page.locator(".mgr-dock").wait_for()
                                assert not page.evaluate("STATE.mgrOpen")
                                page.screenshot(path=str(args.output / f"feedback-{lang}-{width}.png"))
                                page.locator('.mgr-dock [data-action="closeManagerDock"]').click()
                                page.locator('.mgr-dock').wait_for(state="hidden")
                                checkpoint("manager overlay and nonintrusive feedback", lang=lang, width=width,
                                           closed_width=closed["width"], opened_width=opened["width"])
                                assert page.locator('#nav [data-route="meeting"]').count() == 0
                                if lang == "en" and width == 1280:
                                    page.locator("#mgr-toggle").click()
                                    page.set_viewport_size({"width":1600, "height":900})
                                    page.wait_for_function("document.body.classList.contains('has-mgr-panel')")
                                    page.set_viewport_size({"width":width, "height":height})
                                    page.wait_for_function("!document.body.classList.contains('has-mgr-panel')")
                                    assert abs(page.locator("#view").bounding_box()["width"] - closed["width"]) < 1
                                    if page.evaluate("STATE.mgrOpen"):
                                        page.locator("#mgr-toggle").click()
                                    page.locator("#mgr-panel").wait_for(state="hidden")
                                    checkpoint("layout stays coherent across narrow and wide breakpoints")

                                summary = page.locator('[data-company-field="summary"]')
                                before = summary.input_value()
                                summary.fill("Discard this edit")
                                page.locator("#company-cancel").click()
                                page.wait_for_function("document.querySelector('[data-company-field=summary]').value === " + json.dumps(before))
                                summary.fill("Unsaved guarded edit")
                                page.locator('#nav [data-route="dashboard"]').click()
                                page.locator("#confirm-ok").wait_for()
                                page.locator("#modal .modal-footer [data-x]").click()
                                assert summary.input_value() == "Unsaved guarded edit"
                                assert page.evaluate("STATE.route") == "learning"
                                # Language changes and repeated current navigation must also protect edits.
                                other = "en" if lang == "zh" else "zh"
                                page.locator(f'#lang-seg [data-lang="{other}"]').click()
                                page.locator("#confirm-ok").wait_for()
                                page.locator("#modal .modal-footer [data-x]").click()
                                assert page.evaluate("I18N.lang") == lang
                                page.evaluate("void UI.go('learning/playbook')")
                                page.locator("#confirm-ok").wait_for()
                                page.locator("#modal .modal-footer [data-x]").click()
                                assert summary.input_value() == "Unsaved guarded edit"
                                saved_text = f"QA saved {lang} {width}"
                                summary.fill(saved_text)
                                with page.expect_response(lambda response: response.request.method == "PUT" and response.url == base + "/api/company") as saved:
                                    page.locator("#company-save").click()
                                assert saved.value.status == 200
                                page.wait_for_function("document.querySelector('#company-save').disabled")
                                page.reload()
                                page.locator("#company-editor").wait_for()
                                assert summary.input_value() == saved_text
                                assert request("/api/company")["product_lines"][0]["detail"] == "Keep detail"
                                assert page.locator('#company-editor a[href="https://qa.example/about"]').count() == 1
                                editor_text = page.locator("#company-editor").inner_text()
                                assert "qa.pdf" in editor_text
                                if lang == "en":
                                    assert not re.search(r"[\u4e00-\u9fff]", editor_text), editor_text
                                page.screenshot(path=str(args.output / f"company-{lang}-{width}.png"))
                                checkpoint("company save refresh cancel and leave guards", lang=lang, width=width)
                                for old_tab in ("mail", "platform"):
                                    page.goto(base + "/#/accounts/" + old_tab)
                                    page.locator("#tg-id").wait_for()
                                    assert page.evaluate("STATE.tabs.accounts") == "tg"
                                    assert page.locator('#view [data-tab="mail"], #view [data-tab="platform"]').count() == 0
                                checkpoint("hidden planned navigation and old account links fall back to TG", lang=lang, width=width)

                                page.locator('#nav [data-route="dashboard"]').click()
                                page.locator('#wb-pause [data-on="1"]').wait_for()
                                page.locator('#wb-pause [data-on="1"]').click()
                                page.locator("#confirm-ok").click()
                                page.locator('#wb-pause [data-on="0"]').wait_for()
                                assert request("/api/desktop/state")["paused"]
                                page.locator('#wb-pause [data-on="0"]').click()
                                page.locator('#wb-pause [data-on="1"]').wait_for()
                                assert not request("/api/desktop/state")["paused"]
                                started = time.monotonic()
                                request("/api/ai/pause", {"on": True})
                                page.locator('#wb-pause [data-on="0"]').wait_for(timeout=15000)
                                pause_delay = time.monotonic() - started
                                started = time.monotonic()
                                request("/api/ai/pause", {"on": False})
                                page.locator('#wb-pause [data-on="1"]').wait_for(timeout=15000)
                                resume_delay = time.monotonic() - started
                                checkpoint("pause resume and simulated tray synchronization", lang=lang, width=width,
                                           pause_seconds=round(pause_delay, 3), resume_seconds=round(resume_delay, 3),
                                           both_within_10_seconds=pause_delay <= 10 and resume_delay <= 10)

                                # The completion copy must reflect the actual stored evaluation status.
                                for status in ("done", "failed", "applied"):
                                    seed_setting("model_eval", {"status": status})
                                    page.goto(base + "/#/onboarding?step=5&qa_case=" + status)
                                    page.locator("#ob-card").wait_for()
                                    expected = page.evaluate("t('company.modelsDone')" if status == "applied" else "t('company.modelsPending')")
                                    page.wait_for_function("expected => document.querySelector('#ob-card')?.innerText.includes(expected)", arg=expected)
                                    assert expected in page.locator("#ob-card").inner_text(), (status, expected, page.locator("#ob-card").inner_text())
                                    assert page.evaluate("t('company.profileDone')") in page.locator("#ob-card").inner_text()
                                checkpoint("onboarding completion reflects actual model status", lang=lang, width=width)
                                page.screenshot(path=str(args.output / f"onboarding-{lang}-{width}.png"))
                                if lang == "en" and width == 1280:
                                    profile = {"company": request("/api/company"), "icp": {
                                        "name": "QA customer profile", "product": "QA product", "customer_types": ["resellers"],
                                    }, "answers": {}}
                                    with sqlite3.connect(Path(data) / "salesteam.db") as conn:
                                        conn.execute("UPDATE onboarding SET status='review',profile=?", (json.dumps(profile),))
                                    seed_setting("model_eval", {"status": "done"})
                                    page.goto(base + "/#/onboarding?step=4&qa_case=confirmation")
                                    page.locator("#cf-ok").wait_for()
                                    page.locator('#cf-company [data-company-field="summary"]').fill("QA corrected on confirmation")
                                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/confirm")) as confirmed:
                                        page.locator("#cf-ok").click()
                                    assert confirmed.value.status == 200
                                    assert request("/api/company")["summary"]["text"] == "QA corrected on confirmation"
                                    expected = page.evaluate("t('company.modelsPending')")
                                    page.wait_for_function("expected => document.querySelector('#ob-card')?.innerText.includes(expected)", arg=expected)
                                    page.screenshot(path=str(args.output / "confirmation-en-1280.png"))
                                    checkpoint("confirmation editor persists corrections without claiming models applied")
                            except Exception:
                                page.screenshot(path=str(args.output / f"failure-{lang}-{width}.png"))
                                print("FAILURE_STATE", page.evaluate("({route:STATE.route, hash:location.hash, lang:I18N.lang})"), flush=True)
                                raise
                            finally:
                                context.close()
                    assert not errors, errors
                    assert not blocked, blocked
                    checkpoint("no page errors or external requests", page_errors=errors, blocked_external_requests=blocked)
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            server_log.close()
            print("PROCESS_CLEANED", process.pid, process.returncode, flush=True)


if __name__ == "__main__":
    main()

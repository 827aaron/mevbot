"""Real Chromium checks for daily operations, with local mock AI only.

Run .venv/bin/python tests/ui_batch2.py --output /tmp/james-ui-batch2
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


def serve():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from salesteam import server
    import uvicorn
    server.startup()
    uvicorn.run(server.app, host='127.0.0.1', port=int(os.environ['SALESTEAM_PORT']), log_level='warning')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path)
    parser.add_argument('--browser', default='/usr/bin/chromium')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--langs', nargs='+', choices=('zh', 'en'), default=['zh', 'en'])
    parser.add_argument('--widths', nargs='+', type=int, default=[1100, 1280, 390])
    args = parser.parse_args()
    if args.serve:
        serve()
        return
    if args.output is None:
        parser.error('--output is required')
    from playwright.sync_api import sync_playwright
    root = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    checkpoints, failures, blocked = [], [], []
    with tempfile.TemporaryDirectory(prefix='james-ui-batch2-') as data:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = dict(os.environ, SALESTEAM_DATA=data, SALESTEAM_PORT=str(port),
                   SALESTEAM_NO_SCHEDULER='1', SALESTEAM_QA_FIXTURES='',
                   PYTHONDONTWRITEBYTECODE='1')
        log = (args.output / 'server.log').open('w')
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve'], cwd=root,
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path, payload=None, method=None):
            body = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + path, data=body, method=method,
                                         headers={'Content-Type': 'application/json'} if body else {})
            with opener.open(req, timeout=5) as response:
                return json.load(response)
        def setting(key, value):
            with sqlite3.connect(Path(data) / 'salesteam.db') as conn:
                conn.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, json.dumps(value)))
        def checkpoint(label, **extra):
            result = {'passed': label, **extra}
            checkpoints.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('server exited: ' + str(proc.returncode))
                try:
                    request('/api/meta')
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('local server did not start')
            setting('onboarding', {'done': True})
            setting('agreement', {'accepted_at': time.time()})
            setting('onboarding_skips', {'steps': ['ai']})
            profile = {'name': 'QA daily template', 'product': 'QA widgets',
                       'customer_types': ['wholesaler'], 'product_words': ['widget'], 'buy_words': ['buy'],
                       'qualified_grades': ['A', 'B'],
                       'grading_criteria': [{'name': 'fit', 'weight': 100, 'description': 'QA fit'}]}
            with sqlite3.connect(Path(data) / 'salesteam.db') as conn:
                now = time.time()
                conn.execute('INSERT INTO icp_templates(id,name,data,created_at,updated_at) VALUES(1,?,?,?,?)',
                             ('QA daily template', json.dumps(profile), now, now))
                conn.execute("UPDATE agent_configs SET provider='mock',model='qa-mock'")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=args.browser, headless=True,
                                             args=['--no-sandbox', '--disable-background-networking'])
                try:
                    for lang in args.langs:
                        for width in args.widths:
                            height = 844 if width < 500 else 700 if width == 1100 else 800
                            setting('ui_lang', lang)
                            setting('pause_all', {'on': False})
                            setting('manager_goal', {'enabled': False, 'icp_id': None, 'target': 10,
                                     'sources': ['google'], 'time_of_day': '09:00', 'max_retries': 1})
                            setting('scout_settings', {'enabled': False, 'icp_id': None, 'auto_queue': False})
                            setting('joining', {'assisted_join': False})
                            with sqlite3.connect(Path(data) / 'salesteam.db') as conn:
                                conn.execute('DELETE FROM manager_commands')
                                conn.execute('DELETE FROM manager_plans')
                                conn.execute('DELETE FROM manager_reports')
                                conn.execute('DELETE FROM tasks')
                                conn.execute('DELETE FROM leads')
                            context = browser.new_context(viewport={'width': width, 'height': height})
                            def local_only(route):
                                if route.request.url.startswith(base + '/'):
                                    route.continue_()
                                else:
                                    blocked.append(route.request.url)
                                    route.abort()
                            context.route('**/*', local_only)
                            page = context.new_page()
                            page.on('pageerror', lambda error: failures.append(str(error)))
                            try:
                                page.goto(base + '/#/run')
                                page.locator('#ops-goal-form').wait_for()
                                assert not page.locator('#ops-enabled').is_checked()
                                page.locator('#ops-goal-save').click()
                                assert request('/api/manager/state')['goal']['icp_id'] is None
                                page.locator('#ops-enabled').check()
                                page.locator('#ops-icp').select_option('1')
                                page.locator('#ops-target').fill('12')
                                page.locator('#ops-time').fill('09:10')
                                page.locator('#ops-retries').select_option('2')
                                page.locator('#ops-goal-save').click()
                                page.wait_for_function("document.querySelector('#ops-target')?.value === '12' && document.querySelector('#ops-goal-save')?.disabled === false")
                                assert request('/api/manager/state')['goal']['target'] == 12
                                page.reload()
                                page.locator('#ops-goal-form').wait_for()
                                assert page.locator('#ops-target').input_value() == '12'
                                checkpoint('daily goal save and refresh', lang=lang, width=width)
                                # Client cannot bypass explicit confirmation; a proposal is persisted separately.
                                page.locator('[data-action="opsCommand"]').click()
                                page.locator('#ops-command-input').fill('Daily find 17 leads using Google at 09:30')
                                page.locator('#ops-command-send').click()
                                page.locator('#ops-command-result [data-action="opsConfirm"]').wait_for()
                                assert request('/api/manager/state')['goal']['target'] == 12
                                page.locator('#modal .modal-footer [data-action="closeModal"]').click()
                                page.reload()
                                page.locator('#ops-proposal-wrap [data-action="opsConfirm"]').wait_for()
                                page.locator('#ops-proposal-wrap [data-action="opsDismiss"]').click()
                                page.wait_for_function("!document.querySelector('#ops-proposal-wrap [data-action=opsConfirm]')")
                                page.reload()
                                page.locator('#ops-goal-form').wait_for()
                                assert page.locator('#ops-proposal-wrap [data-action="opsConfirm"]').count() == 0
                                assert request('/api/manager/state')['goal']['target'] == 12
                                page.locator('[data-action="opsCommand"]').click()
                                page.locator('#ops-command-input').fill('Daily find 17 leads using Google at 09:30')
                                page.locator('#ops-command-send').click()
                                page.locator('#ops-command-result [data-action="opsConfirm"]').wait_for()
                                page.locator('#ops-command-result [data-action="opsConfirm"]').click()
                                page.wait_for_function("document.querySelector('#ops-target')?.value === '17'")
                                assert request('/api/manager/state')['goal']['target'] == 17
                                checkpoint('AI proposal persists and requires user confirmation', lang=lang, width=width)
                                # Real persisted report and task/lead rows, so browser validates
                                # date selection, measured progress and immutable plan snapshot.
                                today = time.strftime('%Y-%m-%d')
                                goal = request('/api/manager/state')['goal']
                                with sqlite3.connect(Path(data) / 'salesteam.db') as conn:
                                    now = time.time()
                                    pid = conn.execute("INSERT INTO manager_plans(day,goal,status,attempts,created_at,updated_at) VALUES(?,?,'met',1,?,?)",
                                                       (today, json.dumps(goal), now, now)).lastrowid
                                    tid = f'qa-ui-{lang}-{width}'
                                    conn.execute("INSERT INTO tasks(id,kind,icp_id,params,status,target,stats,created_by,parent_id,goal_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                                 (tid, 'research', 1, json.dumps(goal), 'done', 17, json.dumps({'qualified': 17}), 'manager', f'manager:{pid}:1', today, now))
                                    for n in range(17):
                                        conn.execute("INSERT INTO leads(company_name,company_key,grade,qualified,status,icp_id,task_id,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,?)",
                                                     (f'QA {n}', f'qa-{n}', 'A', 'qualified', 1, tid, now, now))
                                    report = {'day': today, 'target': 17, 'qualified': 17, 'remaining': 0,
                                              'status': 'met', 'attempts': 1, 'counts': {'processed': 17}, 'last_error': ''}
                                    conn.execute('INSERT INTO manager_reports(day,data,updated_at) VALUES(?,?,?)',
                                                 (today, json.dumps(report), now))
                                page.reload()
                                page.locator('#ops-goal-form').wait_for()
                                expected_progress = page.evaluate("t('ops.progress', {done:17,target:17})")
                                page.wait_for_function("expected => document.querySelector('#ops-report-body')?.innerText.includes(expected)", arg=expected_progress)
                                assert len(page.locator('#ops-report-body .ops-plan').all()) == 1
                                page.locator('#ops-report-day').fill('2020-01-01')
                                page.locator('#ops-report-day').dispatch_event('change')
                                no_plan = page.evaluate("t('ops.noPlans')")
                                page.wait_for_function("expected => document.querySelector('#ops-report-body')?.innerText.includes(expected)", arg=no_plan)
                                page.locator('#ops-report-day').fill(today)
                                page.locator('#ops-report-day').dispatch_event('change')
                                page.wait_for_function("expected => document.querySelector('#ops-report-body')?.innerText.includes(expected)", arg=expected_progress)
                                checkpoint('persisted daily report progress and date selection', lang=lang, width=width)
                                request('/api/ai/pause', {'on': True})
                                expected = page.evaluate("t('ops.block.ai_paused')")
                                page.wait_for_function("expected => document.querySelector('#ops-manager-state')?.innerText.includes(expected)", arg=expected, timeout=10000)
                                assert expected in page.locator('#ops-manager-state').inner_text()
                                request('/api/ai/pause', {'on': False})
                                page.screenshot(path=str(args.output / f'manager-{lang}-{width}.png'))
                                if lang == 'en':
                                    text = page.locator('#ops-manager').inner_text()
                                    assert not re.search(r'[\u4e00-\u9fff]', text), text
                                # Move with direct existing routes: mobile navigation need not expose all items at once.
                                page.goto(base + '/#/channels')
                                page.locator('#scout-form').wait_for()
                                assert not page.locator('#scout-enabled').is_checked()
                                assert not page.locator('#scout-auto-queue').is_checked()
                                page.locator('#scout-icp').select_option('1')
                                page.locator('#scout-auto-queue').check()
                                page.locator('#scout-save').click()
                                page.locator('#scout-risk-agree').wait_for()
                                page.locator('#confirm-ok').click()
                                assert page.locator('#scout-risk-agree').is_visible()
                                assert not request('/api/scout/settings')['auto_queue']
                                page.locator('#modal .modal-footer [data-x]').click()
                                assert not request('/api/scout/settings')['auto_queue']
                                page.locator('#scout-save').click()
                                page.locator('#scout-risk-agree').check()
                                page.locator('#confirm-ok').click()
                                page.locator('#scout-form').wait_for()
                                page.wait_for_function("document.querySelector('#scout-auto-queue')?.checked")
                                assert request('/api/scout/settings')['auto_queue']
                                assert request('/api/tg/join')['assisted']
                                page.reload()
                                page.locator('#scout-form').wait_for()
                                assert page.locator('#scout-auto-queue').is_checked()
                                if lang == 'en':
                                    text = page.locator('#ops-scout').inner_text()
                                    assert not re.search(r'[\u4e00-\u9fff]', text), text
                                page.screenshot(path=str(args.output / f'scout-{lang}-{width}.png'))
                                checkpoint('high score queue default off, risk validation, cancel and confirmed save', lang=lang, width=width)
                                # New cards must not add horizontal page overflow at any requested size.
                                overflow = page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1')
                                assert not overflow, f'horizontal overflow at {width}'
                                checkpoint('operations responsive layout and localized labels', lang=lang, width=width)
                            except Exception:
                                page.screenshot(path=str(args.output / f'failure-{lang}-{width}.png'))
                                raise
                            finally:
                                context.close()
                    assert not failures, failures
                    assert not blocked, blocked
                    checkpoint('no browser errors or external requests', errors=failures, external=blocked)
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            log.close()
            (args.output / 'results.json').write_text(json.dumps({'checkpoints': checkpoints,
                'errors': failures, 'blocked_external': blocked, 'server_stopped': proc.poll() is not None},
                ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

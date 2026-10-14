"""Batch 2 lifecycle and automation regressions, isolated and without external services."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from salesteam import channels, db, llm, paths, server, settings, tasks, vault
from salesteam.errors import UserError


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        db.close()
        vault.reset()
        data = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='james-batch2-test-')))
        for name, value in {'DATA_DIR': data, 'DB_PATH': data / 'salesteam.db',
                            'KEY_PATH': data / '.secret.key', 'UPLOAD_DIR': data / 'uploads',
                            'EXPORT_DIR': data / 'exports'}.items():
            self.enterContext(mock.patch.object(paths, name, value))
        self.enterContext(mock.patch.dict('os.environ', {'SALESTEAM_NO_SCHEDULER': '1',
                                                        'SALESTEAM_QA_FIXTURES': ''}))
        self.enterContext(mock.patch.object(tasks, '_RUNS', {}))
        self.addCleanup(db.close)
        self.addCleanup(vault.reset)
        server.startup()
        self.client = self.enterContext(TestClient(server.app, base_url='http://127.0.0.1'))
        self.today = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))
        self.day = '2026-10-15'
        profile = {'name': 'QA template', 'product': 'QA widgets',
                   'customer_types': ['wholesaler'], 'product_words': ['widget'],
                   'buy_words': ['buy'], 'qualified_grades': ['A', 'B'],
                   'grading_criteria': [{'name': 'fit', 'weight': 100, 'description': 'QA fit'}]}
        self.icp_id = db.execute('INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)',
                                ('QA template', db.dumps(profile), self.today, self.today))

    def configure_mock(self):
        for agent in ('manager', 'scout', 'researcher', 'analyst', 'monitor'):
            llm.update_config(agent, {'provider': 'mock', 'model': 'qa-mock'})

    def goal_config(self, **changes):
        return dict(enabled=True, icp_id=self.icp_id, target=3, sources=['google'],
                    time_of_day='09:00', max_retries=1, **changes)

    def seed_discoveries(self):
        for name, score in [('boundary', 70), ('highscore', 99), ('lowscore', 69)]:
            db.execute('INSERT INTO tg_discoveries(icp_id,link,title,score,created_at) VALUES(?,?,?,?,?)',
                       (self.icp_id, 'https://t.me/' + name, name, score, self.today))

    def finish_task(self, task_id, status='done', qualified=0, result='sources_exhausted'):
        db.execute('UPDATE tasks SET status=?,stats=?,result=?,finished_at=? WHERE id=?',
                   (status, db.dumps({'qualified': qualified}), result, self.today + 60, task_id))
        if task_id in tasks._RUNS:
            tasks._RUNS[task_id].done = True

    def make_lead(self, timestamp, name='qualified', qualified=1, task_id=None):
        return db.execute('INSERT INTO leads(company_name,company_key,grade,qualified,status,icp_id,created_at,updated_at,task_id) '
                          'VALUES(?,?,?,?,?,?,?,?,?)',
                          (name, name, 'A', qualified, 'qualified', self.icp_id, timestamp, timestamp, task_id))

    def worker(self, fn, name='batch2-worker'):
        outcome = {}
        def invoke():
            try:
                outcome['result'] = fn()
            except BaseException as exc:
                outcome['error'] = exc
        thread = threading.Thread(target=invoke, name=name, daemon=True)
        thread.start()
        return thread, outcome

    def finish_worker(self, worker):
        thread, outcome = worker
        thread.join(5)
        self.assertFalse(thread.is_alive(), thread.name)
        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('result')


class ManagerTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        from salesteam.agents import manager
        self.manager = manager
        self.configure_mock()

    def test_defaults_are_off_and_mutations_validate_atomically(self):
        self.assertFalse(self.manager.goal()['enabled'])
        self.assertEqual(self.manager.tick(self.today), [])
        saved = self.manager.set_goal(self.goal_config())
        for changes in ({'target': 0}, {'target': 501}, {'target': True},
                        {'sources': ['unsupported']}, {'time_of_day': '25:90'}, {'max_retries': 3}):
            with self.subTest(changes=changes):
                invalid = dict(saved, **changes)
                with self.assertRaises(UserError):
                    self.manager.set_goal(invalid)
                self.assertEqual(self.manager.goal(), saved)

    def test_same_day_and_restart_do_not_duplicate_active_plan(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            started = self.manager.tick(self.today)
            self.assertEqual(len(started), 1)
            self.assertEqual(self.manager.tick(self.today + 60), [])
            # Model restart without changing persistent data; do not let startup
            # itself schedule anything, and inspect the durable task registration.
            tasks._RUNS.clear()
            db.close()
            self.assertEqual(self.manager.tick(self.today + 120), [])
        rows = db.query("SELECT * FROM tasks WHERE created_by='manager'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['goal_date'], self.day)
        self.assertEqual(rows[0]['target'], 3)

    def test_paused_budget_and_missing_model_do_not_dispatch(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            settings.put('pause_all', {'on': True})
            self.assertEqual(self.manager.tick(self.today), [])
            settings.put('pause_all', {'on': False})
            with mock.patch.object(llm, 'budget_state', return_value={'paused': True}):
                self.assertEqual(self.manager.tick(self.today), [])
            llm.update_config('manager', {'model': ''})
            self.assertEqual(self.manager.tick(self.today), [])
            llm.update_config('manager', {'model': 'qa-mock'})
            llm.update_config('researcher', {'model': ''})
            self.assertEqual(self.manager.tick(self.today), [])
        self.assertEqual(db.query("SELECT id FROM tasks WHERE created_by='manager'"), [])
        self.assertEqual(self.manager.goal()['target'], 3)

    def test_goal_met_today_prevents_dispatch_without_carryover(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            started = self.manager.tick(self.today)
            self.assertEqual(len(started), 1)
            for n in range(5):
                self.make_lead(self.today + 1, f'today-{n}', task_id=started[0])
            self.finish_task(started[0], qualified=5, result='target_reached')
            self.assertEqual(self.manager.tick(self.today + 60), [])
            next_day = self.today + 86400
            following = self.manager.tick(next_day)
        self.assertEqual(len(following), 1)
        task = db.query_one('SELECT * FROM tasks WHERE id=?', (following[0],))
        self.assertEqual(task['goal_date'], '2026-10-16')
        self.assertEqual(task['target'], 3)
        self.assertEqual(self.manager.report(self.day)['qualified'], 5)

    def test_real_restart_keeps_distinct_attempts_and_remediation_limit(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            first = self.manager.tick(self.today)
            original = db.query_one('SELECT parent_id FROM tasks WHERE id=?', (first[0],))['parent_id']
            tasks._RUNS.clear()
            db.close()
            server.startup()
            following = self.manager.tick(self.today + 60)
            self.assertEqual(len(following), 1)
            self.assertEqual(db.query_one('SELECT status FROM tasks WHERE id=?', (first[0],))['status'], 'interrupted')
            self.assertEqual(db.query_one('SELECT COUNT(*) n FROM tasks WHERE parent_id=?', (original,))['n'], 1)
            self.finish_task(following[0])
            self.assertEqual(self.manager.tick(self.today + 120), [])
        self.assertEqual(len(self.manager.plans(self.day)), 1)

    def test_concurrent_ticks_register_only_one_attempt(self):
        self.manager.set_goal(self.goal_config())
        ready = threading.Barrier(3)
        def tick():
            ready.wait(5)
            return self.manager.tick(self.today)
        with mock.patch.object(tasks, '_execute'):
            first = self.worker(tick, 'manager-concurrent-1')
            second = self.worker(tick, 'manager-concurrent-2')
            ready.wait(5)
            results = self.finish_worker(first) + self.finish_worker(second)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(db.query("SELECT * FROM tasks WHERE created_by='manager'")), 1)
        self.assertEqual(len(self.manager.plans(self.day)), 1)

    def test_retries_bounded_and_next_day_independent(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            first = self.manager.tick(self.today)
            self.assertEqual(len(first), 1)
            self.finish_task(first[0])
            second = self.manager.tick(self.today + 3600)
            self.assertEqual(len(second), 1)
            self.finish_task(second[0])
            self.assertEqual(self.manager.tick(self.today + 7200), [])
            self.assertEqual(self.manager.tick(self.today + 10800), [])
            following = self.manager.tick(self.today + 86400)
            self.assertEqual(len(following), 1)
        self.assertEqual(len(db.query("SELECT id FROM tasks WHERE goal_date=? AND created_by='manager'", (self.day,))), 2)
        report = self.manager.report(self.day)
        self.assertTrue(report)
        self.assertEqual(self.manager.goal()['target'], 3)
        self.assertEqual(self.manager.goal()['max_retries'], 1)
        self.assertEqual(settings.get('budget'), settings.DEFAULTS['budget'])
        self.assertEqual(settings.get('joining')['assisted_join'], False)


    def test_registered_task_survives_crash_before_plan_writeback(self):
        self.manager.set_goal(self.goal_config())
        execute = db.execute
        failed = []
        def crash(sql, args=()):
            if sql.startswith("UPDATE manager_plans SET status='running'") and not failed:
                failed.append(True)
                raise RuntimeError('QA crash after task registration')
            return execute(sql, args)
        with mock.patch.object(tasks, '_execute'):
            with mock.patch.object(db, 'execute', side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, 'QA crash'):
                    self.manager.tick(self.today)
            first = db.query_one("SELECT * FROM tasks WHERE created_by='manager'")
            self.assertIsNotNone(first)
            self.assertEqual(self.manager.tick(self.today + 60), [])
            tasks._RUNS.clear()
            db.close()
            server.startup()
            following = self.manager.tick(self.today + 120)
            self.assertEqual(len(following), 1)
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM tasks WHERE parent_id=?', (first['parent_id'],))['n'], 1)
        self.assertEqual(len(self.manager.plans(self.day)), 1)

    def test_watchdog_children_do_not_inflate_remediation_attempts(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            first = self.manager.tick(self.today)[0]
            self.finish_task(first, status='restarted')
            child = tasks.create_task({'icp_id': self.icp_id, 'target': 3, 'sources': ['google']},
                                      created_by='watchdog', parent_id=first, goal_date=self.day)['id']
            self.assertEqual(self.manager.tick(self.today + 60), [])
            self.assertEqual(self.manager.plans(self.day)[0]['attempts'], 1)
            self.make_lead(self.today + 10, 'watchdog-qualified', task_id=child)
            self.make_lead(self.today + 86400, 'next-day-old-worker', task_id=child)
            self.finish_task(child, qualified=2)
            second = self.manager.tick(self.today + 120)
            self.assertEqual(len(second), 1)
            self.assertEqual(db.query_one('SELECT target FROM tasks WHERE id=?', (second[0],))['target'], 2)
            self.assertEqual(self.manager.report(self.day)['qualified'], 1)
            self.assertEqual(self.manager.plans(self.day)[0]['attempts'], 2)
            self.finish_task(second[0])
            self.assertEqual(self.manager.tick(self.today + 180), [])

    def test_real_total_budget_limit_blocks_dispatch(self):
        self.manager.set_goal(self.goal_config())
        settings.put('budget', {'daily': .01, 'monthly': 0})
        db.execute('INSERT INTO usage(ts,day,month,agent,provider,model,purpose,cost) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time(), time.strftime('%Y-%m-%d'), time.strftime('%Y-%m'),
                    'researcher', 'mock', 'qa-mock', 'qa-budget', .02))
        self.assertTrue(llm.budget_state()['paused'])
        with mock.patch.object(tasks, '_execute'):
            self.assertEqual(self.manager.tick(self.today), [])
        self.assertEqual(db.query("SELECT id FROM tasks WHERE created_by='manager'"), [])
        self.assertEqual(self.manager.goal()['target'], 3)

    def test_current_plan_snapshot_is_immutable_and_remediation_changes_search(self):
        self.manager.set_goal(self.goal_config())
        with mock.patch.object(tasks, '_execute'):
            first = self.manager.tick(self.today)[0]
            initial = db.loads(db.query_one('SELECT params FROM tasks WHERE id=?', (first,))['params'], {})
            self.finish_task(first)
            self.manager.set_goal({'target': 20, 'max_retries': 2})
            second = self.manager.tick(self.today + 60)[0]
            retry = db.loads(db.query_one('SELECT params FROM tasks WHERE id=?', (second,))['params'], {})
        self.assertEqual(self.manager.plans(self.day)[0]['goal']['target'], 3)
        self.assertEqual(self.manager.plans(self.day)[0]['goal']['max_retries'], 1)
        self.assertEqual(self.manager.goal()['target'], 20)
        self.assertEqual(initial.get('manager_query_offset', 0), 0)
        self.assertEqual(retry.get('manager_query_offset'), 1)
        self.assertEqual(initial['sources'], retry['sources'])
        self.assertEqual(initial['icp_id'], retry['icp_id'])


    def test_mock_model_command_proposal_confirmation_and_rejection(self):
        self.manager.set_goal(self.goal_config())
        saved = self.manager.goal()
        response = self.client.post('/api/manager/command', json={'message': '每天找 8 条，从 Google，10:30 开始'})
        self.assertEqual(response.status_code, 200)
        pending = response.json()
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['proposal']['goal']['target'], 8)
        self.assertEqual(self.manager.goal(), saved)
        confirm = self.client.post(f"/api/manager/command/{pending['id']}/confirm", json={})
        self.assertEqual(confirm.status_code, 200)
        self.assertEqual(self.manager.goal()['target'], 8)
        self.assertEqual(self.manager.goal()['time_of_day'], '10:30')
        self.assertEqual(self.manager.goal()['sources'], ['google'])
        canceled = self.client.post('/api/manager/command', json={'message': '每天找 11 条'})
        self.assertEqual(canceled.status_code, 200)
        cid = canceled.json()['id']
        reject = self.client.post(f'/api/manager/command/{cid}/reject', json={})
        self.assertEqual(reject.status_code, 200)
        self.assertEqual(self.manager.goal()['target'], 8)
        self.assertEqual(self.manager.state()['commands'], [])
        self.assertNotEqual(self.client.post(f'/api/manager/command/{cid}/confirm', json={}).status_code, 200)

    def test_unsupported_commands_and_invalid_model_proposal_do_not_change_locked_fields(self):
        self.manager.set_goal(self.goal_config())
        original = self.manager.goal()
        budget = settings.get('budget')
        for message in ('开启自动销售', '开启自动跟进', 'Change budget to 100', 'Relax filters'):
            with self.subTest(message=message):
                response = self.client.post('/api/manager/command', json={'message': message})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.manager.goal(), original)
                self.assertEqual(settings.get('budget'), budget)
        with mock.patch.object(llm, 'call_json', return_value=({'goal': {'budget': 1000}, 'explanation': 'bad'}, {})):
            with self.assertRaises(UserError):
                self.manager.command('daily find 20 leads')
        self.assertEqual(self.manager.goal(), original)
        self.assertEqual(db.query('SELECT * FROM manager_commands'), [])


class BackgroundTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        from salesteam import background
        self.bg = background

    def reset(self):
        return server.delete_all({'confirm': 'DELETE', 'scope': 'business'})

    def test_async_operations_block_reset_until_their_actual_thread_exits(self):
        for name in ('push', 'tg-upkeep', 'watchdog-old-task'):
            with self.subTest(name=name):
                entered, release = threading.Event(), threading.Event()
                def job():
                    with self.bg.operation(name=name):
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('blocked operation timed out')
                        settings.put('worker-finished', name)
                worker = self.worker(job, name)
                try:
                    self.assertTrue(entered.wait(5))
                    before = self.bg.capture()
                    with self.assertRaises(UserError) as rejected:
                        self.reset()
                    self.assertEqual(rejected.exception.key, 'task_already_running')
                    self.assertEqual(self.bg.capture(), before)
                finally:
                    release.set()
                    self.finish_worker(worker)
                self.assertEqual(settings.get('worker-finished'), name)
                self.assertTrue(self.reset()['ok'])

    def test_spawn_captured_before_committed_reset_never_runs_afterward(self):
        ran = threading.Event()
        original_start = threading.Thread.start
        queued = []
        with mock.patch.object(threading.Thread, 'start', side_effect=lambda self: queued.append(self), autospec=True):
            self.bg.spawn(ran.set, name='late-old-generation')
        self.assertEqual(len(queued), 1)
        before = self.bg.capture()
        self.assertTrue(self.reset()['ok'])
        self.assertGreater(self.bg.capture(), before)
        original_start(queued[0])
        queued[0].join(5)
        self.assertFalse(queued[0].is_alive())
        self.assertFalse(ran.is_set())

    def test_committed_reset_rejects_captured_context_and_old_write(self):
        token = self.bg.capture()
        self.assertTrue(self.reset()['ok'])
        with self.assertRaises(self.bg.StaleOperation):
            with self.bg.operation(name='captured-late-write', token=token):
                self.make_lead(self.today, 'should-not-exist')
        self.assertIsNone(db.query_one("SELECT * FROM leads WHERE company_key='should-not-exist'"))

    def test_failed_reset_preserves_generation_and_valid_pending_worker(self):
        token = self.bg.capture()
        self.make_lead(self.today, 'original')
        before = db.query('SELECT * FROM leads')
        execute = db.execute
        def fail(sql, args=()):
            if sql == 'DELETE FROM memos':
                raise RuntimeError('QA reset failure')
            return execute(sql, args)
        with mock.patch.object(db, 'execute', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'QA reset failure'):
                self.reset()
        self.assertEqual(self.bg.capture(), token)
        self.assertEqual(db.query('SELECT * FROM leads'), before)
        with self.bg.operation(name='still-valid-after-rollback', token=token):
            settings.put('valid-worker-write', True)
        self.assertTrue(settings.get('valid-worker-write'))

    def test_timed_out_call_still_blocks_reset_while_child_can_write(self):
        from salesteam import heal
        entered, release, wrote = threading.Event(), threading.Event(), threading.Event()
        def slow():
            entered.set()
            if not release.wait(5):
                raise AssertionError('timeout fixture did not release')
            self.make_lead(self.today, 'after-timeout')
            wrote.set()
        try:
            with self.assertRaises(heal.CallTimeout):
                heal.call_with_timeout(slow, timeout=.05)
            self.assertTrue(entered.wait(5))
            with self.assertRaises(UserError) as rejected:
                self.reset()
            self.assertEqual(rejected.exception.key, 'task_already_running')
        finally:
            release.set()
        self.assertTrue(wrote.wait(5))
        deadline = time.monotonic() + 5
        while self.bg.state()['active'] and time.monotonic() < deadline:
            time.sleep(.001)
        self.assertFalse(self.bg.state()['active'])
        self.assertTrue(self.reset()['ok'])
        self.assertEqual(db.query('SELECT * FROM leads'), [])

    def test_real_async_push_blocks_reset(self):
        from salesteam import push
        entered, release = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return True
        settings.put('notify', {'lead_recipients': ['@qa_team'], 'push_on_task_done': True})
        with mock.patch.object(push.pool, 'notify_account', return_value={'id': 1}), \
                mock.patch.object(push.pool, 'notify_ready', return_value=True), \
                mock.patch.object(push.pool, 'notify_send', side_effect=send):
            try:
                worker = self.worker(lambda: push.auto_push('task_done'), 'real-push')
                self.assertTrue(entered.wait(5))
                with self.assertRaises(UserError):
                    self.reset()
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while self.bg.state()['active'] and time.monotonic() < deadline:
                time.sleep(.001)
        self.finish_worker(worker)
        self.assertFalse(self.bg.state()['active'])

    def test_real_upkeep_worker_blocks_reset(self):
        from salesteam.tg import upkeep
        entered, release = threading.Event(), threading.Event()
        def inspect(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'quick': {}, 'full': {}}
        with mock.patch.object(upkeep, 'due_quick', return_value=[1]), \
                mock.patch.object(upkeep, 'run_once', side_effect=inspect):
            try:
                upkeep.tick()
                self.assertTrue(entered.wait(5))
                with self.assertRaises(UserError):
                    self.reset()
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while self.bg.state()['active'] and time.monotonic() < deadline:
                time.sleep(.001)
        self.assertFalse(self.bg.state()['active'])


    def test_watchdog_marks_done_but_live_old_task_still_blocks_reset(self):
        from salesteam import heal
        self.configure_mock()
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def execute(run):
            calls.append(run.id)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(5))
                self.make_lead(self.today, 'old-worker-final-write', task_id=run.id)
                exited.set()
        with mock.patch.object(tasks, '_execute', side_effect=execute), \
                mock.patch.object(heal, '_stuck_alert'):
            first = tasks.create_task({'icp_id': self.icp_id, 'target': 3, 'sources': ['google']})['id']
            try:
                self.assertTrue(entered.wait(5))
                restarted = heal._restart_task(tasks._RUNS[first], 1000)
                self.assertTrue(tasks._RUNS[first].done)
                self.assertIn('restarted_as', restarted)
                self.finish_task(restarted['restarted_as'])
                self.assertTrue(all(r.done for r in tasks._RUNS.values()))
                with self.assertRaises(UserError) as rejected:
                    self.reset()
                self.assertEqual(rejected.exception.key, 'task_already_running')
            finally:
                release.set()
                self.assertTrue(exited.wait(5))
                deadline = time.monotonic() + 5
                while self.bg.state()['active'] and time.monotonic() < deadline:
                    time.sleep(.001)
        self.assertFalse(self.bg.state()['active'])
        self.assertTrue(self.reset()['ok'])
        self.assertEqual(db.query('SELECT * FROM leads'), [])


    def test_successful_reset_clears_derived_configuration_and_queued_sentinels(self):
        from salesteam import scheduler
        from salesteam.agents import scout
        from salesteam.tg import upkeep
        for key in ('manager_goal', 'scout_settings', 'channel_overrides'):
            settings.put(key, {'old-business-data': True})
        with mock.patch.object(scheduler, '_last', {'health': self.today}), \
                mock.patch.object(scheduler, '_tg_info_busy', [True]), \
                mock.patch.object(upkeep, '_next_quick', {1: self.today}), \
                mock.patch.object(upkeep, '_busy', [True]), \
                mock.patch.object(scout, '_active_days', {self.day}):
            self.assertTrue(self.reset()['ok'])
            for key in ('manager_goal', 'scout_settings', 'channel_overrides'):
                self.assertIsNone(db.query_one('SELECT * FROM settings WHERE key=?', (key,)))
            self.assertEqual(scheduler._last, {})
            self.assertFalse(scheduler._tg_info_busy[0])
            self.assertEqual(upkeep._next_quick, {})
            self.assertFalse(upkeep._busy[0])
            self.assertEqual(scout._active_days, set())


class ScoutTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        from salesteam.agents import scout
        from salesteam.tg import joining
        self.scout, self.joining = scout, joining
        self.enterContext(mock.patch.object(scout, '_active_days', set()))
        self.configure_mock()

    def test_auto_queue_default_off_and_enable_requires_explicit_confirmation(self):
        self.seed_discoveries()
        before = self.scout.get_settings()
        self.assertFalse(before['enabled'])
        self.assertFalse(before['auto_queue'])
        self.assertEqual(self.joining.auto_queue_discoveries(), 0)
        self.assertEqual(db.query('SELECT * FROM tg_join_queue'), [])
        for confirmation in (None, False, 1, 'true'):
            with self.subTest(confirmation=confirmation):
                payload = {'auto_queue': True}
                if confirmation is not None:
                    payload['confirmed'] = confirmation
                with self.assertRaises(UserError) as err:
                    self.scout.set_settings(payload)
                self.assertEqual(err.exception.key, 'risk_confirm_required')
                self.assertEqual(self.scout.get_settings(), before)
                self.assertFalse(settings.get('joining')['assisted_join'])
                self.assertEqual(db.query('SELECT * FROM risk_log'), [])
        self.scout.set_settings({'auto_queue': True, 'confirmed': True, 'icp_id': self.icp_id})
        self.assertTrue(self.scout.get_settings()['auto_queue'])
        self.assertTrue(settings.get('joining')['assisted_join'])
        self.assertEqual({r['feature'] for r in db.query('SELECT * FROM risk_log')},
                         {'joining.assisted_join', 'scout.auto_queue'})

    def test_queue_threshold_and_idempotence_keep_low_scores_manual(self):
        self.seed_discoveries()
        self.scout.set_settings({'auto_queue': True, 'confirmed': True, 'icp_id': self.icp_id})
        self.assertEqual(self.joining.auto_queue_discoveries(), 2)
        self.assertEqual(self.joining.auto_queue_discoveries(), 0)
        rows = db.query('SELECT * FROM tg_join_queue ORDER BY link')
        self.assertEqual([r['link'] for r in rows], ['https://t.me/boundary', 'https://t.me/highscore'])
        self.assertTrue(all(r['source'] == 'discovery' and r['status'] == 'pending' for r in rows))
        low = db.query_one('SELECT * FROM tg_discoveries WHERE score=69')
        self.assertEqual(low['status'], 'suggested')
        self.scout.set_settings({'auto_queue': False})
        db.execute("INSERT INTO tg_discoveries(icp_id,link,title,score,created_at) VALUES(?,?,?,?,?)",
                   (self.icp_id, 'https://t.me/later_high', 'Later high', 95, self.today))
        self.assertEqual(self.joining.auto_queue_discoveries(), 0)

    def test_discovery_daily_claim_survives_restart_and_failure_is_visible(self):
        from salesteam import background
        self.scout.set_settings({'enabled': True, 'icp_id': self.icp_id})
        with mock.patch.object(self.scout.pool, 'scout_accounts', return_value=[{'id': 1}]), \
                mock.patch.object(background, 'spawn') as spawn:
            self.assertEqual(self.scout.tick(self.today), self.day)
            self.assertIsNone(self.scout.tick(self.today + 60))
            db.close()
            self.assertIsNone(self.scout.tick(self.today + 120))
            self.assertEqual(spawn.call_count, 1)
            with mock.patch.object(self.joining, 'discover', side_effect=UserError('agent_not_configured')):
                result = self.scout.run_once(self.day)
            self.assertEqual(result['error'], 'agent_not_configured')
        row = db.query_one('SELECT * FROM scout_runs WHERE day=?', (self.day,))
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error'], 'agent_not_configured')

    def test_manual_retirement_survives_repeated_sync_and_rediscovery(self):
        self.seed_discoveries()
        channels.sync_tg()
        candidate = db.query_one("SELECT * FROM channels WHERE key='tg:@highscore'")
        # A real joined group is initially monitored. Then user explicitly turns
        # it off, and later daily synchronization/AI recommendation revisit it.
        aid = db.execute("INSERT INTO tg_accounts(phone,status,added_at) VALUES(?,?,?)",
                         ('+10000000001', 'active', self.today))
        cid = db.execute('INSERT INTO tg_chats(account_id,chat_id,title,username,monitored,added_at) '
                         'VALUES(?,?,?,?,?,?)', (aid, '1001', 'High score', 'highscore', 1, self.today))
        channels.sync_tg()
        channels.set_status(candidate['id'], 'retired')
        channels.sync_tg()
        rows = channels.list_all()
        updated = next(r for r in rows if r['id'] == candidate['id'])
        self.assertEqual(updated['status'], 'retired')
        self.assertEqual(db.query_one('SELECT monitored FROM tg_chats WHERE id=?', (cid,))['monitored'], 0)
        channels.register('tg_group', 'Rediscovered', 'https://t.me/highscore', key='tg:@highscore',
                          status='monitored', source='ai_discovery')
        self.assertEqual(db.query_one('SELECT status FROM channels WHERE id=?', (candidate['id'],))['status'], 'retired')


    def test_auto_and_manual_queue_race_creates_only_one_row(self):
        self.seed_discoveries()
        self.scout.set_settings({'auto_queue': True, 'confirmed': True, 'icp_id': self.icp_id})
        high = db.query_one('SELECT id FROM tg_discoveries WHERE score=99')['id']
        barrier = threading.Barrier(3)
        def auto():
            barrier.wait(5)
            return self.joining.auto_queue_discoveries()
        def manual():
            barrier.wait(5)
            return self.joining.approve_discoveries([high])
        first, second = self.worker(auto), self.worker(manual)
        barrier.wait(5)
        self.finish_worker(first)
        self.finish_worker(second)
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM tg_join_queue WHERE discovery_id=?', (high,))['n'], 1)

    def test_disable_while_discovering_prevents_queue_and_records_block(self):
        self.seed_discoveries()
        cfg = self.scout.set_settings({'enabled': True, 'auto_queue': True, 'confirmed': True, 'icp_id': self.icp_id})
        db.execute("INSERT INTO scout_runs(day,status,created_at,updated_at) VALUES(?,'running',?,?)",
                   (self.day, self.today, self.today))
        def discover(*args, **kwargs):
            self.scout.set_settings({'enabled': False})
            return {'added': 3, 'evaluated': 3}
        with mock.patch.object(self.scout.pool, 'scout_accounts', return_value=[{'id': 1}]), \
                mock.patch.object(self.joining, 'discover', side_effect=discover):
            result = self.scout.run_once(self.day, cfg)
        self.assertEqual(result['queued'], 0)
        self.assertEqual(db.query('SELECT * FROM tg_join_queue'), [])
        self.assertEqual(db.query_one('SELECT status FROM scout_runs WHERE day=?', (self.day,))['status'], 'blocked')

    def test_auto_queue_obeys_per_account_daily_cap_and_warmup(self):
        from salesteam.tg import pool
        settings.put('tg', {'backend': 'mock'})
        self.enterContext(mock.patch.object(pool, '_backend', None))
        aid = db.execute('INSERT INTO tg_accounts(phone,status,session_enc,daily_join_limit,warmup_days,added_at) '
                         'VALUES(?,?,?,?,?,?)',
                         ('+10000000001', 'active', vault.encrypt('mock-session:10000000001'), 3, 3, time.time()))
        self.scout.set_settings({'auto_queue': True, 'confirmed': True, 'icp_id': self.icp_id})
        for i, link in enumerate(('https://t.me/johor_solar_installers', 'https://t.me/battery_buyers_sgmy')):
            db.execute('INSERT INTO tg_discoveries(icp_id,link,title,score,created_at) VALUES(?,?,?,?,?)',
                       (self.icp_id, link, 'QA group', 90, self.today))
        self.assertEqual(self.joining.auto_queue_discoveries(), 2)
        first = self.joining.process_one()
        self.assertIn('joined', first)
        self.assertEqual(pool.count(aid, 'join'), 1)
        settings.put('join_state', {'next_at': 0})
        self.assertEqual(self.joining.process_one(), {'skipped': 'no_account'})
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM tg_join_queue WHERE status='pending'")['n'], 1)
        pool.skip_warmup(aid)
        db.execute('UPDATE tg_accounts SET daily_join_limit=1 WHERE id=?', (aid,))
        self.assertEqual(self.joining.process_one(), {'skipped': 'no_account'})
        self.assertEqual(pool.count(aid, 'join'), 1)


    def test_retired_group_stays_off_across_accounts_and_new_sync(self):
        from salesteam.tg import pool
        account_ids = [db.execute('INSERT INTO tg_accounts(phone,status,added_at) VALUES(?,?,?)',
                                  (f'+1000000000{n}', 'active', self.today)) for n in range(1, 4)]
        chat = {'chat_id': '1001', 'title': 'Same group', 'username': 'same_group', 'kind': 'group'}
        for aid in account_ids[:2]:
            pool.upsert_chat(aid, chat)
        channels.sync_tg()
        channel = db.query_one("SELECT * FROM channels WHERE key='tg:@same_group'")
        channels.set_status(channel['id'], 'retired')
        self.assertFalse(any(r['monitored'] for r in db.query("SELECT * FROM tg_chats WHERE username='same_group'")))
        pool.upsert_chat(account_ids[2], chat)
        channels.sync_tg()
        self.assertEqual(db.query_one('SELECT status FROM channels WHERE id=?', (channel['id'],))['status'], 'retired')
        rows = db.query("SELECT * FROM tg_chats WHERE username='same_group'")
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(r['monitored'] for r in rows))

    def test_retirement_only_after_observed_bad_output_not_just_zero_results(self):
        self.scout.set_settings({'enabled': True, 'auto_retire': True, 'icp_id': self.icp_id})
        retired = channels.register('tg_group', 'Bad observed', key='tg:@bad_observed', status='monitored')
        no_samples = channels.register('tg_group', 'No samples', key='tg:@no_samples', status='monitored')
        good = channels.register('tg_group', 'Has qualified', key='tg:@has_qualified', status='monitored')
        for cid in (retired, no_samples, good):
            db.execute('UPDATE channels SET created_at=? WHERE id=?', (time.time() - 8 * 86400, cid))
        for cid in (retired, good):
            for n in range(10):
                lid = self.make_lead(self.today, f'channel-{cid}-{n}', qualified=int(cid == good and n == 0))
                db.execute('UPDATE leads SET channel_id=?,feedback=-1 WHERE id=?', (cid, lid))
        suggested = self.scout.suggestions()
        self.assertEqual([r['id'] for r in suggested], [retired])
        db.execute("INSERT INTO scout_runs(day,status,created_at,updated_at) VALUES(?,'running',?,?)",
                   (self.day, self.today, self.today))
        with mock.patch.object(self.scout.pool, 'scout_accounts', return_value=[{'id': 1}]), \
                mock.patch.object(self.joining, 'discover', return_value={'added': 0, 'evaluated': 0}):
            result = self.scout.run_once(self.day)
        self.assertEqual(result['retired'], [retired])
        self.assertEqual(db.query_one('SELECT status FROM channels WHERE id=?', (retired,))['status'], 'retired')
        self.assertEqual(db.query_one('SELECT status FROM channels WHERE id=?', (no_samples,))['status'], 'monitored')
        self.assertEqual(db.query_one('SELECT status FROM channels WHERE id=?', (good,))['status'], 'monitored')


if __name__ == '__main__':
    unittest.main()

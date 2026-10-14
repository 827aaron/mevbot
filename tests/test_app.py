"""Source-level API regression checks using isolated, temporary data."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from salesteam import db, desktop_api, paths, schedules, server, settings, tasks, vault
from salesteam.errors import UserError
from salesteam.agents import onboarding


class AppTests(unittest.TestCase):
    def setUp(self):
        db.close()
        vault.reset()
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="james-agent-test-"))
        data = Path(tmp)
        for name, value in {
            "DATA_DIR": data,
            "DB_PATH": data / "salesteam.db",
            "KEY_PATH": data / ".secret.key",
            "UPLOAD_DIR": data / "uploads",
            "EXPORT_DIR": data / "exports",
        }.items():
            self.enterContext(mock.patch.object(paths, name, value))
        self.enterContext(mock.patch.dict("os.environ", {
            "SALESTEAM_NO_SCHEDULER": "1", "SALESTEAM_QA_FIXTURES": "",
        }))
        self.enterContext(mock.patch.object(tasks, "_RUNS", {}))
        self.addCleanup(db.close)
        self.addCleanup(vault.reset)
        server.startup()
        self.client = self.enterContext(TestClient(server.app, base_url="http://127.0.0.1"))

    def make_run(self, ident, kind="research", done=False):
        db.execute("INSERT INTO tasks(id, kind, params, status, created_at) VALUES(?,?,?,?,?)",
                   (ident, kind, "{}", "done" if done else "running", db.now()))
        run = tasks.TaskRun(db.query_one("SELECT * FROM tasks WHERE id=?", (ident,)))
        run.done = done
        tasks._RUNS[ident] = run
        return run

    def seed_business_records(self):
        now = db.now()
        # Independent business inventory: seed every business table, so an empty
        # table cannot accidentally make reset coverage look complete.
        records = {
            "icp_templates": dict(id=1, name="QA", data="{}", created_at=now, updated_at=now),
            "tasks": dict(id="seed-task", kind="research", params="{}", status="done", created_at=now),
            "task_schedules": dict(mode="daily", time_of_day="00:00", params="{}", created_at=now, updated_at=now),
            "task_events": dict(task_id="seed-task", seq=1, ts=now, agent="system", level="info", key="qa", params="{}"),
            "raw_leads": dict(task_id="seed-task", source="tg", data="{}", created_at=now),
            "leads": dict(id=1, company_name="QA", company_key="qa", grade="A", status="qualified", created_at=now, updated_at=now),
            "lead_identities": dict(kind="tg", value="qa_contact", lead_id=1),
            "icp_samples": dict(icp_id=1, polarity=1, origin="manual_mark", created_at=now),
            "icp_knowledge": dict(icp_id=1, version=1, kind="keywords", content="{}", status="confirmed", created_at=now),
            "tg_join_queue": dict(link="https://t.me/qa_contact", created_at=now),
            "tg_discoveries": dict(link="https://t.me/qa_discovery", created_at=now),
            "outbox": dict(ts=now, recipient="@qa_sales", kind="lead_push", status="mock"),
            "alerts": dict(key="qa", level="general", created_at=now, updated_at=now),
            "memos": dict(agent="all", text="QA", created_at=now),
            "agent_chats": dict(agent="manager", role="user", content="QA", ts=now),
            "decision_tests": dict(ts=now, provider="mock", model="qa", n=1, correct=1, accuracy=1),
            "embeddings": dict(kind="qa", ref_id=1, model="qa", vec=b"qa"),
            "onboarding": dict(id=1, status="done", created_at=now, updated_at=now),
            "site_pages": dict(onboarding_id=1, url="https://qa.example"),
            "profile_versions": dict(version=1, company="{}", icp="{}", created_at=now),
            "agent_actions": dict(ts=now, agent="manager", action="qa"),
            "channels": dict(key="tg:qa", kind="tg", created_at=now, updated_at=now),
            "contact_blacklist": dict(kind="tg", value="qa_contact", reason="do_not_contact", created_at=now),
            "contact_history": dict(ident_kind="tg", ident_value="qa_contact", ts=now),
            "quality_checks": dict(ts=now, task_id="seed-task"),
            "pool_events": dict(kind="discarded", ts=now),
            "manager_plans": dict(day="2026-10-15", goal="{}", created_at=now, updated_at=now),
            "manager_commands": dict(message="QA command", proposal="{}", created_at=now),
            "manager_reports": dict(day="2026-10-15", data="{}", updated_at=now),
            "scout_runs": dict(day="2026-10-15", status="done", created_at=now, updated_at=now),
        }
        for table, record in records.items():
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            db.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders})", tuple(record.values()))
        self.business_tables = tuple(records)

    def assert_business_empty(self):
        for table in self.business_tables:
            with self.subTest(table=table):
                self.assertEqual(db.query_one(f"SELECT COUNT(*) n FROM {table}")["n"], 0)

    def business_snapshot(self):
        return {table: db.query(f"SELECT * FROM {table}") for table in self.business_tables}

    def start_worker(self, name, fn):
        outcome = {}

        def work():
            try:
                outcome["result"] = fn()
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=work, name=name, daemon=True)
        thread.start()
        return thread, outcome

    def finish_worker(self, worker):
        thread, outcome = worker
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), f"worker did not finish: {thread.name}")
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("result")

    @staticmethod
    def observed_lock(lock, worker_name, attempted):
        # Signal the exact lock attempt rather than relying on thread timing.
        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == worker_name:
                    attempted.set()
                lock.acquire()
                return self

            def __exit__(self, *_):
                lock.release()
        return ObservedLock()

    def assert_schedule_reset(self):
        self.assertEqual(self.client.get("/api/schedules").json(), [])
        self.assertEqual(schedules.due(), [])
        self.assertEqual(schedules.tick(), [])
        self.assertEqual(db.query("SELECT * FROM alerts WHERE category='schedule_failed'"), [])
        self.assertEqual(db.query("SELECT * FROM tasks"), [])

    def test_source_startup_and_ui(self):
        response = self.client.get("/api/meta")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["agents"]), 5)
        self.assertEqual(self.client.get("/").status_code, 200)
        for asset in ("js/boot.js", "i18n/zh.json", "i18n/en.json"):
            self.assertEqual(self.client.get("/static/" + asset).status_code, 200)

    def test_pause_cancels_every_active_ai_task(self):
        active = [self.make_run(f"research-{n}") for n in range(1, 6)]
        active += [self.make_run("monitor-1", "monitor"), self.make_run("pool-1", "pool")]
        finished = self.make_run("finished", done=True)
        onboarding = self.make_run("onboarding", "onboarding")
        response = self.client.post("/api/ai/pause", json={"on": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(settings.ai_paused())
        for run in active:
            with self.subTest(task=run.id):
                self.assertTrue(run.cancel_flag.is_set())
                self.assertTrue(run.should_stop())
                self.assertEqual(run.events[-1]["key"], "ev.ai_paused_all")
        self.assertFalse(finished.cancel_flag.is_set())
        self.assertFalse(onboarding.cancel_flag.is_set())
        original_runs = dict(tasks._RUNS)
        with mock.patch.object(tasks, "create_task") as create:
            response = self.client.post("/api/ai/pause", json={"on": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(settings.ai_paused())
        create.assert_not_called()
        self.assertEqual(tasks._RUNS, original_runs)
        self.assertTrue(all(run.cancel_flag.is_set() for run in active))

    def test_resume_does_not_cancel_running_tasks(self):
        run = self.make_run("research")
        settings.put("pause_all", {"on": True})
        response = self.client.post("/api/ai/pause", json={"on": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(settings.ai_paused())
        self.assertFalse(run.cancel_flag.is_set())

    def test_business_reset_clears_records_and_preserves_settings(self):
        self.seed_business_records()
        settings.put("ui_lang", "en")
        settings.set_secret("unit_test_key", "synthetic-test-key")
        response = self.client.post("/api/data/delete-all", json={"confirm": "DELETE", "scope": "business"})
        self.assertEqual(response.status_code, 200)
        self.assert_business_empty()
        self.assert_schedule_reset()
        self.assertEqual(settings.get("ui_lang"), "en")
        self.assertEqual(settings.get_secret("unit_test_key"), "synthetic-test-key")

    def test_full_reset_clears_business_records_and_secrets(self):
        self.seed_business_records()
        settings.put("ui_lang", "en")
        settings.put("qa_custom_setting", {"synthetic": True})
        settings.set_secret("unit_test_key", "synthetic-test-key")
        response = self.client.post("/api/data/delete-all", json={"confirm": "DELETE", "scope": "everything"})
        self.assertEqual(response.status_code, 200)
        self.assert_business_empty()
        self.assert_schedule_reset()
        self.assertEqual(settings.get_secret("unit_test_key"), "")
        self.assertEqual(db.query("SELECT * FROM secrets"), [])
        self.assertIsNone(db.query_one("SELECT * FROM settings WHERE key='qa_custom_setting'"))
        self.assertEqual(settings.get("ui_lang"), "zh")

    def test_invalid_delete_scope_preserves_data(self):
        self.seed_business_records()
        settings.put("ui_lang", "en")
        settings.set_secret("unit_test_key", "synthetic-test-key")
        before = self.business_snapshot()
        for scope in ("typo", "", None, 0, True, [], {}, ["business"]):
            with self.subTest(scope=scope):
                response = self.client.post("/api/data/delete-all", json={"confirm": "DELETE", "scope": scope})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.business_snapshot(), before)
                self.assertEqual(settings.get("ui_lang"), "en")
                self.assertEqual(settings.get_secret("unit_test_key"), "synthetic-test-key")

    def test_default_delete_scope_is_business(self):
        self.seed_business_records()
        settings.put("ui_lang", "en")
        settings.set_secret("unit_test_key", "synthetic-test-key")
        response = self.client.post("/api/data/delete-all", json={"confirm": "DELETE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["scope"], "business")
        self.assert_business_empty()
        self.assertEqual(settings.get("ui_lang"), "en")
        self.assertEqual(settings.get_secret("unit_test_key"), "synthetic-test-key")

    def test_pause_waits_for_registration_then_cancels_new_task(self):
        validating, release, pause_attempt = (threading.Event() for _ in range(3))
        clean = dict(icp_id=None, target=1, sources=["web"], urls=[], follow_links=False)

        def validate(_):
            validating.set()
            self.assertTrue(release.wait(5), "registration barrier timed out")
            return clean

        observed = self.observed_lock(tasks._lock, "pause-worker", pause_attempt)
        with mock.patch.object(tasks, "_lock", observed), \
                mock.patch.object(tasks, "validate_research", side_effect=validate), \
                mock.patch.object(tasks, "_execute", return_value=None):
            creator = self.start_worker("create-worker", lambda: tasks.create_task({}))
            self.assertTrue(validating.wait(5))
            pauser = self.start_worker("pause-worker", lambda: desktop_api.pause({"on": True}))
            try:
                self.assertTrue(pause_attempt.wait(5))
                self.assertFalse(settings.ai_paused(), "pause must wait for the registering task")
            finally:
                release.set()
            created = self.finish_worker(creator)
            self.finish_worker(pauser)
        run = tasks._RUNS[created["id"]]
        self.assertTrue(settings.ai_paused())
        self.assertTrue(run.cancel_flag.is_set())
        self.assertTrue(run.should_stop())
        before = db.query("SELECT id FROM tasks")
        with self.assertRaises(UserError) as rejected:
            tasks.create_task({}, kind="pool")
        self.assertEqual(rejected.exception.key, "ai_paused")
        self.assertEqual(db.query("SELECT id FROM tasks"), before)

    def test_reset_waits_for_tick_and_rejects_the_registered_task(self):
        self.seed_business_records()
        due_ready, release, reset_attempt = (threading.Event() for _ in range(3))
        original_due = schedules.due

        def due(ts=None):
            result = original_due(ts)
            due_ready.set()
            self.assertTrue(release.wait(5), "schedule barrier timed out")
            return result

        def reset():
            try:
                server.delete_all({"confirm": "DELETE", "scope": "business"})
            except UserError as exc:
                return exc.key
            self.fail("reset must refuse a task just registered by the schedule")

        observed = self.observed_lock(schedules._lock, "reset-worker", reset_attempt)
        clean = dict(icp_id=1, target=1, sources=["web"], urls=[], follow_links=False)
        with mock.patch.object(schedules, "_lock", observed), \
                mock.patch.object(schedules, "due", side_effect=due), \
                mock.patch.object(tasks, "validate_research", return_value=clean), \
                mock.patch.object(tasks, "_execute", return_value=None):
            ticker = self.start_worker("tick-worker", schedules.tick)
            self.assertTrue(due_ready.wait(5))
            resetter = self.start_worker("reset-worker", reset)
            try:
                self.assertTrue(reset_attempt.wait(5))
                self.assertIsNotNone(db.query_one("SELECT id FROM icp_templates WHERE id=1"))
            finally:
                release.set()
            started = self.finish_worker(ticker)
            rejection = self.finish_worker(resetter)
        self.assertEqual(rejection, "task_already_running")
        self.assertEqual(len(started), 1)
        self.assertFalse(tasks._RUNS[started[0]].done)
        self.assertIsNotNone(db.query_one("SELECT id FROM tasks WHERE id=?", (started[0],)))
        self.assertIsNotNone(db.query_one("SELECT id FROM task_schedules"))
        self.assertEqual(db.query("SELECT * FROM alerts WHERE category='schedule_failed'"), [])

    def test_tick_waits_for_reset_and_cannot_use_old_schedule(self):
        self.seed_business_records()
        deleting, release, tick_attempt = (threading.Event() for _ in range(3))
        original_execute = db.execute

        def execute(sql, args=()):
            result = original_execute(sql, args)
            if sql == "DELETE FROM task_schedules":
                deleting.set()
                self.assertTrue(release.wait(5), "reset barrier timed out")
            return result

        observed = self.observed_lock(schedules._lock, "tick-worker", tick_attempt)
        with mock.patch.object(schedules, "_lock", observed), \
                mock.patch.object(db, "execute", side_effect=execute), \
                mock.patch.object(tasks, "create_task") as create:
            resetter = self.start_worker("reset-worker", lambda: server.delete_all({"confirm": "DELETE"}))
            self.assertTrue(deleting.wait(5))
            ticker = self.start_worker("tick-worker", schedules.tick)
            try:
                self.assertTrue(tick_attempt.wait(5))
                self.assertTrue(ticker[0].is_alive())
            finally:
                release.set()
            self.assertTrue(self.finish_worker(resetter)["ok"])
            self.assertEqual(self.finish_worker(ticker), [])
            create.assert_not_called()
        self.assert_business_empty()
        self.assert_schedule_reset()

    def test_reset_rolls_back_business_and_settings_on_delete_failure(self):
        self.seed_business_records()
        finished = self.make_run("finished", done=True)
        settings.put("ui_lang", "en")
        settings.set_secret("unit_test_key", "synthetic-test-key")
        before = self.business_snapshot()
        original_execute = db.execute

        def execute(sql, args=()):
            if sql == "DELETE FROM settings":
                raise RuntimeError("synthetic deletion failure")
            return original_execute(sql, args)

        with mock.patch.object(db, "execute", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "synthetic deletion failure"):
                server.delete_all({"confirm": "DELETE", "scope": "everything"})
        self.assertEqual(self.business_snapshot(), before)
        self.assertEqual(settings.get("ui_lang"), "en")
        self.assertEqual(settings.get_secret("unit_test_key"), "synthetic-test-key")
        self.assertIs(tasks._RUNS[finished.id], finished)

    def test_reset_rejects_task_before_final_event_has_been_written(self):
        run = self.make_run("monitor", "monitor")
        closing, release = threading.Event(), threading.Event()
        original_emit = run.emit

        def emit(agent, key, **kwargs):
            if key == "ev.task_closed":
                closing.set()
                self.assertTrue(release.wait(5), "final event barrier timed out")
            return original_emit(agent, key, **kwargs)

        with mock.patch("salesteam.agents.monitor.Monitor.execute", return_value=None), \
                mock.patch.object(run, "emit", side_effect=emit):
            worker = self.start_worker("monitor-worker", lambda: tasks._execute_monitor(run))
            try:
                self.assertTrue(closing.wait(5))
                self.assertFalse(run.done)
                with self.assertRaises(UserError) as rejected:
                    server.delete_all({"confirm": "DELETE"})
                self.assertEqual(rejected.exception.key, "task_already_running")
            finally:
                release.set()
            self.finish_worker(worker)
        self.assertTrue(run.done)
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM task_events WHERE task_id=? AND key='ev.task_closed'", (run.id,))["n"], 1)
        self.assertTrue(server.delete_all({"confirm": "DELETE"})["ok"])
        self.assertEqual(db.query("SELECT * FROM task_events"), [])

    def test_company_edit_merges_fields_and_preserves_product_metadata(self):
        self.seed_business_records()
        original = {
            "name": {"text": "Old company", "sources": ["https://qa.example/about"]},
            "summary": {"text": "Old summary", "sources": ["pdf:qa.pdf"], "confidence": 0.9},
            "product_lines": [{"name": "QA product", "detail": "Keep technical detail", "sources": ["interview"]}],
            "signature": "Keep signature", "extra_context": {"keep": True},
        }
        settings.put("company_profile", original)
        before_templates = db.query("SELECT * FROM icp_templates")
        before_versions = db.query("SELECT * FROM profile_versions")
        fields = ("name", "summary", "oem_odm", "advantages", "certifications", "factory", "cases", "markets",
                  "moq", "price_range", "payment_terms", "lead_time", "competitors")
        patch = {field: f"  Corrected {field}  " for field in fields}
        patch.update(product_lines=[" QA product ", "New product"], extra_context="should be ignored", icp="not writable")
        response = self.client.put("/api/company", json=patch)
        self.assertEqual(response.status_code, 200)
        saved = self.client.get("/api/company").json()
        for field in fields:
            with self.subTest(field=field):
                self.assertEqual(saved[field]["text"], f"Corrected {field}")
                self.assertIn("input:customer", saved[field]["sources"])
        self.assertIn("https://qa.example/about", saved["name"]["sources"])
        self.assertIn("pdf:qa.pdf", saved["summary"]["sources"])
        self.assertEqual(saved["summary"]["confidence"], 0.9)
        self.assertEqual(saved["product_lines"][0]["detail"], "Keep technical detail")
        self.assertEqual(saved["product_lines"][0]["sources"], ["input:customer", "interview"])
        self.assertEqual(saved["product_lines"][1], {"name": "New product", "sources": ["input:customer"]})
        self.assertEqual(saved["extra_context"], original["extra_context"])
        self.assertEqual(saved["signature"], original["signature"])
        self.assertNotIn("icp", saved)
        self.assertEqual(db.query("SELECT * FROM icp_templates"), before_templates)
        self.assertEqual(db.query("SELECT * FROM profile_versions"), before_versions)

    def test_company_invalid_patch_is_atomic(self):
        original = {"name": {"text": "Old name"}, "summary": {"text": "Old summary"}, "product_lines": []}
        settings.put("company_profile", original)
        invalid = (
            {"summary": None}, {"summary": True}, {"summary": " "}, {"summary": "x" * 2001},
            {"advantages": {}}, {"markets": []}, {"product_lines": "one product"},
            {"product_lines": [1]}, {"product_lines": [""]}, {"product_lines": ["x" * 201]},
            {"product_lines": [f"Product {n}" for n in range(51)]},
        )
        for bad in invalid:
            with self.subTest(field=next(iter(bad))):
                response = self.client.put("/api/company", json={"name": "Should not save", **bad})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(settings.get("company_profile"), original)

    def test_redo_baseline_uses_latest_corrections_without_changing_history(self):
        self.seed_business_records()
        historical = {"summary": {"text": "Historical summary", "sources": ["pdf:qa.pdf"]}}
        historical_icp = {"product": "Historical ICP product", "customer_types": ["resellers"]}
        db.execute("UPDATE profile_versions SET company=?, icp=?", (db.dumps(historical), db.dumps(historical_icp)))
        before_versions = db.query("SELECT * FROM profile_versions")
        current = {"summary": {"text": "Corrected summary", "sources": ["input:customer"]}, "updated_at": 123, "onboarding_id": 1}
        settings.put("company_profile", current)
        self.assertEqual(onboarding._previous_profile(), {
            "company": {"summary": current["summary"]}, "icp": historical_icp,
        })
        self.assertEqual(settings.get("company_profile"), current)
        self.assertEqual(db.query("SELECT * FROM profile_versions"), before_versions)
        db.execute("DELETE FROM settings WHERE key='company_profile'")
        self.assertEqual(onboarding._previous_profile(), {"company": historical, "icp": historical_icp})

    def test_redo_baseline_reads_manual_company_before_any_completed_profile(self):
        current = {"summary": {"text": "Manual summary", "sources": ["input:customer"]}, "updated_at": 123}
        settings.put("company_profile", current)
        self.assertEqual(db.query("SELECT * FROM profile_versions"), [])
        self.assertEqual(onboarding._previous_profile(), {"company": {"summary": current["summary"]}, "icp": {}})
        self.assertEqual(settings.get("company_profile"), current)
        db.execute("DELETE FROM settings WHERE key='company_profile'")
        self.assertIsNone(onboarding._previous_profile())

    def test_reset_rejects_running_tasks_and_missing_confirmation(self):
        self.make_run("research")
        for payload in ({"scope": "everything"}, {"confirm": "DELETE", "scope": "everything"}):
            response = self.client.post("/api/data/delete-all", json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertIsNotNone(db.query_one("SELECT id FROM tasks WHERE id='research'"))


if __name__ == "__main__":
    unittest.main()

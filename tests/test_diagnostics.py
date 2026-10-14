"""Privacy and useful-zero-results checks for the local diagnostic download."""
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import zipfile

import test_app as app_tests
from salesteam import VERSION, db, llm, paths, settings, vault


class DiagnosticsTests(unittest.TestCase):
    # Reuse isolation only; do not inherit AppTests or bind it in module scope,
    # which would make unittest discover its existing tests for a second time.
    setUp = app_tests.AppTests.setUp

    def archive(self, payload=None):
        from salesteam import diagnostics
        filename, payload = diagnostics.build() if payload is None else ("download.zip", payload)
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            return filename, {name: archive.read(name) for name in archive.namelist()}

    @staticmethod
    def text(files):
        return "\n".join(value.decode("utf-8") for value in files.values())

    def task(self, ident="zero-results", *, created_at=None, events=0):
        now = db.now() if created_at is None else created_at
        db.execute("INSERT INTO tasks(id,kind,params,status,target,stats,result,error,created_at,finished_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (ident, "research", db.dumps({"sources": ["google", "youtube"], "target": 10}), "done", 10,
                    db.dumps({"processed": 4, "qualified": 0, "discarded": 3, "errors": 1}),
                    "no_results", "SSL CERTIFICATE_VERIFY_FAILED unable to get local issuer certificate", now, now))
        for seq in range(1, events + 1):
            db.execute("INSERT INTO task_events(task_id,seq,ts,agent,level,key,params) VALUES(?,?,?,?,?,?,?)",
                       (ident, seq, now + seq / 1000, "researcher", "info", "ev.source_error",
                        db.dumps({"source": "google", "reason": "search_blocked", "n": seq})))
        return ident

    def seed_zero_results(self):
        ident = self.task(events=3)
        for source, status, reason in (("google", "filtered", "keywords_no_match"),
                                       ("google", "filtered", "keywords_no_match"),
                                       ("google", "filtered", "keywords_no_match"),
                                       ("youtube", "error", "youtube_key_missing")):
            db.execute("INSERT INTO raw_leads(task_id,source,data,status,reason,created_at) VALUES(?,?,?,?,?,?)",
                       (ident, source, db.dumps({"customer": "DO_NOT_EXPORT_RAW_CUSTOMER"}), status, reason, db.now()))
        return ident

    def snapshot(self):
        tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: db.query(f'SELECT * FROM "{table}"') for table in tables}

    def test_zip_members_manifest_and_local_attachment_response(self):
        self.seed_zero_results()
        (paths.DATA_DIR / "app.log").write_text("SSL CERTIFICATE_VERIFY_FAILED\n", encoding="utf-8")
        (paths.DATA_DIR / "app.prev.log").write_text("previous startup\n", encoding="utf-8")
        filename, files = self.archive()
        self.assertEqual(set(files), {"manifest.json", "config.json", "tasks.json", "summary.json", "README.txt",
                                      "logs/app.log", "logs/app.prev.log"})
        manifest = json.loads(files["manifest.json"])
        self.assertIn(VERSION, json.dumps(manifest))
        self.assertTrue(manifest)
        response = self.client.get("/api/diagnostics/export")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/zip")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn(".zip", response.headers["content-disposition"])
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertTrue(self.archive(response.content)[1])

    def test_zero_results_summary_keeps_source_status_and_failure_evidence(self):
        self.seed_zero_results()
        _, files = self.archive()
        summary = json.loads(files["summary.json"])
        self.assertEqual(summary["task_status_counts"], [{"status": "done", "count": 1}])
        self.assertEqual(summary["raw_status_counts"], [
            {"source": "google", "status": "filtered", "count": 3},
            {"source": "youtube", "status": "error", "count": 1},
        ])
        self.assertEqual(summary["lead_status_counts"], [])
        self.assertEqual(summary["failure_reasons"], [
            {"status": "filtered", "reason": "keywords_no_match", "count": 3},
            {"status": "error", "reason": "youtube_key_missing", "count": 1},
        ])
        text = json.dumps(summary)
        for value in ("google", "youtube", "filtered", "error", "keywords_no_match", "youtube_key_missing"):
            self.assertIn(value, text)
        tasks = json.loads(files["tasks.json"])
        task_text = json.dumps(tasks)
        for value in ("zero-results", "no_results", "CERTIFICATE_VERIFY_FAILED", "qualified", "processed"):
            self.assertIn(value, task_text)
        self.assertNotIn("DO_NOT_EXPORT_RAW_CUSTOMER", self.text(files))

    def test_credentials_customer_records_and_files_are_not_exported(self):
        secret_values = {
            "agent": "QA-AGENT-SECRET-8993999b39",
            "search": "QA-SEARCH-SECRET-38287af45c",
            "session": "QA-TG-SESSION-PRIVATE-4838a98ad",
            "proxy": "QA-PROXY-PASSWORD-588998aa",
        }
        llm.update_config("analyst", {"provider": "openai_compat", "model": "qa-model",
                                    "api_key": secret_values["agent"], "base_url": "https://qa.example/v1"})
        settings.set_secret("serper_api_key", secret_values["search"])
        encrypted_session, encrypted_proxy = vault.encrypt(secret_values["session"]), vault.encrypt(secret_values["proxy"])
        db.execute("INSERT INTO tg_accounts(phone,status,session_enc,proxy,added_at) VALUES(?,?,?,?,?)",
                   ("+12025550199", "active", encrypted_session, db.dumps({"password_enc": encrypted_proxy}), db.now()))
        db.execute("INSERT INTO tg_accounts(phone,status,login_state,proxy,added_at) VALUES(?,?,?,?,?)",
                   ("+12025550201", "active", "[]", '"corrupt-proxy-shape"', db.now()))
        customer = "CUSTOMER_PROFILE_SENTINEL_7838"
        message = "CUSTOMER_MESSAGE_ORIGINAL_9825"
        self.task()
        db.execute("UPDATE tasks SET params=?,stats=? WHERE id='zero-results'",
                   (db.dumps({"sources": ["google"], "target": 10, "customer_profile": customer, "urls": [customer]}),
                    db.dumps({"processed": 1, "qualified": 0, "note": message})))
        db.execute("INSERT INTO task_events(task_id,seq,ts,agent,level,key,params) VALUES(?,?,?,?,?,?,?)",
                   ("zero-results", 1, db.now(), "researcher", "info", "ev.source_error",
                    db.dumps({"source": "google", "company_name": customer, "raw_message": message,
                              "contacts": [{"email": "qa.private@example.com"}], "api_key": secret_values["agent"]})))
        db.execute("INSERT INTO leads(company_name,company_key,grade,status,profile,contacts,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (customer, customer, "C", "backup", db.dumps({"summary": message}),
                                              db.dumps([{"email": "qa.private@example.com"}]), db.now(), db.now()))
        settings.put("company_profile", {"name": customer, "summary": message})
        settings.put("private_unrelated_setting", {"customer": customer, "password": "UNLISTED-PASSWORD-912875"})
        db.execute("INSERT INTO agent_chats(agent,role,content,ts) VALUES(?,?,?,?)",
                   ("manager", "user", message, db.now()))
        for directory in (paths.UPLOAD_DIR, paths.EXPORT_DIR):
            directory.mkdir(exist_ok=True)
            (directory / "private-customer.txt").write_text("CUSTOMER_ATTACHMENT_SENTINEL_51364", encoding="utf-8")
        (paths.DATA_DIR / "unrelated.txt").write_text("UNRELATED_FILE_SENTINEL_2841", encoding="utf-8")
        (paths.DATA_DIR / "app.log").write_text("\n".join(secret_values.values()), encoding="utf-8")
        _, files = self.archive()
        text = self.text(files)
        forbidden = [*secret_values.values(), encrypted_session, encrypted_proxy, customer, message,
                     "qa.private@example.com", "+12025550199", "UNLISTED-PASSWORD-912875",
                     "CUSTOMER_ATTACHMENT_SENTINEL_51364", "UNRELATED_FILE_SENTINEL_2841",
                     paths.KEY_PATH.read_text().strip(), str(paths.DATA_DIR)]
        for value in forbidden:
            with self.subTest(value=value[:20]):
                self.assertNotIn(value, text)
        self.assertNotIn(vault.mask(secret_values["agent"]), text, "Key hints must not be exported")
        self.assertFalse(any(name.endswith((".db", ".key", ".session")) or name.startswith(("uploads/", "exports/"))
                             for name in files))
        config = json.loads(files["config.json"])
        analyst = next(row for row in config["agents"] if row["agent"] == "analyst")
        self.assertIs(analyst["key_present"], True)
        self.assertIs(config["source_keys_present"]["serper_api_key"], True)
        self.assertIs(config["source_keys_present"]["youtube_api_key"], False)

    @staticmethod
    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from DiagnosticsTests.leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from DiagnosticsTests.leaves(item)
        else:
            yield value

    def test_log_authentication_query_email_phone_and_local_paths_are_redacted(self):
        lines = [
            "Authorization: Bearer bearer-only-secret-8992817",
            "Cookie: harmless=x; sessionid=COOKIE-SESSION-ONLY-2918; csrftoken=COOKIE-CSRF-ONLY-9472",
            "Set-Cookie: refresh_token=COOKIE-REFRESH-ONLY-5148; Path=/; HttpOnly",
            "token=OPAQUE-TOKEN-ONLY-77191",
            "X-Auth-Token: OPAQUE-XAUTH-ONLY-43183",
            "https://qa.example/callback?auth=OPAQUE-AUTH-ONLY-37841",
            "api_key=label-only-key-879721 password=label-only-password-999917",
            "https://url-user-8391:url-pass-7181@nano-gpt.com/api/v1/models?api_key=query-api-only-113&access_token=query-token-only-441&password=query-pass-only-313&mode=test",
            "https://qa.example/download?X-Amz-Signature=signed-url-only-2817&X-Goog-Credential=credential-query-only-745",
            'email=diagnostics.private@example.com phone=+1 (202) 555-0123',
            'File "/Users/diagnostics-owner/private/customer.log", line 3',
            r'File "C:\Users\diagnostics-owner\AppData\Roaming\private.log", line 7',
            "SSL CERTIFICATE_VERIFY_FAILED unable to get local issuer certificate",
            "source=google raw=0 filtered=0 qualified=0 reason=search_blocked",
        ]
        (paths.DATA_DIR / "app.log").write_text("\n".join(lines), encoding="utf-8")
        _, files = self.archive()
        text = files["logs/app.log"].decode("utf-8")
        for secret in ("bearer-only-secret-8992817", "COOKIE-SESSION-ONLY-2918", "COOKIE-CSRF-ONLY-9472",
                       "COOKIE-REFRESH-ONLY-5148", "OPAQUE-TOKEN-ONLY-77191", "OPAQUE-XAUTH-ONLY-43183",
                       "OPAQUE-AUTH-ONLY-37841", "label-only-key-879721", "label-only-password-999917",
                       "url-user-8391", "url-pass-7181", "query-api-only-113", "query-token-only-441",
                       "query-pass-only-313", "signed-url-only-2817", "credential-query-only-745",
                       "diagnostics.private@example.com", "+1 (202) 555-0123", "diagnostics-owner"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        for diagnostic in ("nano-gpt.com", "CERTIFICATE_VERIFY_FAILED", "source=google", "search_blocked"):
            self.assertIn(diagnostic, text)

    def test_recent_twenty_tasks_and_hundred_events_each_are_bounded(self):
        for index in range(23):
            self.task(f"task-{index:02d}", created_at=1000 + index, events=105 if index == 22 else 1)
        _, files = self.archive()
        decoded = json.loads(files["tasks.json"])
        rows = decoded if isinstance(decoded, list) else decoded.get("tasks")
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 20)
        self.assertEqual({row["id"] for row in rows}, {f"task-{i:02d}" for i in range(3, 23)})
        newest = next(row for row in rows if row["id"] == "task-22")
        self.assertIn("events", newest)
        self.assertEqual(len(newest["events"]), 100)
        self.assertEqual({e["seq"] for e in newest["events"]}, set(range(6, 106)))
        self.assertTrue(all((event.get("event_key") or event.get("key")) == "ev.source_error"
                            for event in newest["events"]), "diagnostic event symbols must survive redaction")
        self.assertTrue(all(len(row["events"]) <= 100 for row in rows))

    def test_logs_are_tail_bounded_and_rotation_may_be_missing(self):
        # Byte limits apply to the resulting archive too, including multibyte log text.
        for name in ("app.log", "app.prev.log"):
            middle = ("日志行\n" * 70000).encode("utf-8") if name == "app.log" else b"api_key=x\n" * 40000
            content = b"START_MUST_NOT_BE_INCLUDED\n" + middle + b"TAIL_LAST_LINE\n"
            (paths.DATA_DIR / name).write_bytes(content)
        _, files = self.archive()
        for name in ("logs/app.log", "logs/app.prev.log"):
            self.assertLessEqual(len(files[name]), 256 * 1024)
            self.assertNotIn(b"START_MUST_NOT_BE_INCLUDED", files[name])
            self.assertIn(b"TAIL_LAST_LINE", files[name])
            files[name].decode("utf-8")
        (paths.DATA_DIR / "app.prev.log").unlink()
        self.assertTrue(self.archive()[1])
        (paths.DATA_DIR / "app.log").unlink()
        self.assertTrue(self.archive()[1])

    def test_download_does_not_mutate_business_data_or_attempt_network(self):
        self.seed_zero_results()
        settings.set_secret("qa_secret", "READONLY_SECRET_119385")
        (paths.DATA_DIR / "app.log").write_text("local diagnostic evidence\n", encoding="utf-8")
        before = self.snapshot()
        digest = hashlib.sha256(paths.DB_PATH.read_bytes()).hexdigest()
        files = {str(p.relative_to(paths.DATA_DIR)): p.read_bytes() for p in paths.DATA_DIR.rglob("*") if p.is_file()}
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected diagnostics network")), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("unexpected diagnostics socket")):
            self.assertTrue(self.archive()[1])
            self.assertEqual(self.client.get("/api/diagnostics/export").status_code, 200)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(hashlib.sha256(paths.DB_PATH.read_bytes()).hexdigest(), digest)
        after = {str(p.relative_to(paths.DATA_DIR)): p.read_bytes() for p in paths.DATA_DIR.rglob("*") if p.is_file()}
        self.assertEqual(after, files)
        # A missing encryption key must not be silently recreated by diagnostic
        # decryption, which would replace the user's recoverable credential state.
        paths.KEY_PATH.unlink()
        vault.reset()
        snapshot_without_key = self.snapshot()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected diagnostics network")), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("unexpected diagnostics socket")):
            self.assertTrue(self.archive()[1])
        self.assertFalse(paths.KEY_PATH.exists())
        self.assertEqual(self.snapshot(), snapshot_without_key)

    def test_external_host_and_origin_cannot_export(self):
        for headers in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"},
                        {"Origin": "http://localhost.attacker.example"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get("/api/diagnostics/export", headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()

"""Conversation/memo deletion scope and late model response regressions."""
import threading
import unittest
from unittest import mock

import test_app
from salesteam import db, settings
from salesteam.agents import chat, memory
from salesteam.errors import UserError


class ChatClearTests(unittest.TestCase):
    def setUp(self):
        test_app.AppTests.setUp(self)
        self.seed_business_records = test_app.AppTests.seed_business_records.__get__(self)

    def seed_context(self):
        chat.post("manager", "old manager reply", user_text="old customer question")
        chat.post("analyst", "other agent conversation")
        memory.add_memo("manager", "old manager instruction", status="confirmed")
        memory.add_memo("analyst", "other agent instruction", status="confirmed")
        memory.add_memo("all", "shared instruction", status="confirmed")
        chat.mark_seen("manager")
        chat.mark_seen("analyst")

    def test_history_clear_keeps_memos_and_other_agent_and_resets_unread(self):
        self.seed_context()
        other_seen = settings.get("chat_seen")["analyst"]
        response = self.client.delete("/api/chat/manager")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(chat.history("manager"), [])
        self.assertEqual(len(db.query("SELECT * FROM memos")), 3)
        self.assertEqual(len(chat.history("analyst")), 1)
        self.assertNotIn("manager", settings.get("chat_seen"))
        self.assertEqual(settings.get("chat_seen")["analyst"], other_seen)
        self.assertEqual(chat.unread("manager"), {"manager": 0})

    def test_agent_memory_clear_preserves_shared_and_other_agent_context(self):
        self.seed_context()
        response = self.client.delete("/api/memory/manager")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["shared_memos_retained"])
        self.assertEqual(chat.history("manager"), [])
        self.assertEqual(len(chat.history("analyst")), 1)
        self.assertEqual({r["agent"] for r in db.query("SELECT * FROM memos")}, {"all", "analyst"})
        with mock.patch("salesteam.llm.configured", return_value=True), \
                mock.patch("salesteam.llm.call_json", return_value=({"reply": "fresh", "memos": []}, {})) as model:
            chat.send("manager", "new question")
        system, user = model.call_args.args[2:4]
        self.assertNotIn("old manager instruction", system)
        self.assertIn("shared instruction", system)
        self.assertNotIn("old customer question", user)
        self.assertNotIn("old manager reply", user)

    def test_all_memory_clear_preserves_business_records_configuration_and_prompts(self):
        self.seed_business_records()
        self.seed_context()
        db.execute("INSERT INTO prompt_overrides(name,text,updated_at) VALUES(?,?,?)",
                   ("chat.system", "customer prompt", db.now()))
        settings.put("company_profile", {"name": "QA business"})
        tables = ["icp_templates", "leads", "channels", "icp_samples", "icp_knowledge", "tasks", "tg_join_queue", "prompt_overrides"]
        before = {table: db.query(f"SELECT * FROM {table}") for table in tables}
        response = self.client.delete("/api/memory/all")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["shared_memos_retained"])
        self.assertEqual(db.query("SELECT * FROM agent_chats"), [])
        self.assertEqual(db.query("SELECT * FROM memos"), [])
        self.assertEqual(settings.get("chat_seen"), {})
        self.assertEqual(settings.get("company_profile"), {"name": "QA business"})
        self.assertEqual(before, {table: db.query(f"SELECT * FROM {table}") for table in tables})

    def assert_late_reply_discarded(self, endpoint):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow_model(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("clear blocked behind the model request")
            return {"reply": "late reply", "memos": ["late instruction"]}, {}

        def send():
            try:
                chat.send("manager", "in-flight question")
            except Exception as exc:
                errors.append(exc)

        with mock.patch("salesteam.llm.configured", return_value=True), \
                mock.patch("salesteam.llm.call_json", side_effect=slow_model):
            worker = threading.Thread(target=send)
            worker.start()
            try:
                self.assertTrue(entered.wait(3))
                if endpoint == "single-message":
                    mid = db.query_one("SELECT id FROM agent_chats WHERE agent='manager' AND role='user'")["id"]
                    endpoint = f"/api/chat/manager/messages/{mid}"
                self.assertEqual(self.client.delete(endpoint).status_code, 200)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], UserError)
        self.assertEqual(errors[0].key, "chat_cleared")
        self.assertEqual(chat.history("manager"), [])
        self.assertEqual(db.query("SELECT * FROM memos"), [])

    def test_late_response_cannot_restore_cleared_conversation_or_create_memo(self):
        self.assert_late_reply_discarded("/api/chat/manager")

    def test_late_response_cannot_restore_globally_cleared_memory(self):
        self.assert_late_reply_discarded("/api/memory/all")

    def test_single_message_delete_preserves_other_messages_and_saved_memos(self):
        self.seed_context()
        old = chat.history("manager")
        mid = old[0]["id"]
        self.assertEqual(self.client.delete(f"/api/chat/analyst/messages/{mid}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/chat/manager/messages/{mid}").status_code, 200)
        self.assertEqual([r["id"] for r in chat.history("manager")], [old[1]["id"]])
        self.assertEqual(len(chat.history("analyst")), 1)
        self.assertEqual(len(db.query("SELECT * FROM memos")), 3)
        self.assertEqual(self.client.delete(f"/api/chat/manager/messages/{mid}").status_code, 404)
        with mock.patch("salesteam.llm.configured", return_value=True), \
                mock.patch("salesteam.llm.call_json", return_value=({"reply": "fresh", "memos": []}, {})) as model:
            chat.send("manager", "new question")
        self.assertNotIn("old customer question", model.call_args.args[3])
        self.assertIn("old manager reply", model.call_args.args[3])

    def test_deleting_in_flight_user_message_discards_its_late_reply_and_memos(self):
        self.assert_late_reply_discarded("single-message")

    def test_invalid_agent_does_not_delete_records(self):
        self.seed_context()
        for endpoint in ("/api/chat/unknown", "/api/memory/unknown", "/api/chat/all"):
            self.assertEqual(self.client.delete(endpoint).status_code, 404)
        self.assertEqual(len(db.query("SELECT * FROM agent_chats")), 3)
        self.assertEqual(len(db.query("SELECT * FROM memos")), 3)


if __name__ == "__main__":
    unittest.main()

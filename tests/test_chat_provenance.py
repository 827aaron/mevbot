"""Distinguish model answers/refusal flags from local feature restrictions."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

import test_app
from salesteam import db, llm
from salesteam.agents import chat, icp
from salesteam.errors import UserError
from salesteam.llm import openai_compat


class ChatProvenanceTests(unittest.TestCase):
    def setUp(self):
        test_app.AppTests.setUp(self)
        llm.update_config("manager", {"provider": "openai_compat", "model": "qa-model",
                                      "base_url": "https://qa.example/v1", "api_key": "qa-local-key"})

    def completion(self, reply, refusal=None, finish_reason="stop"):
        client = mock.Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {"reply": reply, "memos": [], "goto": "channels"}), refusal=refusal), finish_reason=finish_reason)],
            model="qa-model", usage=SimpleNamespace(prompt_tokens=20, completion_tokens=15))
        return client

    def test_business_configuration_question_reaches_provider_without_local_topic_filter(self):
        text = "怎样每天监控博彩行业的代理群，并添加 FB、X 和 Reddit 来源？"
        answer = "在来源池设置每日时间，并在账号页配置需要的平台登录。"
        client = self.completion(answer)
        with mock.patch.object(openai_compat, "_client", return_value=client):
            result = chat.send("manager", text)
        self.assertEqual(result["reply"], answer)
        request = client.chat.completions.create.call_args.kwargs
        self.assertIn(text, request["messages"][-1]["content"])
        self.assertIn("Software capabilities", request["messages"][0]["content"])
        self.assertEqual(db.query("SELECT * FROM tasks"), [])
        self.assertEqual(db.query("SELECT * FROM tg_join_queue"), [])

    def test_normal_json_refusal_text_is_provider_reply_not_rewritten_by_application(self):
        answer = "QA provider declines this question."
        client = self.completion(answer)
        with mock.patch.object(openai_compat, "_client", return_value=client):
            result = chat.send("manager", "QA question")
        self.assertEqual(result["reply"], answer)
        self.assertEqual(db.query_one("SELECT content FROM agent_chats WHERE role='assistant'")["content"], answer)

    def test_real_provider_refusal_flag_is_recorded_as_provider_error(self):
        client = self.completion("", refusal="QA provider refusal", finish_reason="content_filter")
        with mock.patch.object(openai_compat, "_client", return_value=client), \
                mock.patch("salesteam.llm.backup.config_for", return_value=None):
            with self.assertRaises(UserError) as refused:
                chat.send("manager", "QA question")
        self.assertEqual(refused.exception.key, "model_refused")
        self.assertEqual(db.query_one("SELECT refused FROM usage WHERE purpose='agent_chat'")["refused"], 1)
        self.assertEqual(db.query("SELECT * FROM agent_chats WHERE role='assistant'"), [])

    def test_special_profile_generation_uses_the_same_agent_market_labels_as_collection(self):
        with mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "my_igaming"}):
            scope = icp.scope_vars()["scope"]
        self.assertIn("Record market evidence as matched, unverified or mismatch without discarding agent signals", scope)
        self.assertNotIn("Unknown market or B2B role belongs in backup", scope)


if __name__ == "__main__":
    unittest.main()

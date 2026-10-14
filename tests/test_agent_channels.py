"""Public agent evidence and bounded, opt-in TG collection. No real TG traffic."""
import os
import time
import unittest
from unittest import mock

import test_app as app_tests
from salesteam import background, db, edition, llm, settings, tasks, vault
from salesteam.agents import analyst, icp, manager, monitor, scout
from salesteam.tg import joining, pool


class AgentChannelTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "my_igaming"}))
        app_tests.AppTests.setUp(self)
        self.profile = icp.normalize(edition.presets()[0]["data"])
        self.icp_id = db.execute("INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)",
                                 ("QA agent channels", db.dumps(self.profile), db.now(), db.now()))
        for agent in ("scout", "monitor", "analyst", "manager"):
            llm.update_config(agent, {"provider": "mock", "model": "qa-mock"})
        settings.put("tg", {"backend": "mock"})
        self.enterContext(mock.patch.object(pool, "_backend", None))
        self.enterContext(mock.patch.object(scout, "_active_days", set()))
        self.aid = db.execute("INSERT INTO tg_accounts(phone,status,session_enc,daily_join_limit,warmup_days,added_at) "
                              "VALUES(?,?,?,?,?,?)", ("+10000000001", "active", vault.encrypt("mock-session:10000000001"),
                                                       5, 0, time.time() - 10 * 86400))
        self.sequence = 0

    def make_run(self, continuous=False):
        self.sequence += 1
        ident = "qa-agent-channel-" + str(self.sequence)
        params = {"sources": ["tg"], "continuous_monitor": continuous}
        db.execute("INSERT INTO tasks(id,kind,icp_id,params,status,target,created_at) VALUES(?,?,?,?,?,?,?)",
                   (ident, "research", self.icp_id, db.dumps(params), "running", 1, db.now()))
        run = tasks.TaskRun(db.query_one("SELECT * FROM tasks WHERE id=?", (ident,)))
        tasks._RUNS[ident] = run
        return run

    def test_own_promotion_link_evidence_does_not_use_group_description_for_other_members(self):
        run = self.make_run(continuous=True)
        reader = monitor.Monitor(run)
        reader.tasks = [run]
        reader.counts = {run.id: {"hits": 0}}
        ctx = {"icp": self.profile, "icp_id": self.icp_id, "words": {}, "types": []}
        own = {"id": 1, "text": "Casino professional promotion partnership: https://partner.example/signup?ref=QA_AGENT",
               "link": "https://t.me/qa_agent_group/1", "sender_name": "QA Business Agent", "sender_username": "qa_business_agent",
               "sender_id": 123, "date": time.time()}
        unrelated = {**own, "id": 2, "text": "Hello everyone, the weather is sunny today.",
                     "sender_username": "qa_other_member", "link": "https://t.me/qa_agent_group/2"}
        ordinary_signup = {**unrelated, "id": 3, "text": "Official casino registration https://casino.example/signup"}
        chat = {"chat_id": "-100123", "title": "Casino affiliate agent network", "about": own["text"]}
        with mock.patch.object(llm, "call_json", side_effect=AssertionError("special own-evidence path should not infer via AI")), \
                mock.patch.object(reader, "_channel", return_value=None):
            cands = reader.prefilter([(m, m["text"]) for m in (own, unrelated, ordinary_signup)], ctx)
            self.assertEqual([m["id"] for m in cands], [1])
            hits = reader.classify(pool.backend(), pool._acc(pool._row(self.aid)), pool._row(self.aid), chat, cands, ctx, run)
        self.assertEqual(hits, {1})
        raws = db.query("SELECT * FROM raw_leads WHERE task_id=?", (run.id,))
        self.assertEqual(len(raws), 1)
        evidence = db.loads(raws[0]["data"], {})["verified_evidence"]
        self.assertEqual(evidence["origin"], "tg_message")
        self.assertEqual(evidence["author"], "qa_business_agent")
        self.assertEqual(evidence["quote"], own["text"])
        self.assertEqual(evidence["url"], own["link"])
        candidate = db.loads(raws[0]["data"], {})
        forged = {**candidate, "verified_evidence": {**evidence, "author": "unrelated_author"},
                  "verified_text": False}
        self.assertEqual(analyst.Analyst._source_documents(raws[0], forged, []), [])
        analyst.Passthrough(run, self.profile).process(raws[0]["id"])
        lead = db.query_one("SELECT * FROM leads WHERE task_id=?", (run.id,))
        self.assertEqual(lead["qualified"], 1)
        self.assertEqual(db.loads(lead["profile"], {})["b2b_validation"]["status"], "suspected")
        self.assertEqual(db.query("SELECT * FROM outbox"), [])

    def test_low_score_advertising_source_uses_own_signal_but_general_threshold_remains(self):
        scout.set_settings({"icp_id": self.icp_id, "auto_queue": True, "confirmed": True})
        signal = edition.validate_b2b(self.profile, [{"url": "https://t.me/qa_ad_agents", "text": "Casino affiliate agent promotion team."}])
        for name, score, reason in (("qa_ad_agents", 0, db.dumps({"agent_signal": signal})),
                                    ("qa_unrelated", 99, db.dumps({"agent_signal": {"status": "unverified", "evidence": []}}))):
            db.execute("INSERT INTO tg_discoveries(icp_id,link,title,score,ad_prob,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                       (self.icp_id, "https://t.me/" + name, name, score, 1, reason, db.now()))
        self.assertEqual(joining.auto_queue_discoveries(), 1)
        self.assertEqual(joining.auto_queue_discoveries(), 0)
        self.assertEqual([q["link"] for q in joining.queue()], ["https://t.me/qa_ad_agents"])
        with mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "general"}):
            db.execute("UPDATE tg_discoveries SET status='suggested' WHERE link='https://t.me/qa_ad_agents'")
            self.assertEqual(joining.auto_queue_discoveries(), 1)  # Only unrelated score 99, ordinary quality gate.
            self.assertEqual(db.query_one("SELECT status FROM tg_discoveries WHERE link='https://t.me/qa_ad_agents'")["status"], "suggested")

    def test_ordinary_referral_without_professional_agent_context_is_unverified(self):
        result = edition.validate_b2b(self.profile, [{"url": "https://public.example/qa",
            "text": "Casino: my referral code FRIEND123 https://casino.example/signup?ref=FRIEND123"}])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["evidence"], [])

    def test_low_score_industry_group_is_source_only_and_cannot_qualify_ordinary_members(self):
        scout.set_settings({"icp_id": self.icp_id, "auto_queue": True, "confirmed": True})
        backend = pool.backend()
        groups = [{"username": "qa_casino_source", "title": "Casino public industry discussion"},
                  {"username": "qa_solar_source", "title": "Solar industry discussion"}]

        def preview(account, link, sample=20):
            casino = "casino" in link
            return {"title": groups[0 if casino else 1]["title"], "about": "Public casino industry news" if casino else "Public solar industry news",
                    "members": 20, "msgs_per_day": 20,
                    "samples": ["Neighbour member: I am an iGaming agent offering business partnerships."]}

        with mock.patch.object(backend, "search", return_value=groups), \
                mock.patch.object(backend, "preview", side_effect=preview), \
                mock.patch.object(joining.decision, "enabled", return_value=False), \
                mock.patch.object(llm, "call_json", return_value=({"groups": [
                    {"idx": 0, "relevance": 0.05, "ad_prob": 1, "reason": "QA advertising source"},
                    {"idx": 1, "relevance": 0.05, "ad_prob": 1, "reason": "QA other industry"}]}, {})):
            result = joining.discover(self.icp_id, auto_queue=False)
        self.assertEqual(result["added"], 2)
        casino = next(d for d in joining.discoveries() if d["link"].endswith("qa_casino_source"))
        self.assertEqual(casino["score"], 0)
        self.assertEqual(casino["source_signal"]["status"], "matched")
        self.assertEqual(casino["source_signal"]["kind"], "industry_source")
        self.assertEqual(casino["agent_signal"]["status"], "unverified", "member sample cannot define group's agency identity")
        self.assertEqual(joining.auto_queue_discoveries(), 1)
        self.assertEqual([row["link"] for row in joining.queue()], ["https://t.me/qa_casino_source"])
        run = self.make_run(continuous=True)
        reader = monitor.Monitor(run)
        texts = [{"text": "Official casino registration https://casino.example/signup", "link": "https://t.me/qa_casino_source/1"},
                 {"text": "Casino: my referral code FRIEND123 https://casino.example/signup?ref=FRIEND123", "link": "https://t.me/qa_casino_source/2"}]
        self.assertEqual(reader.prefilter([(m, m["text"]) for m in texts], {"icp": self.profile, "words": {}}), [])
        self.assertEqual(db.query("SELECT * FROM raw_leads WHERE task_id=?", (run.id,)), [])

    def test_private_invite_uses_normal_approval_queue_without_reading_or_retrying(self):
        scout.set_settings({"icp_id": self.icp_id, "auto_queue": True, "confirmed": True})
        self.assertEqual(joining.enqueue("https://t.me/+QA_PRIVATE_INVITE", icp_id=self.icp_id), 1)
        backend = pool.backend()
        with mock.patch.object(backend, "read_new", side_effect=AssertionError("cannot read unapproved private group")), \
                mock.patch.object(backend, "send", side_effect=AssertionError("no outreach")):
            self.assertEqual(joining.process_one(), {"needs_approval": "https://t.me/+QA_PRIVATE_INVITE"})
            settings.put("join_state", {"next_at": 0})
            self.assertIsNone(joining.process_one())
        row = joining.queue()[0]
        self.assertEqual(row["status"], "needs_approval")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["error"], "")
        self.assertEqual(pool.count(self.aid, "join"), 1)
        self.assertEqual(db.query("SELECT * FROM tg_chats"), [])

    def test_same_title_private_groups_report_only_joined_chat_identity_and_migrate_legacy_rows(self):
        title = "QA Same Private Group Title"
        decoy = db.execute("INSERT INTO tg_chats(account_id,chat_id,title,monitored,added_at) VALUES(?,?,?,?,?)",
                           (self.aid, "-100001", title, 1, db.now()))
        old = db.execute("INSERT INTO tg_join_queue(link,title,status,account_id,icp_id,created_at) VALUES(?,?,?,?,?,?)",
                         ("https://t.me/+QA_LEGACY_PRIVATE", title, "joined", self.aid, self.icp_id, db.now()))
        # Reopen a pre-migration database containing real business rows. The
        # added nullable association must preserve rows, not infer by title.
        db.execute("ALTER TABLE tg_join_queue DROP COLUMN tg_chat_id")
        db.close()
        self.assertIsNone(db.query_one("SELECT tg_chat_id FROM tg_join_queue WHERE id=?", (old,))["tg_chat_id"])
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["monitored"], 0)
        scout.set_settings({"icp_id": self.icp_id, "auto_queue": True, "confirmed": True})
        link = "https://t.me/+QA_JOINED_PRIVATE"
        self.assertEqual(joining.enqueue(link, icp_id=self.icp_id), 1)
        with mock.patch.object(pool.backend(), "join", return_value={"chat_id": "-100002", "title": title,
                "username": "", "kind": "group", "members": 10}):
            self.assertEqual(joining.process_one(), {"joined": link})
        actual = db.query_one("SELECT id FROM tg_chats WHERE account_id=? AND chat_id='-100002'", (self.aid,))["id"]
        queued = db.query_one("SELECT * FROM tg_join_queue WHERE link=?", (link,))
        self.assertEqual(queued["tg_chat_id"], actual)
        self.assertNotEqual(actual, decoy)
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["joined"], 2)
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["monitored"], 1)
        db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (decoy,))
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["monitored"], 1)
        db.execute("UPDATE tg_chats SET monitored=1 WHERE id=?", (decoy,))
        db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (actual,))
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["monitored"], 0)
        db.execute("UPDATE tg_chats SET monitored=1,left_at=? WHERE id=?", (db.now(), actual))
        self.assertEqual(scout._join_report(manager.day(), self.icp_id)["monitored"], 0)

    def test_continuous_monitor_survives_target_but_stops_at_day_end_or_cancel(self):
        regular, continuous = self.make_run(), self.make_run(continuous=True)
        regular.bump(qualified=1)
        continuous.bump(qualified=3)
        self.assertTrue(regular.should_stop())
        self.assertFalse(continuous.should_stop())
        self.assertEqual(tasks.tg_tasks(), [continuous])
        with mock.patch.object(tasks.time, "time", return_value=continuous.deadline + 1):
            self.assertTrue(continuous.should_stop())
            self.assertEqual(tasks.tg_tasks(), [])
        other = self.make_run(continuous=True)
        tasks.cancel(other.id)
        self.assertTrue(other.should_stop())

    def test_daily_collection_is_independent_of_met_manager_goal_and_durable(self):
        cfg = scout.set_settings({"enabled": True, "auto_monitor": True, "icp_id": self.icp_id})
        date = manager.day()
        db.execute("INSERT INTO manager_plans(day,goal,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                   (date, "{}", "met", db.now(), db.now()))
        with mock.patch.object(background, "spawn"):
            ident = scout.ensure_collection(date, cfg)
            self.assertTrue(ident)
            run = tasks._RUNS[ident]
            self.assertTrue(run.params["continuous_monitor"])
            run.bump(qualified=20)
            self.assertFalse(run.should_stop())
            self.assertEqual(scout.ensure_collection(date, cfg), ident)
            self.assertEqual(db.query_one("SELECT COUNT(*) n FROM tasks WHERE created_by='scout'")["n"], 1)
            tasks.mark_interrupted()
            tasks._RUNS.clear()
            resumed = scout.ensure_collection(date, cfg)
            self.assertNotEqual(resumed, ident)
            self.assertEqual(tasks._RUNS[resumed].params["resume_from"], ident)
            self.assertEqual(scout.ensure_collection(date, cfg), resumed)
            self.assertEqual(db.query_one("SELECT parent_id FROM tasks WHERE id=?", (resumed,))["parent_id"], ident)
            scout.set_settings({"auto_monitor": False})
            self.assertTrue(tasks._RUNS[resumed].cancel_flag.is_set())
            self.assertIsNone(scout.ensure_collection(date))
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (resumed,))
            tasks._RUNS[resumed].done = True
            scout.set_settings({"auto_monitor": True})
            restored = scout.ensure_collection(date)
            self.assertNotEqual(restored, resumed)
            tasks.cancel(restored)
            tasks._RUNS[restored].done = True
            db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (restored,))
            self.assertEqual(scout.ensure_collection(date), restored, "explicit manual stop must remain stopped")
            self.assertEqual(db.query_one("SELECT COUNT(*) n FROM tasks WHERE created_by='scout'")["n"], 3)


if __name__ == "__main__":
    unittest.main()

"""Daily opt-in source collection and encrypted platform sessions, without browsers/network."""
from contextlib import nullcontext
import os
import time
import unittest
from unittest import mock

import test_app as app_tests
from salesteam import background, db, edition, llm, server, settings, social, source_pool, tasks
from salesteam.agents import icp, manager, researcher
from salesteam.errors import UserError


class Node:
    def __init__(self, text, links):
        self.text, self.links = text, links

    def inner_text(self, **kwargs):
        return self.text

    def locator(self, selector):
        return self

    def evaluate_all(self, script):
        return self.links


class Nodes:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class BrowserFixture:
    def __init__(self, url, units=(), login_wall=False, storage=None, redirect=None):
        self.url, self.units = url, units
        self.login_wall, self.storage, self.redirect = login_wall, storage or {}, redirect
        self.context_states, self.routes, self.requests = [], [], []
        self.pages = [self]
        self.mouse = self
        self.closed = False

    def new_context(self, **kwargs):
        self.context_states.append(kwargs.get("storage_state"))
        return self

    def new_page(self):
        return self

    def route(self, pattern, handler):
        self.routes.append(handler)

    def goto(self, url, **kwargs):
        self.url = self.redirect or url
        return mock.Mock(status=200)

    def locator(self, selector):
        if selector == 'input[type="password"]':
            return Nodes([object()] if self.login_wall else [])
        return Nodes(self.units)

    def wheel(self, *args):
        pass

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "QA public source"

    def storage_state(self):
        return self.storage

    def close(self):
        self.closed = True


class SourcePoolTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"SALESTEAM_EDITION": "my_igaming"}))
        app_tests.AppTests.setUp(self)
        self.enterContext(mock.patch.object(social, "_sessions", {}))
        self.enterContext(mock.patch.object(social, "_session_versions", {}))
        self.profile = icp.normalize(edition.presets()[0]["data"])
        self.icp_id = db.execute("INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)",
                                 ("QA source pool", db.dumps(self.profile), db.now(), db.now()))
        for agent in ("researcher", "analyst", "manager"):
            llm.update_config(agent, {"provider": "mock", "model": "qa-mock"})

    def browser(self, fixture):
        self.enterContext(mock.patch.object(social, "_check_browser"))
        self.enterContext(mock.patch.object(social, "_launch", return_value=fixture))
        self.enterContext(mock.patch("playwright.sync_api.sync_playwright", side_effect=lambda: nullcontext(object())))

    def test_forum_entries_and_learned_words_keep_real_provenance(self):
        source_pool.set_settings({"icp_id": self.icp_id, "keywords": ["public partners"]})
        row = source_pool.add({"url": "https://forum.example/agent-thread", "kind": "forum"})
        self.assertEqual(row["access"], "public")
        self.assertEqual(source_pool.add({"url": row["url"], "kind": "forum"})["id"], row["id"])
        text = "We are a casino affiliate agency and offer iGaming agent partnerships."
        with mock.patch.object(llm, "call_json", side_effect=AssertionError("learn words only from actual source text")):
            learned = source_pool.learn_keywords(self.icp_id, [{"url": row["url"], "text": text}])
            self.assertTrue(learned)
            self.assertEqual(source_pool.learn_keywords(self.icp_id, [{"url": row["url"], "text": text}]), [])
            self.assertEqual(source_pool.learn_keywords(self.icp_id, [{"url": "https://unrelated.example", "text": "Weather and hobbies."}]), [])
        for value in learned:
            self.assertEqual(value["url"], row["url"])
            self.assertIn(value["quote"], text)
            self.assertIn(value["word"].lower(), value["quote"].lower())
        self.assertIn("public partners", source_pool.keywords_for(self.icp_id))
        discovered_url = "https://directory.example/public-agency-directory"
        added = source_pool.discover_page(self.icp_id, {"url": discovered_url, "title": "QA public directory", "markdown": text})
        self.assertTrue(added)
        persisted = source_pool.entry_for(discovered_url)
        self.assertEqual(persisted["origin"], "discovery")
        self.assertTrue(persisted["evidence"])
        self.assertEqual(persisted["evidence"][0]["url"], discovered_url)
        self.assertIn(persisted["evidence"][0]["quote"], text)
        self.assertIsNone(source_pool.discover_page(self.icp_id, {"url": "https://unrelated.example", "markdown": "Weather and hobbies."}))
        source_pool.update(row["id"], {"status": "retired"})
        self.assertNotIn(row["url"], source_pool.urls())
        self.assertEqual(source_pool.add({"url": row["url"], "kind": "forum"})["status"], "retired")

    def test_daily_finite_task_does_not_repeat_after_restart_and_pause_or_budget_blocks(self):
        ts = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))
        cfg = {"enabled": True, "icp_id": self.icp_id, "time_of_day": "09:00"}
        source_pool.set_settings(cfg)
        source_pool.add({"url": "https://forum.example/agents"})
        with mock.patch.object(background, "spawn"):
            tasks.set_paused(True)
            self.assertIsNone(source_pool.tick(ts))
            self.assertEqual(db.query("SELECT * FROM tasks"), [])
            tasks.set_paused(False)
            budget = llm.budget_state()
            budget.update(paused=True, period="day")
            with mock.patch.object(llm, "budget_state", return_value=budget):
                self.assertIsNone(source_pool.tick(ts))
                self.assertEqual(db.query("SELECT * FROM tasks"), [])
            ident = source_pool.tick(ts)
            self.assertTrue(ident)
            run = tasks._RUNS[ident]
            self.assertEqual(run.params["sources"], ["google", "web"])
            self.assertEqual(run.params["urls"], ["https://forum.example/agents"])
            self.assertEqual(run.target, 0, "finite source round must not stop at a candidate quota")
            self.assertFalse(run.params.get("continuous_monitor", False))
            self.assertIsNone(source_pool.tick(ts))
            tasks.mark_interrupted()
            tasks._RUNS.clear()
            self.assertIsNone(source_pool.tick(ts))
            self.assertTrue(source_pool.tick(ts + 86400))
            self.assertEqual(db.query_one("SELECT COUNT(*) n FROM tasks WHERE created_by='source_pool'")["n"], 2)

    def test_platform_login_filters_cross_platform_cookies_encrypts_and_logout_is_separate(self):
        state = {"cookies": [{"name": "fbqa", "value": "FB_QA_SESSION_SENTINEL", "domain": ".facebook.com"},
                             {"name": "igqa", "value": "IG_QA_SESSION_SENTINEL", "domain": ".instagram.com"},
                             {"name": "tracker", "value": "TRACKER_QA_SENTINEL", "domain": "tracker.example"}],
                 "origins": [{"origin": "https://www.facebook.com", "localStorage": []},
                             {"origin": "https://www.instagram.com", "localStorage": []}]}
        fixture = BrowserFixture("https://www.facebook.com/login/", storage=state)
        self.browser(fixture)
        social.open_login("facebook")
        end = time.monotonic() + 3
        while social.status("facebook")["status"] == "starting" and time.monotonic() < end:
            time.sleep(0.005)
        self.assertEqual(social.status("facebook")["status"], "open")
        self.assertEqual(social.finish_login("facebook")["status"], "done")
        saved = social.storage_state("facebook")
        self.assertEqual([c["value"] for c in saved["cookies"]], ["FB_QA_SESSION_SENTINEL"])
        self.assertEqual([o["origin"] for o in saved["origins"]], ["https://www.facebook.com"])
        self.assertEqual(social.storage_state("instagram"), {})
        encrypted = db.query_one("SELECT value_enc FROM secrets WHERE name='social_session:facebook'")["value_enc"]
        self.assertNotIn("FB_QA_SESSION_SENTINEL", encrypted)
        self.assertNotIn("SESSION_SENTINEL", db.dumps(social.status_all()))
        settings.set_secret("social_session:instagram", db.dumps({"cookies": [state["cookies"][1]]}))
        social.clear_session("facebook")
        self.assertFalse(social.status("facebook")["configured"])
        self.assertTrue(social.status("instagram")["configured"])
        self.assertEqual(social.storage_state("instagram")["cookies"][0]["value"], "IG_QA_SESSION_SENTINEL")

    def test_public_youtube_comments_use_no_login_key_and_preserve_individual_units(self):
        url = "https://www.youtube.com/watch?v=QA_PUBLIC"
        comment = Node("QA Professional Agent: my casino affiliate promotion https://partner.example/?ref=QA",
                       [{"url": url + "&lc=QA_COMMENT", "text": "comment"}])
        fixture = BrowserFixture(url, units=[comment])
        self.browser(fixture)
        with mock.patch.object(social, "storage_state", side_effect=AssertionError("public crawl must not request session")), \
                mock.patch.object(settings, "get_secret", side_effect=AssertionError("public comments require no API key")):
            result = social.read(url)
        self.assertEqual(fixture.context_states, [None])
        self.assertEqual(result["platform"], "youtube")
        self.assertEqual(result["units"][0]["url"], url + "&lc=QA_COMMENT")
        self.assertEqual(result["units"][0]["text"], comment.text)
        self.assertTrue(fixture.closed)
        # Exercise the actual no-key source dispatch and per-comment extraction,
        # rather than merely calling the social browser helper directly.
        source_pool.set_settings({"icp_id": self.icp_id})
        with mock.patch.object(background, "spawn"):
            task = tasks.create_task({"icp_id": self.icp_id, "sources": ["youtube"], "target": 10, "urls": [url]})
        run = tasks._RUNS[task["id"]]
        worker = researcher.Researcher(run, self.profile)
        worker.mock = False
        candidate = {"company_name": "QA Professional Agent", "evidence_quote": comment.text,
                     "source_url": url + "&lc=QA_COMMENT", "country": "", "telegram": [], "emails": []}
        with mock.patch.object(researcher.yt, "collect", side_effect=AssertionError("no paid API path")), \
                mock.patch.object(llm, "call_json", return_value=({"candidates": [candidate]}, {})) as extraction:
            worker.source_youtube()
        extraction.assert_called_once()
        self.assertEqual(extraction.call_args.kwargs["context"]["text"], comment.text)
        raw = db.query_one("SELECT * FROM raw_leads WHERE task_id=?", (run.id,))
        self.assertEqual(raw["source"], "youtube")
        self.assertEqual(raw["source_url"], url + "&lc=QA_COMMENT")
        self.assertEqual(raw["evidence"], comment.text)
        run.done = True

    def test_expired_login_empty_content_and_navigation_redirects_are_explicit(self):
        settings.set_secret("social_session:facebook", db.dumps({"cookies": [{"name": "qa", "value": "qa", "domain": ".facebook.com"}]}))
        fixture = BrowserFixture("https://www.facebook.com/groups/QA", login_wall=True)
        self.browser(fixture)
        with self.assertRaises(UserError) as caught:
            social.read(fixture.url, access="login")
        self.assertEqual(caught.exception.key, "social_login_required")
        # Test the actual registered browser route: external navigation is
        # aborted, and platform hosts with deceptive suffixes stay external.
        self.assertTrue(fixture.routes)
        for url, expected in (("https://www.instagram.com/qa", "abort"),
                              ("https://facebook.com.attacker.example/", "abort"),
                              ("https://www.facebook.com/groups/QA", "continue_")):
            request = mock.Mock(url=url)
            request.is_navigation_request.return_value = True
            route = mock.Mock(request=request)
            fixture.routes[0](route)
            getattr(route, expected).assert_called_once()
            getattr(route, "continue_" if expected == "abort" else "abort").assert_not_called()
        fixture.login_wall = False
        with self.assertRaises(UserError) as caught:
            social.read("https://www.facebook.com/groups/QA", access="login")
        self.assertEqual(caught.exception.key, "social_content_unavailable")
        with self.assertRaises(UserError) as caught:
            social.read("https://www.instagram.com/qa", access="login")
        self.assertEqual(caught.exception.key, "social_login_required")

    def test_reset_clears_business_sources_and_everything_clears_social_sessions(self):
        source_pool.set_settings({"enabled": True, "icp_id": self.icp_id, "keywords": ["agent partners"]})
        source_pool.add({"url": "https://forum.example/agents"})
        settings.put("source_pool_report", {"day": manager.day(), "task_id": "old"})
        settings.put("scout_collection_state", {"task_id": "old"})
        settings.set_secret("social_session:facebook", db.dumps({"cookies": [{"value": "QA_ACCOUNT"}]}))
        social._sessions["facebook"] = {"status": "done"}
        self.assertTrue(server.delete_all({"confirm": "DELETE", "scope": "business"})["ok"])
        for key in ("source_pool_settings", "source_pool_entries", "source_pool_report", "scout_collection_state"):
            self.assertIsNone(db.query_one("SELECT * FROM settings WHERE key=?", (key,)), key)
        self.assertEqual(source_pool.items(), [])
        self.assertFalse(source_pool.config()["enabled"])
        self.assertTrue(social.status("facebook")["configured"])
        self.assertTrue(server.delete_all({"confirm": "DELETE", "scope": "everything"})["ok"])
        self.assertFalse(social.status("facebook")["configured"])
        self.assertEqual(social._sessions, {})

    def test_hard_crawler_skips_do_not_fall_back_to_social_browser_and_unreadable_sources_fail(self):
        from salesteam.sources import crawler
        url = "https://www.facebook.com/groups/QA_PUBLIC"
        with mock.patch.object(background, "spawn"):
            task = tasks.create_task({"icp_id": self.icp_id, "sources": ["web"], "target": 10, "urls": [url]})
        run = tasks._RUNS[task["id"]]
        for key in ("ev.robots_blocked", "ev.crawl_blocked", "ev.crawl_daily_limit"):
            with self.subTest(key=key):
                worker = researcher.Researcher(run, self.profile)
                with mock.patch.object(worker.crawler, "fetch", side_effect=crawler.Skip(key)), \
                        mock.patch.object(social, "read", side_effect=AssertionError("hard skip must not bypass crawler")), \
                        mock.patch.object(worker.crawler, "reserve_rendered", side_effect=AssertionError("hard skip must not reserve browser")):
                    with self.assertRaises((UserError, crawler.Skip)) as caught:
                        worker.source_web()
                self.assertIn(getattr(caught.exception, "key", ""), (key, "source_read_failed"))
                self.assertTrue(worker.fetch_failures)
                self.assertEqual(worker.fetch_failures[0].key, "source_read_failed")
        run.done = True

    def test_logout_during_authenticated_read_stops_before_exporting_stale_session_content(self):
        url = "https://www.facebook.com/groups/QA"
        settings.set_secret("social_session:facebook", db.dumps({"cookies": [{"name": "qa", "value": "QA_AUTH", "domain": ".facebook.com"}]}))
        fixture = BrowserFixture(url, units=[Node("QA private account content", [])])
        self.browser(fixture)
        with mock.patch.object(fixture, "wait_for_timeout", side_effect=lambda ms: social.clear_session("facebook")):
            with self.assertRaises(UserError) as caught:
                social.read(url, access="login")
        self.assertEqual(caught.exception.key, "social_login_required")
        self.assertFalse(social.status("facebook")["configured"])
        self.assertTrue(fixture.closed)


if __name__ == "__main__":
    unittest.main()

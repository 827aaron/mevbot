"""Parsed public fixtures and durable reads; one opt-in static DOM JS test, no real account."""
import copy
import json
import os
from pathlib import Path
from html import escape
import unittest
from unittest import mock

from scrapling.parser import Selector

import test_source_pool as pool_tests
from salesteam import background, db, llm, server, settings, social, source_pool, tasks
from salesteam.agents import analyst, researcher
from salesteam.errors import UserError
from salesteam.sources import crawler


class SourceIncrementalTests(unittest.TestCase):
    def setUp(self):
        pool_tests.SourcePoolTests.setUp(self)
        source_pool.set_settings({"icp_id": self.icp_id})

    def browser(self, fixture):
        pool_tests.SourcePoolTests.browser(self, fixture)

    def worker(self, source="web", urls=()):
        with mock.patch.object(background, "spawn"):
            task = tasks.create_task({"icp_id": self.icp_id, "sources": [source], "target": 10, "urls": list(urls)})
        run = tasks._RUNS[task["id"]]
        return researcher.Researcher(run, self.profile)

    def public_page(self, url="https://forum.example/thread", text="QA Agency is an iGaming affiliate agency.", links=()):
        html = '<html><body><p>' + escape(text) + '</p>'
        html += ''.join('<a href="' + escape(href, quote=True) + '">' + escape(label) + '</a>' for href, label in links)
        html += '</body></html>'
        return crawler.Crawler(count_daily=False)._to_page(Selector(html, url=url), url, md=text)

    def unit_page(self):
        text = "QA Agency is an iGaming affiliate agency. We offer public business partnerships."
        unit = {"id": "qa-post-1", "author": "QA Agency", "url": "https://x.com/qaagency/status/123", "text": text,
                "links": [], "platform": "x"}
        return {"url": "https://x.com/qaagency", "title": "QA public posts", "markdown": text,
                "units": [unit], "links": [], "emails": []}

    @unittest.skipUnless(os.environ.get("SALESTEAM_TEST_STATIC_DOM") == "1", "static DOM JS verified once in targeted suite")
    def test_real_x_reddit_dom_preserves_authors_permalinks_and_removes_nested_quotes(self):
        from playwright.sync_api import sync_playwright
        requested = []
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"])
            try:
                context = browser.new_context()
                context.route("**/*", lambda route: (requested.append(route.request.url), route.abort()))
                page = context.new_page()
                page.set_content('''<article data-testid="tweet"><div data-testid="User-Name"><a href="https://twitter.com/qaagent">QA Agent</a></div>
                    <a href="https://twitter.com/qaagent/status/123?s=20"><time>now</time></a>
                    <div data-testid="tweetText">Own casino affiliate partnership.
                      <a href="https://own.example/forum">Our business forum</a>
                      <div data-testid="quoteTweet"><div data-testid="tweetText">QUOTED_NEIGHBOUR_SENTINEL<a href="https://neighbour.example/">neighbour</a></div></div>
                    </div></article>
                    <article data-testid="tweet"><div data-testid="User-Name"><a href="https://x.com/second">QA Second</a></div>
                      <a href="https://x.com/second/status/456"><time>later</time></a><div data-testid="tweetText">Separate public post.</div></article>
                    <article data-testid="tweet"><div data-testid="User-Name"><a href="https://x.com/qaagent">QA Agent</a></div>
                      <a href="https://x.com/qaagent/status/123?utm_source=qa"><time>duplicate</time></a><div data-testid="tweetText">Own casino affiliate partnership.</div></article>
                    <article data-testid="tweet"><div data-testid="tweetText">Missing author and permalink must be skipped.</div></article>''')
                tweets = social.extract_units(page, "x")
                self.assertEqual(len(tweets), 2)
                self.assertEqual(tweets[0]["author"], "QA Agent")
                self.assertEqual(tweets[0]["url"], "https://x.com/qaagent/status/123")
                self.assertNotIn("QUOTED_NEIGHBOUR_SENTINEL", tweets[0]["text"])
                self.assertNotIn("https://neighbour.example/", [link["url"] for link in tweets[0]["links"]])
                self.assertIn("Own casino affiliate partnership", tweets[0]["text"])
                self.assertEqual(tweets[1]["author"], "QA Second")
                page.set_content('''<shreddit-post author="QA Post Author" post-id="t3_qa" permalink="https://old.reddit.com/r/qa/comments/abc/topic/">
                  <div slot="text-body">Own casino affiliate partnership.
                    <blockquote>QUOTED_REDDIT_SENTINEL<a href="https://quoted.example/">quoted author link</a></blockquote>
                    <shreddit-comment author="QA Comment Author" comment-id="t1_qa" permalink="https://www.reddit.com/r/qa/comments/abc/topic/def/">
                      <div slot="comment">COMMENT_ONLY_SENTINEL<a href="https://comment.example/thread">comment forum</a></div>
                    </shreddit-comment>
                  </div></shreddit-post>''')
                reddit = social.extract_units(page, "reddit")
                self.assertEqual(len(reddit), 2)
                post = next(u for u in reddit if u["id"] == "t3_qa")
                comment = next(u for u in reddit if u["id"] == "t1_qa")
                self.assertEqual(post["author"], "QA Post Author")
                self.assertNotIn("COMMENT_ONLY_SENTINEL", post["text"])
                self.assertNotIn("QUOTED_REDDIT_SENTINEL", post["text"])
                self.assertEqual(post["links"], [])
                self.assertEqual(comment["author"], "QA Comment Author")
                self.assertIn("COMMENT_ONLY_SENTINEL", comment["text"])
                self.assertEqual(comment["url"], "https://reddit.com/r/qa/comments/abc/topic/def")
                self.assertEqual(social.platform_for("https://twitter.com/qaagent"), "x")
                self.assertEqual(context.cookies(), [])
            finally:
                browser.close()
        self.assertEqual(requested, [])
        Path("/workspace/project-review/JamesAgent-source-incremental-dom.json").write_text(
            json.dumps({"engine": "Chromium actual JS page.evaluate", "x_authors": [t["author"] for t in tweets],
                        "reddit_authors": [r["author"] for r in reddit], "external_requests": requested,
                        "accounts": 0, "login": False}), encoding="utf-8")

    def test_real_scrapling_forum_posts_exclude_quoted_other_author_text_and_links(self):
        url = "https://forum.example/thread"
        html = '''<article class="message" id="post-1"><a class="username">QA Agency</a><div class="message-body">
          We are an iGaming affiliate agency.<a href="/agency-forum">our forum</a>
          <blockquote>NEIGHBOUR_QUOTE_SENTINEL<a href="https://neighbour.example/thread">neighbour forum</a></blockquote>
          </div></article><article class="message" id="post-2"><a class="username">QA Other</a>
          <div class="message-body">OTHER_POST_SENTINEL</div></article>'''
        page = crawler.Crawler(count_daily=False)._to_page(Selector(html, url=url), url, md="Public forum discussion")
        self.assertEqual(len(page["units"]), 2)
        first, second = page["units"]
        self.assertEqual(first["author"], "QA Agency")
        self.assertNotIn("NEIGHBOUR_QUOTE_SENTINEL", first["text"])
        self.assertNotIn("OTHER_POST_SENTINEL", first["text"])
        self.assertEqual([l["url"] for l in first["links"]], ["https://forum.example/agency-forum"])
        self.assertEqual(second["author"], "QA Other")
        self.assertEqual(first["url"], url + "#post-1")

    def test_linked_sources_keep_parent_proof_are_bounded_and_tg_remains_candidate(self):
        retired = source_pool.add({"url": "https://retired.example/forum"})
        source_pool.update(retired["id"], {"status": "retired"})
        page = self.public_page(links=[("https://retired.example/forum", "retired"),
            ("https://partner.example/signup?ref=QA", "Registration invitation"),
            ("/next-thread", "Public partner forum"), ("/next-thread", "Duplicate"),
            ("https://t.me/qa_linked_group", "Public TG source"), ("https://forum.example/thread", "Cycle")])
        context = {"depth": 0, "max_depth": 2, "expanded": 0, "max_expanded": 10}
        source_pool.discover_page(self.icp_id, page, context)
        child = source_pool.entry_for("https://forum.example/next-thread")
        self.assertEqual(child["parent_url"], page["url"])
        self.assertEqual(child["link_text"], "Public partner forum")
        self.assertEqual(child["depth"], 1)
        self.assertIn(child["evidence"][0]["quote"], page["markdown"])
        self.assertEqual(child["evidence"][0]["url"], page["url"])
        self.assertIsNone(source_pool.entry_for("https://partner.example/signup?ref=QA"))
        self.assertEqual(source_pool.entry_for(retired["url"])["status"], "retired")
        discovery = db.query_one("SELECT * FROM tg_discoveries WHERE link='https://t.me/qa_linked_group'")
        self.assertEqual(discovery["status"], "suggested")
        self.assertEqual(db.loads(discovery["reason"], {})["parent_url"], page["url"])
        self.assertEqual(db.query("SELECT * FROM tg_join_queue"), [])
        self.assertEqual(db.query("SELECT * FROM tg_chats"), [])
        count = len(source_pool.items())
        source_pool.discover_page(self.icp_id, page, context)
        self.assertEqual(len(source_pool.items()), count)
        self.assertEqual(context["expanded"], 2)
        source_pool.discover_page(self.icp_id, self.public_page(url=child["url"], links=[(page["url"], "Back to parent")]), context)
        self.assertEqual(len(source_pool.items()), count)
        capped = {"depth": 2, "max_depth": 2, "expanded": 0, "max_expanded": 10}
        source_pool.discover_page(self.icp_id, self.public_page(links=[("https://third.example/forum", "deeper source")]), capped)
        self.assertIsNone(source_pool.entry_for("https://third.example/forum"))
        many = self.public_page(links=[(f"https://links.example/forum/{i}", f"public forum {i}") for i in range(20)])
        budget = {"depth": 0, "max_depth": 99, "expanded": 0, "max_expanded": 99}
        source_pool.discover_page(self.icp_id, many, budget)
        self.assertEqual(budget["expanded"], 10)

    def test_private_parent_links_do_not_inherit_session_or_login_access(self):
        parent = source_pool.add({"url": "https://x.com/qaagency", "access": "login"})
        state = {"cookies": [{"name": "qa", "value": "QA_X_COOKIE", "domain": ".x.com"}]}
        settings.set_secret("social_session:x", db.dumps(state))
        page = self.public_page(url=parent["url"], links=[("https://www.reddit.com/r/qa", "public next source")])
        page["access"] = "login"
        source_pool.discover_page(self.icp_id, page)
        child = source_pool.entry_for("https://reddit.com/r/qa")
        self.assertEqual(child["access"], "public")
        self.assertEqual(child["parent_url"], parent["url"])
        self.assertEqual(social.storage_state("reddit"), {})
        fixture = pool_tests.BrowserFixture(parent["url"], units=[pool_tests.Node("We are an iGaming affiliate agency.", [])])
        self.browser(fixture)
        # This fixture only exercises request/session isolation; actual X DOM
        # extraction is independently executed by the static Chromium test.
        with mock.patch.object(social, "extract_units", return_value=self.unit_page()["units"]):
            social.read(parent["url"], access="login")
        self.assertEqual(fixture.context_states, [state])
        route = mock.Mock(request=mock.Mock(url=child["url"]))
        route.request.is_navigation_request.return_value = True
        fixture.routes[0](route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()

    def test_successive_round_roots_extend_beyond_historical_depth_with_real_lineage_and_round_caps(self):
        urls = ["https://rounds.example/forum/" + letter for letter in "abcd"]
        first = self.worker()
        context = first.discovery_context
        self.assertTrue(context["round_roots"])
        context["depth"] = 0
        source_pool.discover_page(self.icp_id, self.public_page(url=urls[0], links=[(urls[1], "B forum")]), context)
        context["depth"] = 1
        source_pool.discover_page(self.icp_id, self.public_page(url=urls[1], links=[(urls[2], "C forum")]), context)
        context["depth"] = 2
        source_pool.discover_page(self.icp_id, self.public_page(url=urls[2], links=[(urls[3], "D forum")]), context)
        self.assertIsNone(source_pool.entry_for(urls[3]), "same round cannot follow a third hop")
        self.assertEqual(context["expanded"], 2)
        historical = source_pool.entry_for(urls[2])
        self.assertEqual(historical["depth"], 2)
        self.assertEqual(historical["lineage_depth"], 2)
        # Keep legacy no-context depth semantics. A new Researcher round
        # explicitly opts into continuing from its previously found sources.
        source_pool.discover_page(self.icp_id, self.public_page(url=urls[2], links=[(urls[3], "D forum")]))
        self.assertIsNone(source_pool.entry_for(urls[3]))
        second = self.worker()
        next_context = second.discovery_context
        page = self.public_page(url=urls[2], links=[(urls[3], "D forum"), (urls[0], "Back to A"), (urls[1], "Back to B")])
        source_pool.discover_page(self.icp_id, page, next_context)
        continued = source_pool.entry_for(urls[3])
        self.assertEqual(continued["parent_url"], urls[2])
        self.assertEqual(continued["depth"], 1)
        self.assertEqual(continued["lineage_depth"], 3)
        self.assertEqual(continued["evidence"][0]["url"], urls[2])
        self.assertIn(continued["evidence"][0]["quote"], page["markdown"])
        self.assertEqual(next_context["expanded"], 1, "cycles must not consume new-source allowance")
        source_pool.discover_page(self.icp_id, page, next_context)
        self.assertEqual(next_context["expanded"], 1)
        next_context["depth"] = 1
        many = self.public_page(url=urls[3], links=[(f"https://branches.example/forum/{i}", f"public forum {i}") for i in range(20)])
        source_pool.discover_page(self.icp_id, many, next_context)
        self.assertEqual(next_context["expanded"], 10)
        branches = [item for item in source_pool.items() if item.get("parent_url") == urls[3]]
        self.assertEqual(len(branches), 9)
        self.assertTrue(all(item["depth"] == 2 and item["lineage_depth"] == 4 for item in branches))
        self.assertTrue(all(item["evidence"][0]["url"] == urls[3] for item in branches))
        first.run.done = second.run.done = True

    def test_checkpoint_is_ack_only_hash_persistent_and_isolated_by_icp_and_manual_features(self):
        page = self.unit_page()
        pending = source_pool.pending_units(self.icp_id, page)
        self.assertEqual(len(pending), 1)
        duplicate = {**page, "units": page["units"] + copy.deepcopy(page["units"])}
        self.assertEqual(len(source_pool.pending_units(self.icp_id, duplicate)), 1)
        self.assertEqual(len(source_pool.pending_units(self.icp_id, page)), 1, "unacknowledged content must retry")
        source_pool.ack_unit(self.icp_id, pending[0])
        saved = settings.get("source_pool_checkpoints")
        self.assertNotIn("QA Agency", db.dumps(saved))
        self.assertNotIn(page["url"], db.dumps(saved))
        db.close()
        self.assertEqual(source_pool.pending_units(self.icp_id, page), [])
        changed = copy.deepcopy(page)
        changed["units"][0]["text"] += " New public business partnership details."
        dirty = source_pool.pending_units(self.icp_id, changed)
        self.assertTrue(dirty[0]["checkpoint_changed"])
        links_changed = copy.deepcopy(page)
        links_changed["units"][0]["links"] = [{"url": "https://next.example/forum", "text": "New public forum"}]
        self.assertTrue(source_pool.pending_units(self.icp_id, links_changed)[0]["checkpoint_changed"])
        second_id = db.execute("INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)",
                               ("QA second profile", db.dumps(self.profile), db.now(), db.now()))
        self.assertEqual(len(source_pool.pending_units(second_id, page)), 1)
        self.assertFalse(source_pool.ack_unit(second_id, pending[0]))
        source_pool.learn_keywords(self.icp_id, [{"url": page["url"], "text": page["markdown"]}])
        self.assertEqual(source_pool.pending_units(self.icp_id, page), [], "automatic learned words must not invalidate completed work")
        source_pool.set_settings({"keywords": ["professional partnerships"]})
        self.assertFalse(source_pool.ack_unit(self.icp_id, pending[0]), "feature edit during work must not commit stale namespace")
        self.assertEqual(len(source_pool.pending_units(self.icp_id, page)), 1)
        source_pool.ack_unit(self.icp_id, source_pool.pending_units(self.icp_id, page)[0])
        profile = {**self.profile, "product": "Customer edited business offer"}
        db.execute("UPDATE icp_templates SET data=? WHERE id=?", (db.dumps(profile), self.icp_id))
        self.assertEqual(len(source_pool.pending_units(self.icp_id, page)), 1)

    def test_real_extraction_skips_unchanged_and_retries_llm_or_fetch_failure_without_ack(self):
        page = self.unit_page()
        worker = self.worker()
        with mock.patch.object(llm, "call_json", side_effect=UserError("qa_model_unavailable")):
            with self.assertRaises(UserError):
                worker.extract_page("web", page)
        self.assertEqual(len(source_pool.pending_units(self.icp_id, page)), 1)
        self.assertFalse(settings.get("source_pool_checkpoints", {}))
        source_pool.add({"url": "https://failed.example/forum"})
        failed_worker = self.worker(urls=["https://failed.example/forum"])
        with mock.patch.object(source_pool, "urls", return_value=["https://failed.example/forum"]), \
                mock.patch.object(failed_worker.crawler, "fetch", side_effect=UserError("qa_fetch_unavailable")):
            with self.assertRaises(UserError):
                failed_worker.source_web()
        self.assertFalse(settings.get("source_pool_checkpoints", {}))
        with mock.patch.object(llm, "call_json", return_value=({"candidates": []}, {})) as extraction:
            self.assertEqual(worker.extract_page("web", page), 0)
            extraction.assert_called_once()
        with mock.patch.object(llm, "call_json", side_effect=AssertionError("unchanged must not call LLM")):
            self.assertEqual(worker.extract_page("web", page), 0)
            restarted = self.worker()
            self.assertEqual(restarted.extract_page("web", page), 0)
        changed = copy.deepcopy(page)
        changed["units"][0]["text"] += " Newly published agent partnership terms."
        new = {**page["units"][0], "id": "qa-post-2", "url": "https://x.com/qaagency/status/456"}
        changed["units"].append(new)
        with mock.patch.object(llm, "call_json", return_value=({"candidates": []}, {})) as extraction:
            worker.extract_page("web", changed)
            self.assertEqual(extraction.call_count, 2)
        self.assertEqual(source_pool.pending_units(self.icp_id, changed), [])

    def test_source_reset_clears_checkpoint_namespace(self):
        page = self.unit_page()
        source_pool.ack_unit(self.icp_id, source_pool.pending_units(self.icp_id, page)[0])
        self.assertTrue(settings.get("source_pool_checkpoints"))
        server.delete_all({"confirm": "DELETE", "scope": "business"})
        self.assertIsNone(db.query_one("SELECT * FROM settings WHERE key='source_pool_checkpoints'"))

    def test_actual_post_author_binds_own_quote_without_name_in_body_and_cannot_transfer_to_other_author(self):
        page = self.unit_page()
        own_text = "We are an iGaming affiliate agency offering public business partnerships."
        page["units"][0]["text"] = own_text
        page["markdown"] = own_text
        worker = self.worker()
        candidates = [{"company_name": name, "evidence_quote": own_text, "source_url": page["units"][0]["url"],
                       "website": "", "emails": [], "telegram": [], "whatsapp": [], "country": ""}
                      for name in ("QA Agency", "QA Other Author")]
        with mock.patch.object(llm, "call_json", return_value=({"candidates": candidates}, {})):
            self.assertEqual(worker.extract_page("web", page), 2)
        rows = db.query("SELECT * FROM raw_leads WHERE task_id=? ORDER BY id", (worker.run.id,))
        own, other = (db.loads(row["data"], {}) for row in rows)
        self.assertEqual(own["verified_evidence"]["origin"], "public_post")
        self.assertEqual(own["verified_evidence"]["author"], "QA Agency")
        self.assertNotIn("QA Agency", own_text)
        self.assertTrue(analyst.Analyst._source_documents(rows[0], own, []))
        self.assertEqual(analyst.Analyst._source_documents(rows[1], other, []), [])
        processor = analyst.Passthrough(worker.run, self.profile)
        processor.process(rows[0]["id"])
        processor.process(rows[1]["id"])
        self.assertEqual(db.query_one("SELECT status FROM raw_leads WHERE id=?", (rows[0]["id"],))["status"], "qualified")
        self.assertEqual(db.query_one("SELECT reason FROM raw_leads WHERE id=?", (rows[1]["id"],))["reason"], "b2b_unverified")
        self.assertEqual(db.query_one("SELECT COUNT(*) n FROM leads WHERE task_id=?", (worker.run.id,))["n"], 1)
        worker.run.done = True


if __name__ == "__main__":
    unittest.main()

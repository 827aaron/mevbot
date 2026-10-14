"""Deterministic free-search regressions with real Scrapling HTML parsing."""
import base64
from html import escape
import unittest
from unittest import mock
from urllib.parse import quote

from scrapling.parser import Selector

import test_app as app_tests
from salesteam import db, settings, tasks
from salesteam.errors import UserError
from salesteam.sources import crawler as crawl, search


class SearchPipelineTests(unittest.TestCase):
    setUp = app_tests.AppTests.setUp

    def page(self, engine, links=(), markdown="Public search results"):
        url = search._endpoint_url(engine, "public enterprise partner", "MY", "en")
        html = '<html><head><title>Search fixture</title></head><body>'
        html += ''.join(f'<a href="{escape(href, quote=True)}">{escape(title)}</a>' for href, title in links)
        html += '</body></html>'
        # Real Scrapling parser and real crawler page conversion, no transport
        # replacement that would bypass production link resolution.
        response = Selector(html, url=url)
        return crawl.Crawler(count_daily=False)._to_page(response, url, md=markdown)

    def crawler(self, should_stop=None):
        return crawl.Crawler(count_daily=False, should_stop=should_stop)

    def test_ddg_direct_and_absolute_relative_protocol_relative_redirect_results(self):
        targets = ["https://partner.example/company", "https://second.example/team", "https://third.example/business"]
        links = [(targets[0], "Direct public company"),
                 ("https://duckduckgo.com/l/?uddg=" + quote(targets[1], safe=""), "Absolute redirect"),
                 ("/l/?uddg=" + quote(targets[2], safe=""), "Relative redirect"),
                 ("//duckduckgo.com/l/?uddg=" + quote(targets[0], safe=""), "Duplicate protocol-relative redirect")]
        for engine in ("ddg_lite", "ddg_html"):
            with self.subTest(engine=engine):
                results = search._parse(engine, self.page(engine, links), 8)
                self.assertEqual([r["url"] for r in results], targets)
                self.assertEqual(results[0]["title"], "Direct public company")
                self.assertEqual(len(search._parse(engine, self.page(engine, links), 2)), 2)

    def test_bing_tracking_redirects_decode_urlsafe_base64_and_deduplicate_targets(self):
        targets = ["https://company.example/partners?market=MY", "https://second.example/company"]
        encoded = ["a1" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=") for url in targets]
        links = [("/ck/a?!&&p=qa&u=" + encoded[0] + "&ntb=1", "First company"),
                 ("https://www.bing.com/ck/a?u=" + encoded[1], "Second company"),
                 (targets[0], "Already decoded duplicate"),
                 ("//www.bing.com/ck/a?u=" + encoded[0], "Protocol-relative duplicate")]
        results = search._parse("bing", self.page("bing", links), 8)
        self.assertEqual([r["url"] for r in results], targets)

    def test_search_engine_ads_invalid_urls_and_fragments_never_become_results(self):
        legitimate = "https://public-partner.example/enterprise"
        links = [("https://duckduckgo.com/about", "Engine navigation"),
                 ("https://www.bing.com/search?q=qa", "Engine navigation"),
                 ("https://www.microsoft.com/", "Engine own site"),
                 ("https://duckduckgo.com/y.js?ad_provider=qa&uddg=" + quote(legitimate, safe=""), "Ad click"),
                 ("https://www.bing.com/aclick?u=" + quote(legitimate, safe=""), "Bing ad"),
                 ("https://googleads.g.doubleclick.net/aclk?adurl=" + quote(legitimate, safe=""), "Ad tracker"),
                 ("/l/?uddg=javascript%3Aalert%281%29", "Non-HTTP redirect"),
                 ("/l/?uddg=https%3A%2F%2F", "Missing hostname"),
                 ("/l/?uddg=https%3A%2F%2F%5Binvalid", "Malformed URL"),
                 ("mailto:private@example.com", "Email"),
                 ("javascript:alert(1)", "Script"),
                 ("#top", "Fragment"),
                 (legitimate, "Real partner")]
        for engine in ("ddg_lite", "ddg_html", "bing"):
            with self.subTest(engine=engine):
                self.assertEqual([r["url"] for r in search._parse(engine, self.page(engine, links), 8)], [legitimate])

    def test_first_readable_empty_engine_falls_through_to_next_engine(self):
        pages = {"ddg_lite": self.page("ddg_lite"),
                 "ddg_html": self.page("ddg_html", [("https://partner.example/", "Public partner")])}
        called = []
        def fetch(name, url, crawler):
            called.append(name)
            return pages[name]
        with mock.patch.object(search, "_endpoint_fetch", side_effect=fetch), \
                mock.patch.object(search, "_stealth_fetch") as stealth:
            results = search.free("public enterprise partner", crawler=self.crawler())
        self.assertEqual(results[0]["url"], "https://partner.example/")
        self.assertEqual(called, ["ddg_lite", "ddg_html"])
        stealth.assert_not_called()

    def test_all_readable_empty_results_are_true_empty_and_do_not_start_browser(self):
        called = []
        def fetch(name, url, crawler):
            called.append(name)
            return self.page(name)
        with mock.patch.object(search, "_endpoint_fetch", side_effect=fetch), \
                mock.patch.object(search, "_stealth_fetch") as stealth:
            self.assertEqual(search.free("public enterprise partner", crawler=self.crawler()), [])
        self.assertEqual(called, list(search.FREE_ENDPOINTS))
        stealth.assert_not_called()

    def test_all_challenges_and_network_failures_are_distinct_explicit_errors(self):
        for error in ("challenge", "network"):
            with self.subTest(error=error):
                def fetch(name, url, crawler):
                    if error == "network":
                        raise crawl.Skip("ev.crawl_failed", reason="connection refused", url=url)
                    return self.page(name, markdown="Bots use DuckDuckGo complete the following challenge")
                with mock.patch.object(search, "_endpoint_fetch", side_effect=fetch), \
                        mock.patch.object(search, "_stealth_fetch", return_value=None):
                    with self.assertRaises(UserError) as rejected:
                        search.free("public enterprise partner", crawler=self.crawler())
                self.assertEqual(rejected.exception.key, "search_blocked" if error == "challenge" else "search_failed")

    def test_readable_empty_is_not_reported_as_blocked_after_other_engine_failures(self):
        def fetch(name, url, crawler):
            if name == "ddg_lite":
                return self.page(name)
            raise crawl.Skip("ev.crawl_failed", reason="network connection refused", url=url)
        with mock.patch.object(search, "_endpoint_fetch", side_effect=fetch), \
                mock.patch.object(search, "_stealth_fetch", return_value=None):
            self.assertEqual(search.free("no public enterprise match", crawler=self.crawler()), [])

    def test_cancelled_search_does_not_fetch_or_launch_browser(self):
        with mock.patch.object(search, "_endpoint_fetch") as fetch, \
                mock.patch.object(search, "_stealth_fetch") as stealth:
            with self.assertRaises((UserError, crawl.Skip)) as rejected:
                search.free("public enterprise partner", crawler=self.crawler(should_stop=lambda: True))
        fetch.assert_not_called()
        stealth.assert_not_called()
        self.assertIn("cancel", (str(rejected.exception) + str(getattr(rejected.exception, "params", {}))).lower())

    def test_hard_limits_stop_search_without_retrying_in_a_browser(self):
        for key in ("ev.crawl_task_limit", "ev.crawl_daily_limit", "ev.cancelled"):
            with self.subTest(key=key), mock.patch.object(search, "_endpoint_fetch", side_effect=crawl.Skip(key)) as fetch, \
                    mock.patch.object(search, "_stealth_fetch") as stealth:
                with self.assertRaises((UserError, crawl.Skip)):
                    search.free("public enterprise partner", crawler=self.crawler())
                self.assertEqual(fetch.call_count, 1)
                stealth.assert_not_called()

    def test_robots_denial_may_use_allowed_engine_but_never_browser_on_denied_site(self):
        def fetch(name, url, crawler):
            if name.startswith("ddg"):
                raise crawl.Skip("ev.robots_blocked", url=url)
            return self.page("bing", [("https://allowed-partner.example/", "Allowed partner")])
        with mock.patch.object(search, "_endpoint_fetch", side_effect=fetch), \
                mock.patch.object(search, "_stealth_fetch") as stealth:
            result = search.free("public enterprise partner", crawler=self.crawler())
        self.assertEqual(result[0]["url"], "https://allowed-partner.example/")
        stealth.assert_not_called()
        with mock.patch.object(search, "_endpoint_fetch", side_effect=crawl.Skip("ev.robots_blocked")), \
                mock.patch.object(search, "_stealth_fetch") as stealth:
            with self.assertRaises(UserError):
                search.free("public enterprise partner", crawler=self.crawler())
            stealth.assert_not_called()

    def test_browser_fallback_disabled_and_busy_do_not_bypass_browser_slot(self):
        from salesteam import browsers
        crawler = self.crawler()
        crawler.cfg["stealth_mode"] = False
        with mock.patch.object(crawler, "_browser_fetch") as browser:
            self.assertIsNone(crawler.stealth_page("https://public.example/"))
            browser.assert_not_called()
        crawler.cfg["stealth_mode"] = True
        slot = mock.Mock()
        slot.acquire.return_value = False
        with mock.patch.object(browsers, "ready", return_value=True), \
                mock.patch.object(crawl, "_browser_slot", slot), \
                mock.patch.object(crawler, "BROWSER_WAIT_S", 1), \
                mock.patch("scrapling.fetchers.StealthyFetcher.fetch") as launch:
            response, reason = crawler._browser_fetch("https://public.example/", stealth=True)
        self.assertIsNone(response)
        self.assertEqual(reason, "browser_busy")
        launch.assert_not_called()
        slot.release.assert_not_called()

    def test_actual_task_lifecycle_fails_only_when_all_requested_sources_fail(self):
        from salesteam.agents import researcher
        now = db.now()
        pid = db.execute("INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)",
                         ("Pipeline QA", db.dumps({"name": "Pipeline QA", "product": "Public partner research",
                           "customer_types": ["channel_partner"], "product_words": ["public partner"],
                           "buy_words": ["partnership"]}), now, now))
        enabled = settings.get("agents_enabled")
        enabled["analyst"] = False
        settings.put("agents_enabled", enabled)
        cases = (
            ({"google": {"status": "failed", "error": "search_blocked"},
              "web": {"status": "failed", "error": "network_error"}}, "failed", "error", "search_blocked"),
            ({"google": {"status": "failed", "error": "search_blocked"},
              "web": {"status": "done"}}, "done", "no_results", ""),
            ({"google": {"status": "done"}, "web": {"status": "done"}}, "done", "no_results", ""),
        )
        for index, (source_status, expected_status, expected_result, expected_error) in enumerate(cases):
            with self.subTest(case=index):
                ident = f"source-lifecycle-{index}"
                params = {"sources": ["google", "web"], "target": 10, "icp_id": pid}
                db.execute("INSERT INTO tasks(id,kind,icp_id,params,status,target,created_at) VALUES(?,?,?,?,?,?,?)",
                           (ident, "research", pid, db.dumps(params), "running", 10, now))
                run = tasks.TaskRun(db.query_one("SELECT * FROM tasks WHERE id=?", (ident,)))
                tasks._RUNS[ident] = run

                def produce():
                    with run.stats_lock:
                        run.stats["source_status"] = source_status
                    run.source_notes.update({src: value["error"] for src, value in source_status.items()
                                             if value.get("error")})
                # The real task worker, queue terminator, analyst drain, final
                # classification and database writes execute; only acquisition is deterministic.
                with mock.patch.object(researcher, "Researcher") as producer, \
                        mock.patch("socket.socket.connect", side_effect=AssertionError("unexpected task test network")):
                    producer.return_value.execute.side_effect = produce
                    tasks._execute(run)
                row = db.query_one("SELECT * FROM tasks WHERE id=?", (ident,))
                self.assertEqual((row["status"], row["result"], row["error"]),
                                 (expected_status, expected_result, expected_error))
                self.assertTrue(run.done)
                self.assertEqual(db.loads(row["stats"], {})["source_status"], source_status)
                self.assertEqual(run.events[-1]["key"], "ev.task_closed")


if __name__ == "__main__":
    unittest.main()

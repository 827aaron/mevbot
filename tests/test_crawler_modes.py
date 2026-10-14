"""Actual Scrapling page parsing, deferred JS shells and native selection setup."""
from io import BytesIO
import os
from types import SimpleNamespace
import unittest
from unittest import mock

import test_app
from scrapling.engines.toolbelt.custom import Response
from salesteam import desktop, paths
from salesteam.sources.crawler import Crawler, Skip


class CrawlerModeTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp

    def response(self, html):
        return Response(url='https://public.fixture.test/partners',content=html.encode(),status=200,reason='OK',
                        cookies={},headers={'content-type':'text/html'},request_headers={})

    def shell(self):
        return self.response('<html><head><title>Company partners</title></head><body>'
                             '<div id="root">Loading partner content</div><script>window.app={};</script></body></html>')

    def test_unready_browser_never_counts_javascript_shell_as_readable_content(self):
        crawler=Crawler(count_daily=False);crawler.cfg['render_js']=True
        with mock.patch.object(crawler,'_robots_ok',return_value=(True,0)), \
                mock.patch('scrapling.fetchers.Fetcher.get',return_value=self.shell()), \
                mock.patch.object(crawler,'_browser_fetch',return_value=(None,'browser_not_ready')):
            with self.assertRaises(Skip) as missing: crawler.fetch('https://public.fixture.test/partners')
        self.assertEqual(missing.exception.key,'ev.crawl_deferred')
        self.assertEqual(missing.exception.params['reason'],'browser_not_ready')

    def test_rendered_page_content_replaces_shell_and_preserves_actual_links(self):
        crawler=Crawler(count_daily=False);crawler.cfg['render_js']=True
        rendered=self.response('<html><head><title>Company partners</title></head><body><article>'
                'We are an iGaming affiliate agency. Our public partner directory contains professional companies. '
                '<a href="https://forum.example/public-partners">Public partners forum</a></article></body></html>')
        with mock.patch.object(crawler,'_robots_ok',return_value=(True,0)), \
                mock.patch('scrapling.fetchers.Fetcher.get',return_value=self.shell()), \
                mock.patch.object(crawler,'_browser_fetch',return_value=(rendered,'')):
            page=crawler.fetch('https://public.fixture.test/partners')
        self.assertIn('iGaming affiliate agency',page['markdown'])
        self.assertIn(('https://forum.example/public-partners','Public partners forum'),page['links'])
        self.assertEqual(crawler.stats['dynamic'],1)

    def test_real_webview_window_receives_text_selection_enabled_without_launching_gui(self):
        import webview
        controller=SimpleNamespace(show=lambda:None,should_terminate=lambda:True,
                                   on_closing=lambda:True,started=lambda:None)
        with mock.patch.dict(os.environ,{'SALESTEAM_HEADLESS':'','SALESTEAM_DATA':str(paths.DATA_DIR)}), \
                mock.patch.object(webview,'windows',[]), \
                mock.patch.object(webview,'start'), \
                mock.patch.object(desktop,'Controller',return_value=controller), \
                mock.patch.object(desktop,'_reload_on_web_crash'), \
                mock.patch.object(desktop,'_show_on_reopen'), \
                mock.patch.object(desktop,'_mac_terminate_hook'), \
                mock.patch('urllib.request.urlopen',return_value=BytesIO(b'{"ui_lang":"zh"}')):
            desktop._window(8765,None)
            self.assertEqual(len(webview.windows),1)
            self.assertTrue(webview.windows[0].text_select)


if __name__=='__main__':unittest.main()

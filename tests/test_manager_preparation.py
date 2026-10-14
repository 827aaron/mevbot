"""Verified source preparation and exactly-once dispatch, without external APIs."""
import os
import unittest
from unittest import mock

import test_app
from salesteam import background, db, edition, llm, settings, source_pool, tasks
from salesteam.agents import icp, manager, preparation, researcher
from salesteam.sources.crawler import Skip
from salesteam.errors import UserError


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'SALESTEAM_EDITION': 'my_igaming'}))
        test_app.AppTests.setUp(self)
        self.enterContext(mock.patch.object(preparation, '_busy', False))
        self.profile = icp.normalize(edition.presets()[0]['data'])
        self.pid = db.execute('INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)',
                              ('Preparation QA', db.dumps(self.profile), db.now(), db.now()))
        for a in ('manager', 'researcher', 'analyst'):
            llm.update_config(a, {'provider': 'mock', 'model': 'qa-mock'})
        self.seed = 'https://directory.example/public-partners'
        self.page = {'url': self.seed, 'title': 'Public industry source',
                     'markdown': 'Public iGaming industry association directory.', 'links': [], 'emails': []}

    def queue(self):
        with mock.patch.object(background, 'spawn'):
            preparation.request(self.pid)
        return preparation.state()['token']

    def test_proposed_urls_are_not_persisted_as_sources_and_are_bounded_cached_and_validated(self):
        proposals = [self.seed, 'http://127.0.0.1/private', 'https://t.me/+inventedgroup',
                     'https://public.example/register', 'https://unrelated.example/']
        with mock.patch.object(llm, 'call_json', return_value=({'urls': proposals}, {})) as model:
            self.assertEqual(source_pool.bootstrap_urls(self.pid), [self.seed, 'https://unrelated.example/'])
            self.assertEqual(source_pool.bootstrap_urls(self.pid), [self.seed, 'https://unrelated.example/'])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(source_pool.items(), [])
        self.assertIsNone(source_pool.config()['icp_id'])

    def test_failed_source_checks_block_dispatch_and_keep_source_pool_empty(self):
        token = self.queue()
        with mock.patch.object(llm, 'test_agent'), \
                mock.patch.object(source_pool, 'bootstrap_urls', return_value=[self.seed]), \
                mock.patch.object(preparation.Crawler, 'fetch', side_effect=Skip('ev.robots_blocked')):
            preparation._work(token)
        self.assertEqual(preparation.state()['phase'], 'blocked')
        self.assertEqual(preparation.state()['error'], 'source_preparation_failed')
        self.assertEqual(db.query('SELECT * FROM tasks'), [])
        self.assertEqual(source_pool.items(), [])

    def test_existing_confirmed_profile_is_recovered_and_queued_only_once(self):
        settings.put('onboarding', {'done': True})
        db.execute('INSERT INTO profile_versions(version,icp_id,company,icp,status,created_at) VALUES(?,?,?,?,?,?)',
                   (1, self.pid, '{}', db.dumps(self.profile), 'active', db.now()))
        with mock.patch.object(background, 'spawn') as spawn:
            preparation.tick()
            preparation.tick()
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(preparation.state()['icp_id'], self.pid)
        self.assertEqual(db.query('SELECT * FROM tasks'), [])

    def test_verified_source_then_dispatch_retries_and_restart_do_not_duplicate_first_task(self):
        token = self.queue()
        enabled = settings.get('agents_enabled')
        self.assertTrue(enabled['manager'])
        before_budget = settings.get('budget')
        with mock.patch.object(llm, 'test_agent') as probes, \
                mock.patch.object(source_pool, 'bootstrap_urls', return_value=[self.seed]), \
                mock.patch.object(preparation.Crawler, 'fetch', return_value=self.page), \
                mock.patch.object(background, 'spawn'):
            preparation._work(token)
            self.assertEqual(probes.call_count, 3)
            first = db.query('SELECT * FROM tasks')
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]['created_by'], 'manager')
            self.assertEqual(db.loads(first[0]['params'])['urls'], [self.seed])
            self.assertEqual(db.loads(first[0]['params'])['sources'], ['web'])
            preparation.request(self.pid)
            preparation.tick()
            manager.tick()
        self.assertEqual(preparation.state()['phase'], 'dispatched')
        self.assertEqual(len(db.query('SELECT * FROM tasks')), 1)
        self.assertTrue(source_pool.config()['enabled'])
        self.assertEqual(source_pool.config()['icp_id'], self.pid)
        self.assertTrue(source_pool.entry_for(self.seed)['evidence'])
        self.assertEqual(settings.get('budget'), before_budget)

    def test_missing_model_or_failed_model_probe_stops_before_crawling(self):
        token = self.queue()
        with mock.patch.object(llm, 'configured', return_value=False), \
                mock.patch.object(preparation.Crawler, 'fetch') as fetch:
            preparation._work(token)
        self.assertEqual(preparation.state()['error'], 'agent_not_configured')
        fetch.assert_not_called()
        self.assertEqual(db.query('SELECT * FROM tasks'), [])

    def test_invalid_configured_decision_model_blocks_before_source_or_task(self):
        token = self.queue()
        with mock.patch.object(llm, 'test_agent'), \
                mock.patch('salesteam.llm.decision.configured', return_value=True), \
                mock.patch('salesteam.llm.decision.test', side_effect=UserError('decision_bad_format')), \
                mock.patch.object(preparation.Crawler, 'fetch') as fetch:
            preparation._work(token)
        self.assertEqual(preparation.state()['error'], 'decision_bad_format')
        self.assertEqual(db.query('SELECT * FROM tasks'), [])
        fetch.assert_not_called()

    def test_unrelated_readable_page_does_not_count_as_verified_industry_source(self):
        token = self.queue()
        with mock.patch.object(llm, 'test_agent'), \
                mock.patch.object(source_pool, 'bootstrap_urls', return_value=[self.seed]), \
                mock.patch.object(preparation.Crawler, 'fetch', return_value={**self.page, 'markdown': 'Weather forecast.'}):
            preparation._work(token)
        self.assertEqual(preparation.state()['phase'], 'blocked')
        self.assertEqual(source_pool.items(), [])
        self.assertEqual(db.query('SELECT * FROM tasks'), [])

    def test_manual_web_task_bootstraps_without_free_search_and_extracts_actual_read_evidence(self):
        params = {'icp_id': self.pid, 'target': 20, 'sources': ['web'], 'urls': []}
        tid = 'bootstrap-manual-qa'
        db.execute('INSERT INTO tasks(id,kind,icp_id,params,status,target,created_at) VALUES(?,?,?,?,?,?,?)',
                   (tid, 'research', self.pid, db.dumps(params), 'running', 20, db.now()))
        run = tasks.TaskRun(db.query_one('SELECT * FROM tasks WHERE id=?', (tid,)))
        worker = researcher.Researcher(run, self.profile)
        worker.mock = False
        candidate = {'company_name': 'QA agency', 'description': 'We are an iGaming affiliate agency.',
                     'evidence_quote': 'We are an iGaming affiliate agency.', 'source_url': self.seed}
        page = {**self.page, 'markdown': candidate['evidence_quote']}
        with mock.patch.object(source_pool, 'bootstrap_urls', return_value=[self.seed]), \
                mock.patch.object(worker.crawler, 'fetch', return_value=page), \
                mock.patch('salesteam.sources.search.search', side_effect=AssertionError('no search dependency')), \
                mock.patch.object(llm, 'call_json', return_value=({'candidates': [candidate]}, {})):
            worker.execute()
        self.assertEqual(run.stats['pages'], 1)
        self.assertEqual(run.stats['pool'], 1)
        row = db.query_one('SELECT * FROM raw_leads WHERE task_id=?', (tid,))
        self.assertEqual(row['evidence'], candidate['evidence_quote'])
        self.assertEqual(row['source_url'], self.seed)
        self.assertEqual(source_pool.config()['icp_id'], self.pid)


if __name__ == '__main__':
    unittest.main()

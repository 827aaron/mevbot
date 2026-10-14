"""Expansion before bounded reruns, counted across the original task's lineage."""
import os
import unittest
from unittest import mock
import test_app
from salesteam import background, db, edition, llm, settings, source_pool, tasks
from salesteam.agents import icp, manager, preparation, remediation
from salesteam.sources.crawler import Skip


class RemediationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {'SALESTEAM_EDITION': 'my_igaming'}))
        test_app.AppTests.setUp(self)
        self.enterContext(mock.patch.object(remediation, '_active', set()))
        profile = icp.normalize(edition.presets()[0]['data'])
        self.pid = db.execute('INSERT INTO icp_templates(name,data,created_at,updated_at) VALUES(?,?,?,?)',
                              ('Retry QA', db.dumps(profile), db.now(), db.now()))
        for a in ('manager', 'researcher', 'analyst'):
            llm.update_config(a, {'provider': 'mock', 'model': 'qa-mock'})
        source_pool.set_settings({'icp_id': self.pid})

    def root(self, retries=2, result='sources_exhausted'):
        p={'icp_id':self.pid,'sources':['web'],'target':5,'retry_count':retries,'urls':[]}
        db.execute('INSERT INTO tasks(id,kind,icp_id,params,status,target,result,created_at) VALUES(?,?,?,?,?,?,?,?)',
                   ('retry-root','research',self.pid,db.dumps(p),'done',5,result,db.now()))
        return 'retry-root'

    def lead(self, tid, name):
        db.execute('INSERT INTO leads(company_name,company_key,grade,qualified,status,task_id,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?,?)', (name,name,'A',1,'qualified',tid,db.now(),db.now()))

    def finish(self, tid):
        db.execute("UPDATE tasks SET status='done',result='sources_exhausted' WHERE id=?",(tid,))
        if tid in tasks._RUNS: tasks._RUNS[tid].done=True

    def expand(self, key, url):
        page={'url':url,'title':'QA public industry source','markdown':'Public iGaming partner association directory.',
              'links':[],'emails':[]}
        with mock.patch.object(preparation,'_probe'), \
                mock.patch.object(source_pool,'bootstrap_urls',return_value=[url]), \
                mock.patch.object(remediation.Crawler,'fetch',return_value=page), \
                mock.patch.object(background,'spawn'):
            remediation._expand(key)

    def test_single_attempt_expands_first_retries_remaining_target_and_never_exceeds_customer_limit(self):
        root=self.root(2);self.lead(root,'QA first');self.lead(root,'QA second')
        with mock.patch.object(background,'spawn'):
            remediation.tick();remediation.tick()
        self.assertEqual(len(db.query('SELECT * FROM tasks')),1)
        first_key='single:retry-root:1'
        self.expand(first_key,'https://forum.example/new-source-one')
        child=db.query_one("SELECT * FROM tasks WHERE created_by='manager_retry'")
        self.assertEqual(child['target'],3)
        self.assertTrue(source_pool.entry_for('https://forum.example/new-source-one')['evidence'])
        self.assertEqual(db.loads(child['params'])['retry_count'],0)
        self.lead(child['id'],'QA third');self.finish(child['id'])
        with mock.patch.object(background,'spawn'): remediation.tick()
        self.expand('single:retry-root:2','https://forum.example/new-source-two')
        children=db.query("SELECT * FROM tasks WHERE created_by='manager_retry' ORDER BY created_at")
        self.assertEqual([c['target'] for c in children],[3,2])
        self.finish(children[-1]['id'])
        with mock.patch.object(background,'spawn'):
            remediation.tick();remediation.tick()
        self.assertEqual(len(db.query('SELECT * FROM tasks')),3)
        self.assertEqual(set(remediation.jobs()),{first_key,'single:retry-root:2'})

    def test_failed_expansion_never_dispatches_an_empty_rerun(self):
        self.root()
        with mock.patch.object(background,'spawn'): remediation.tick()
        with mock.patch.object(preparation,'_probe'), \
                mock.patch.object(source_pool,'bootstrap_urls',return_value=['https://forum.example/blocked']), \
                mock.patch.object(remediation.Crawler,'fetch',side_effect=Skip('ev.robots_blocked')):
            remediation._expand('single:retry-root:1')
        self.assertEqual(remediation.jobs()['single:retry-root:1']['phase'],'blocked')
        self.assertEqual(len(db.query('SELECT * FROM tasks')),1)
        self.assertEqual(source_pool.items(),[])

    def test_zero_retries_pause_and_cancelled_chain_do_not_expand_or_dispatch(self):
        self.root(0)
        with mock.patch.object(background,'spawn') as spawn: remediation.tick()
        spawn.assert_not_called()
        self.assertEqual(remediation.jobs(),{})
        db.execute("UPDATE tasks SET params=? WHERE id='retry-root'",(db.dumps({'sources':['web'],'retry_count':3}),))
        tasks.set_paused(True)
        remediation.tick()
        self.assertEqual(remediation.jobs(),{})
        tasks.set_paused(False)
        db.execute("UPDATE tasks SET status='cancelled' WHERE id='retry-root'")
        remediation.tick()
        self.assertEqual(remediation.jobs(),{})

    def test_daily_retry_waits_for_verified_expansion_and_adds_crawler_source(self):
        manager.set_goal({'enabled':True,'icp_id':self.pid,'sources':['google'],'target':5,'max_retries':3,
                          'time_of_day':'00:00'})
        with mock.patch.object(background,'spawn'):
            first=manager.tick()[0]
            self.finish(first);self.lead(first,'QA daily one')
            self.assertEqual(manager.tick(),[])
        plan=manager.plans()[0]; key=f"manager:{plan['id']}:2"
        self.expand(key,'https://directory.example/new-partners')
        with mock.patch.object(background,'spawn'):
            second=manager.tick()[0]
            self.assertEqual(manager.tick(),[])
        row=db.query_one('SELECT * FROM tasks WHERE id=?',(second,))
        self.assertEqual(row['target'],4)
        self.assertIn('web',db.loads(row['params'])['sources'])
        self.assertEqual(manager.plans()[0]['attempts'],2)


if __name__=='__main__':unittest.main()

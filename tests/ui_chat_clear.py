"""Controlled browser checks for chat and memo deletion; no real model or platform access.
Run with .venv/bin/python tests/ui_chat_clear.py (Chromium required).
"""
import json, os, socket, sqlite3, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
root=Path(__file__).resolve().parents[1]; out=Path(os.environ.get('SALESTEAM_CHAT_UI_OUTPUT', '/tmp/jamesagent-chat-clear-ui')); out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='ja-chat-clear-ui-') as data:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    env=dict(os.environ,SALESTEAM_DATA=data,SALESTEAM_PORT=str(port),SALESTEAM_EDITION='my_igaming',SALESTEAM_NO_SCHEDULER='1',SALESTEAM_NO_BROWSER_DOWNLOAD='1',SALESTEAM_QA_FIXTURES='')
    log=(out/'server.log').open('w')
    proc=subprocess.Popen([sys.executable,'run.py','--skip-deps','--no-browser'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(path):
        with opener.open(base+path,timeout=4) as r:return json.load(r)
    def setting(key,value):
        with sqlite3.connect(Path(data)/'salesteam.db') as db:
            db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(key,json.dumps(value)))
    def seed():
        with sqlite3.connect(Path(data)/'salesteam.db') as db:
            db.execute('DELETE FROM agent_chats');db.execute('DELETE FROM memos')
            for agent,text in [('manager','QA_OLD_MANAGER_REPLY'),('analyst','QA_OTHER_REPLY')]:
                db.execute('INSERT INTO agent_chats(agent,role,content,meta,ts) VALUES(?,?,?,?,?)',(agent,'assistant',text,'{}',time.time()))
            for agent in ['manager','analyst','all']:
                db.execute("INSERT INTO memos(agent,text,status,created_at) VALUES(?,?,?,?)",(agent,'QA_MEMO_'+agent,'confirmed',time.time()))
    try:
        for _ in range(100):
            try:get('/api/meta');break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('server not ready')
        setting('onboarding',{'done':True});setting('agreement',{'accepted_at':time.time()});setting('onboarding_skips',{'steps':['ai']})
        errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-background-networking'])
            for lang in ['zh','en']:
                seed();setting('ui_lang',lang)
                context=browser.new_context(viewport={'width':1280,'height':900})
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base+'/') else route.abort())
                page=context.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
                page.goto(base+'/#/dashboard')
                clear=page.locator('[data-action=mgrClear][data-k=manager]').first
                clear.wait_for();expect(page.get_by_text('QA_OLD_MANAGER_REPLY',exact=True)).to_be_visible()
                clear.click();page.locator('#modal [data-x]').click()
                assert len(get('/api/chat/manager')['messages'])==1
                stale=get('/api/chat/manager');held=[]
                page.route(base+'/api/chat/manager',lambda route:held.append(route) if route.request.method=='GET' else route.continue_())
                page.evaluate('void MGR.load("manager",true)')
                for _ in range(20):
                    page.wait_for_timeout(50)
                    if held:break
                assert held,'no pending history response'
                clear.click();page.locator('#confirm-ok').click()
                expect(page.get_by_text('QA_OLD_MANAGER_REPLY',exact=True)).to_have_count(0)
                held[0].fulfill(status=200,content_type='application/json',body=json.dumps(stale))
                page.wait_for_timeout(100)
                expect(page.get_by_text('QA_OLD_MANAGER_REPLY',exact=True)).to_have_count(0)
                assert len(get('/api/memos'))==3
                page.unroute(base+'/api/chat/manager')
                page.locator('[data-action=mgrForget][data-k=manager]').first.click();page.locator('#confirm-ok').click()
                expect(page.locator('#modal')).to_have_count(0)
                assert {r['agent'] for r in get('/api/memos')}=={'all','analyst'}
                assert len(get('/api/chat/analyst')['messages'])==1
                page.screenshot(path=str(out/f'chat-{lang}.png'))
                page.evaluate("void UI.go('learning/memos')")
                page.locator('[data-action=mgrForget][data-k=all]').wait_for()
                page.screenshot(path=str(out/f'memory-{lang}.png'))
                page.locator('[data-action=mgrForget][data-k=all]').click();page.locator('#confirm-ok').click()
                expect(page.locator('#modal')).to_have_count(0)
                assert get('/api/memos')==[] and get('/api/chat/analyst')['messages']==[]
                print(json.dumps({'lang':lang,'passed':'clear/cancel, stale history ignored, current memory scope, all memory reset, no external requests'},ensure_ascii=False),flush=True)
                context.close()
            browser.close()
        assert not errors,errors
    finally:
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()

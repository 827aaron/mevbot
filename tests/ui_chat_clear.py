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
            db.execute('INSERT INTO agent_chats(agent,role,content,meta,ts) VALUES(?,?,?,?,?)',('manager','user','QA_COPY \"quoted\" <tag> 多行\n第二行','{}',time.time()))
            db.execute('INSERT OR REPLACE INTO icp_templates(id,name,data,created_at,updated_at) VALUES(?,?,?,?,?)',(1,'UI QA',json.dumps({'name':'UI QA','product':'Public industry research','customer_types':['channel_team'],'b2b_segment':'channel_team','product_words':['iGaming'],'buy_words':['agency'],'grading_criteria':[{'name':'fit','weight':100,'description':'QA fit'}]}),time.time(),time.time()))
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
                context.grant_permissions(['clipboard-read','clipboard-write'],origin=base)
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base+'/') else route.abort())
                page=context.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
                page.goto(base+'/#/dashboard')
                clear=page.locator('[data-action=mgrClear][data-k=manager]').first
                clear.wait_for();expect(page.get_by_text('QA_OLD_MANAGER_REPLY',exact=True)).to_be_visible()
                messages=get('/api/chat/manager')['messages'];user=next(m for m in messages if m['from']=='me')
                copy=page.locator('[data-action=chatCopy]').filter(has_text='复制' if lang=='zh' else 'Copy')
                target=page.locator('[data-action=chatCopy][data-copy-text]').filter(has=page.locator('svg')).all()
                copybtn=page.locator('[data-action=chatDelete][data-id="'+str(user['id'])+'"]').locator('..').locator('[data-action=chatCopy]')
                copybtn.click();assert page.evaluate('navigator.clipboard.readText()')==user['text']
                page.evaluate("Object.defineProperty(navigator.clipboard,'writeText',{value:()=>Promise.reject(new Error('WKWebView fixture'))})")
                copybtn.click();assert page.evaluate('navigator.clipboard.readText()')==user['text']
                assert page.locator('.msg.me .bubble').evaluate("e=>getComputedStyle(e).userSelect")=='text'
                delete=page.locator('[data-action=chatDelete][data-id="'+str(user['id'])+'"]')
                delete.click();page.locator('#modal [data-x]').click();assert len(get('/api/chat/manager')['messages'])==2
                delete.click();page.locator('#confirm-ok').click();expect(delete).to_have_count(0)
                assert len(get('/api/chat/manager')['messages'])==1 and len(get('/api/memos'))==3

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
                page.evaluate("void UI.go('run')");page.locator('#ops-preparation').wait_for()
                assert page.locator('#t-retry-count option').count()==4
                assert page.locator('#t-retry-count').input_value()=='1'
                page.locator('[data-action=opsPrepare]').click()
                for _ in range(40):
                    page.wait_for_timeout(100)
                    if get('/api/manager/state')['preparation'].get('phase')=='blocked':break
                assert get('/api/manager/state')['preparation']['error']=='agent_not_configured'
                assert get('/api/tasks')==[]
                page.evaluate('void UI.refresh()');page.locator('#ops-preparation .alert-warning').wait_for()
                warning=page.locator('#ops-preparation .alert-warning').inner_text()
                assert 'API' in warning and '[object Object]' not in warning,warning
                page.screenshot(path=str(out/f'preflight-{lang}.png'))
                print(json.dumps({'lang':lang,'passed':'real clipboard and fallback, single-delete/cancel, stale history ignored, memo reset, preflight blocked without task, retry controls, no external requests'},ensure_ascii=False),flush=True)
                context.close()
            browser.close()
        assert not errors,errors
    finally:
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()

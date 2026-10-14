"""Profile-confirmed Manager preparation, verified sources, then daily dispatch."""
import hashlib
import threading
import time
import uuid

from .. import background, db, edition, llm, settings, source_pool, tasks
from ..errors import UserError
from ..sources.crawler import Crawler, Skip
from . import icp, manager

_lock = threading.RLock()
_busy = False


def state():
    return settings.get('manager_preparation', {}) or {}


def _environment_signature():
    from .. import browsers
    rows = db.query('SELECT agent,provider,model,preset,base_url,updated_at FROM agent_configs')
    stamps = db.query('SELECT name,updated_at FROM secrets')
    return hashlib.sha256(db.dumps([rows, stamps, settings.ai_paused(),
                                   settings.get('agents_enabled'), settings.get('budget'),
                                   settings.get('decision', {}),
                                   llm.budget_state()['paused'], browsers.ready()]).encode()).hexdigest()


@background.tracked('manager:prepare_request')
def request(icp_id):
    profile = icp.get(icp_id)['data']
    missing = icp.required_missing(profile)
    if missing:
        raise UserError('profile_incomplete', fields=','.join(f'@icp.{m}' for m in missing))
    with _lock:
        previous = state()
        signature = hashlib.sha256(db.dumps(profile).encode()).hexdigest()
        if (previous.get('icp_id') == icp_id and previous.get('profile_signature') == signature
                and previous.get('phase') in ('checking', 'sources', 'ready', 'dispatched')):
            return previous
        value = {'icp_id': icp_id, 'profile_signature': signature, 'token': uuid.uuid4().hex,
                 'phase': 'pending', 'checks': [], 'error': '', 'updated_at': db.now()}
        settings.put('manager_preparation', value)
    tick()
    return state()


def _put(token, **fields):
    with _lock:
        value = state()
        if value.get('token') != token:
            raise UserError('manager_preparation_changed')
        value.update(fields, updated_at=db.now())
        settings.put('manager_preparation', value)
        return value


def _check(token, name, status='passed', **params):
    value = state()
    checks = [c for c in value.get('checks', []) if c['name'] != name]
    checks.append({'name': name, 'status': status, 'params': params})
    return _put(token, checks=checks)


def _stopped(token):
    return settings.ai_paused() or state().get('token') != token


def _probe(agent):
    fingerprint = _environment_signature() + ':' + agent
    cached = settings.get('manager_connection_checks', {}) or {}
    if db.now() - cached.get(fingerprint, 0) < 24 * 3600:
        return
    llm.test_agent(agent)
    cached[fingerprint] = db.now()
    settings.put('manager_connection_checks', dict(list(cached.items())[-30:]))


def _probe_decision():
    from ..llm import decision
    if not decision.configured() or decision.get_config().get('fallback_active'):
        return False
    fingerprint = _environment_signature() + ':decision'
    cached = settings.get('manager_connection_checks', {}) or {}
    if db.now() - cached.get(fingerprint, 0) >= 24 * 3600:
        decision.test()
        cached[fingerprint] = db.now()
        settings.put('manager_connection_checks', dict(list(cached.items())[-30:]))
    return True


@background.tracked('manager:prepare_worker')
def _work(token):
    global _busy
    try:
        value = _put(token, phase='checking', checks=[], error='')
        pid = value['icp_id']
        profile = icp.get(pid)['data']
        missing = icp.required_missing(profile)
        if missing:
            raise UserError('profile_incomplete', fields=','.join(f'@icp.{m}' for m in missing))
        _check(token, 'profile')
        for agent in ('manager', 'researcher', 'analyst'):
            if _stopped(token):
                raise UserError('ai_paused')
            if not settings.agent_enabled(agent):
                raise UserError('agent_disabled', agent=f'@agent.{agent}')
            if not llm.configured(agent):
                raise UserError('agent_not_configured', agent=f'@agent.{agent}')
            llm.check_budget(agent, llm.get_config(agent))
            _probe(agent)
            _check(token, agent)
        _check(token, 'decision', 'passed' if _probe_decision() else 'optional')
        _check(token, 'budget')
        _put(token, phase='sources')
        seeds = source_pool.urls() if source_pool.config()['icp_id'] == pid else []
        seeds = [u for u in seeds if (source_pool.entry_for(u) or {}).get('access', 'public') == 'public']
        if not seeds:
            seeds = source_pool.bootstrap_urls(pid)
        crawler = Crawler(should_stop=lambda: _stopped(token), max_pages=5)
        crawler.cfg['render_js'] = True
        verified, reasons = [], []
        for url in seeds[:5]:
            if _stopped(token):
                raise UserError('ai_paused')
            try:
                page = crawler.fetch(url)
                if _stopped(token):
                    raise UserError('ai_paused')
                if (edition.signal_mode(profile) and
                        not source_pool._industry_evidence(page['url'], page.get('markdown', ''))):
                    reasons.append('source_no_evidence')
                    continue
                source_pool.accept_bootstrap_page(pid, page)
                verified.append(page['url'])
            except Skip as exc:
                reasons.append(exc.key)
        if not verified:
            raise UserError('source_preparation_failed', detail=', '.join(dict.fromkeys(reasons)) or 'no proposed URLs')
        _check(token, 'sources', count=len(verified))
        tg_ready = bool(db.query_one("SELECT c.id FROM tg_chats c JOIN tg_accounts a ON a.id=c.account_id "
                                    "WHERE c.monitored=1 AND c.left_at IS NULL AND a.status='active' "
                                    "AND a.session_enc<>'' LIMIT 1"))
        _check(token, 'accounts', 'passed' if tg_ready else 'optional', telegram=tg_ready)
        if _stopped(token):
            raise UserError('ai_paused')
        # Preserve the customer's target, clock time and retry limit. Public
        # crawling is available independently of search keys and social logins.
        g = {**manager.goal(), 'enabled': True, 'icp_id': pid, 'sources': ['web'] + (['tg'] if tg_ready else [])}
        tasks.validate_research({**g, 'urls': verified})
        _put(token, phase='ready', verified_urls=verified)
        with manager._lock, tasks.registry_lock():
            existing = db.query_one('SELECT goal FROM manager_plans WHERE day=?', (manager.day(),))
            if existing and db.loads(existing['goal'], {}).get('icp_id') != pid:
                raise UserError('goal_changed')
            if existing:
                g['sources'] = db.loads(existing['goal'])['sources']
            manager.set_goal(g)
            # A confirmed profile's first run begins after checks now. The
            # usual daily clock continues to govern later calendar days.
            db.execute('INSERT OR IGNORE INTO manager_plans(day,goal,created_at,updated_at) VALUES(?,?,?,?)',
                       (manager.day(), db.dumps(g), db.now(), db.now()))
            started = manager.tick()
            plan = manager.plans(manager.day())[0]
            if not started and plan['status'] not in ('running', 'met'):
                raise UserError(plan.get('last_error') or 'manager_attempts_exhausted')
            if not started:
                started = plan['task_ids']
        if source_pool.config()['icp_id'] == pid:
            source_pool.set_settings({'enabled': True, 'time_of_day': g['time_of_day']})
        _put(token, phase='met' if plan['status'] == 'met' else 'dispatched', task_ids=started, error='')
    except UserError as exc:
        if state().get('token') == token:
            _put(token, phase='blocked', error=exc.key, params=exc.params,
                 environment_signature=_environment_signature())
    except Exception as exc:
        if state().get('token') == token:
            _put(token, phase='blocked', error='manager_preparation_failed',
                 params={'detail': type(exc).__name__}, environment_signature=_environment_signature())
    finally:
        with _lock:
            _busy = False


@background.tracked('manager:prepare_tick')
def tick():
    global _busy
    if edition.current()['id'] != 'my_igaming':
        return
    with _lock:
        value = state()
        if not value and settings.get('onboarding', {}).get('done'):
            confirmed = db.query_one("SELECT icp_id FROM profile_versions WHERE status='active' ORDER BY version DESC LIMIT 1")
            if confirmed and confirmed['icp_id']:
                request(confirmed['icp_id'])
            return
        if not value or _busy or settings.ai_paused():
            return
        retry = (value.get('phase') == 'blocked' and
                 value.get('environment_signature') != _environment_signature())
        g = manager.goal()
        new_day = (g['enabled'] and manager.day(value.get('updated_at', db.now())) != manager.day() and
                   time.strftime('%H:%M', time.localtime()) >= g['time_of_day'] and
                   value.get('phase') in ('dispatched', 'met', 'blocked'))
        retry = retry or new_day
        if value.get('phase') not in ('pending', 'checking', 'sources') and not retry:
            return
        _busy = True
        try:
            background.spawn(_work, value['token'], name='manager-preparation')
        except Exception:
            _busy = False
            raise

"""Bounded retries: verify new source entries before daily or single-task reruns."""
import threading

from .. import background, db, edition, llm, settings, source_pool, tasks
from ..errors import UserError
from ..sources.crawler import Crawler, Skip
from . import preparation

_lock = threading.RLock()
_active = set()


def jobs():
    return settings.get('manager_remediations', {}) or {}


def _save(key, **fields):
    with _lock:
        values = jobs()
        values[key] = {**values.get(key, {}), **fields, 'updated_at': db.now()}
        settings.put('manager_remediations', values)
        return values[key]


@background.tracked('manager:expand_request')
def ensure(key, icp_id):
    with _lock:
        value = jobs().get(key)
        if value is None:
            value = _save(key, icp_id=icp_id, phase='pending', error='')
        elif value['phase'] == 'blocked' and value.get('environment_signature') != preparation._environment_signature():
            value = _save(key, phase='pending', error='')
        if value['phase'] in ('pending', 'expanding') and key not in _active and not settings.ai_paused():
            _active.add(key)
            try:
                background.spawn(_expand, key, name='manager-expand-' + key)
            except Exception:
                _active.discard(key)
                raise
        return value


@background.tracked('manager:expand_worker')
def _expand(key):
    try:
        value = _save(key, phase='expanding')
        pid = value['icp_id']
        for agent in ('manager', 'researcher', 'analyst'):
            if settings.ai_paused():
                raise UserError('ai_paused')
            if not settings.agent_enabled(agent):
                raise UserError('agent_disabled', agent=f'@agent.{agent}')
            if not llm.configured(agent):
                raise UserError('agent_not_configured', agent=f'@agent.{agent}')
            llm.check_budget(agent, llm.get_config(agent))
            preparation._probe(agent)
        preparation._probe_decision()
        known = source_pool.urls()
        candidates = source_pool.bootstrap_urls(pid, exclude=known, nonce=key)
        crawler = Crawler(should_stop=settings.ai_paused, max_pages=5)
        crawler.cfg['render_js'] = True
        verified, reasons = [], []
        for url in candidates[:5]:
            if settings.ai_paused():
                raise UserError('ai_paused')
            try:
                page = crawler.fetch(url)
                if page['url'].rstrip('/') in {u.rstrip('/') for u in known}:
                    continue
                if source_pool.accept_bootstrap_page(pid, page):
                    verified.append(page['url'])
            except Skip as exc:
                reasons.append(exc.key)
        if not verified:
            raise UserError('source_preparation_failed', detail=', '.join(dict.fromkeys(reasons)) or 'no new verified sources')
        _save(key, phase='ready', urls=verified, error='')
        tick()
    except UserError as exc:
        _save(key, phase='blocked', error=exc.key, params=exc.params,
              environment_signature=preparation._environment_signature())
    except Exception as exc:
        _save(key, phase='blocked', error='manager_preparation_failed', params={'detail': type(exc).__name__},
              environment_signature=preparation._environment_signature())
    finally:
        with _lock:
            _active.discard(key)


def _chain(root):
    return db.query('WITH RECURSIVE chain(id) AS (SELECT ? UNION SELECT t.id FROM tasks t JOIN chain c ON t.parent_id=c.id) '
                    'SELECT t.* FROM tasks t JOIN chain c ON t.id=c.id ORDER BY t.created_at,t.id', (root['id'],))


@background.tracked('manager:single_retries')
def tick():
    if edition.current()['id'] != 'my_igaming' or settings.ai_paused():
        return
    for key, value in list(jobs().items()):
        if value['phase'] == 'ready':
            existing = db.query_one("SELECT id FROM tasks WHERE parent_id=? OR "
                                    "(created_by='manager_retry' AND goal_date=?) LIMIT 1", (key, key))
            if existing:
                _save(key, phase='dispatched', task_id=existing['id'])
        if value['phase'] in ('pending', 'expanding'):
            ensure(key, value['icp_id'])
    for row in db.query("SELECT * FROM tasks WHERE kind='research' AND created_by IN ('user','scheduler') "
                        "AND status IN ('done','failed') ORDER BY created_at DESC LIMIT 50"):
        params = db.loads(row['params'], {})
        limit = params.get('retry_count', 0)
        if (not limit or row.get('result') == 'target_reached' or
                (row.get('parent_id') and db.query_one('SELECT id FROM tasks WHERE id=?', (row['parent_id'],)))):
            continue
        chain = _chain(row)
        if any(t['status'] in ('running', 'queued', 'cancelled', 'stopped') for t in chain):
            continue
        attempts = sum(t['created_by'] == 'manager_retry' for t in chain)
        if attempts >= limit:
            continue
        ids = [t['id'] for t in chain]
        qualified = db.query_one('SELECT COUNT(*) n FROM leads WHERE qualified=1 AND task_id IN (' +
                                 ','.join('?' for _ in ids) + ')', ids)['n']
        remaining = row['target'] - qualified
        if remaining <= 0:
            continue
        key = f"single:{row['id']}:{attempts + 1}"
        expansion = ensure(key, row['icp_id'])
        if expansion['phase'] != 'ready':
            continue
        # Registration and the idempotency check share the normal task lock.
        with tasks.registry_lock():
            if db.query_one("SELECT id FROM tasks WHERE created_by='manager_retry' AND parent_id=? AND goal_date=?",
                            (row['id'], key)):
                continue
            try:
                dispatched = tasks.create_task({**params, 'sources': list(dict.fromkeys(params['sources'] + ['web'])),
                                   'target': remaining, 'urls': expansion['urls'], 'retry_count': 0},
                                  created_by='manager_retry', parent_id=row['id'], goal_date=key)
                _save(key, phase='dispatched', task_id=dispatched['id'])
            except UserError as exc:
                _save(key, phase='blocked', error=exc.key, params=exc.params)

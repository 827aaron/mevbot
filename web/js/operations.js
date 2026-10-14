/* Daily operations live in Task management and Channel pool, alongside existing tools. */
(function () {
  'use strict';
  const S = window.STATE, U = window.UI, A = window.ACTIONS, P = window.PAGES;
  const { esc, api, busy, toast, toastErr } = U;
  const dayNow = () => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); };
  const icpOptions = (icps, id) => (!icps.some((i) => i.id === +id) ? '<option value="" selected>' + t(icps.length ? 'ops.selectIcp' : 'ops.noIcpOption') + '</option>' : '') + icps.map((i) => '<option value="' + i.id + '"' + (+id === i.id ? ' selected' : '') + '>' + esc(i.name) + '</option>').join('');
  const statusText = (v) => U.has('ops.state.' + v) ? t('ops.state.' + v) : U.has('tstatus.' + v) ? t('tstatus.' + v) : esc(v || '—');
  const blockedText = (v) => U.has('ops.block.' + v) ? t('ops.block.' + v) : U.has('errors.' + v) ? t('errors.' + v) : esc(v);
  function notice(blocked, enabled, off, ready) {
    const stopped = blocked && blocked !== 'disabled';
    return '<div class="alert ' + (stopped ? 'alert-warning' : 'alert-info') + '"><span><b>' + t(stopped ? 'ops.blocked' : enabled ? ready : off) + '</b>' + (stopped ? '<br>' + blockedText(blocked) : '') + '</span></div>';
  }
  function goalFields(goal, icps, prefix) {
    return '<div class="form-grid"><div class="field"><label class="label" for="' + prefix + '-icp">' + t('tasks.template') + '</label><select class="select" id="' + prefix + '-icp" required>' + icpOptions(icps, goal.icp_id) + '</select></div>' +
      '<div class="field"><label class="label" for="' + prefix + '-target">' + t('tasks.target') + '</label><input class="input" type="number" min="1" max="500" step="1" required id="' + prefix + '-target" value="' + esc(goal.target || 10) + '"></div>' +
      '<div class="field"><label class="label" for="' + prefix + '-time">' + t('ops.time') + '</label><input class="input" type="time" required id="' + prefix + '-time" value="' + esc(goal.time_of_day || '09:00') + '"></div><div class="field"><label class="label" for="' + prefix + '-retries">' + t('ops.retries') + '</label><select class="select" id="' + prefix + '-retries">' + [0, 1, 2].map((n) => '<option value="' + n + '"' + (n === +goal.max_retries ? ' selected' : '') + '>' + t(n ? 'ops.retriesN' : 'ops.noRetries', { n }) + '</option>').join('') + '</select></div></div>' +
      '<fieldset class="ops-sources"><legend class="label">' + t('tasks.sources') + '</legend><div class="row wrap">' + (S.meta.sources || []).filter((k) => ['google', 'web', 'youtube', 'tg'].includes(k)).map((k) => '<label class="checkbox"><input type="checkbox" data-ops-source="' + k + '"' + ((goal.sources || []).includes(k) ? ' checked' : '') + '> ' + t('source.' + k) + '</label>').join('') + '</div></fieldset>';
  }
  function proposalHtml(p) {
    if (!p || p.status !== 'pending') return '';
    const g = p.proposal.goal;
    const icp = (S.opsIcps || []).find((x) => x.id === +g.icp_id);
    return '<div class="ops-proposal alert alert-warning" data-ops-proposal><div class="grow"><b>' + t('ops.pending') + '</b><p>' + esc(p.proposal.explanation || '') + '</p><p>' + esc(icp ? icp.name : '—') + ' · ' + t('tasks.target') + ': ' + esc(g.target) + ' · ' + esc(g.time_of_day) + ' · ' + (g.sources || []).map((k) => esc(t('source.' + k))).join(' / ') + '</p><p class="xs">' + t('ops.enabled') + ': ' + t(g.enabled ? 'c.on' : 'c.off') + ' · ' + t('ops.retries') + ': ' + esc(g.max_retries) + '</p><p class="xs">' + t('ops.nextDay') + '</p><div class="row wrap"><button class="btn btn-primary btn-sm" data-action="opsConfirm" data-id="' + esc(p.id) + '">' + t('ops.confirm') + '</button><button class="btn btn-ghost btn-sm" data-action="opsDismiss" data-id="' + esc(p.id) + '">' + t('ops.dismiss') + '</button></div></div></div>';
  }
  function reportHtml(report, plans) {
    if (!report && !(plans || []).length) return '<p class="muted small">' + t('ops.noPlans') + '</p>';
    return (report ? '<div class="row wrap ops-report-summary"><b>' + t('ops.progress', { done: report.qualified || 0, target: report.target || 0 }) + '</b><span class="tag">' + statusText(report.status) + '</span><span class="xs muted">' + t('ops.attempts', { n: report.attempts || 0 }) + '</span></div>' + (report.last_error ? '<div class="alert alert-warning"><span>' + blockedText(report.last_error) + '</span></div>' : '') + '<div class="row wrap xs muted">' + ['processed', 'backup', 'discarded', 'errors'].map((k) => esc(t('ops.count.' + k)) + ': ' + esc((report.counts || {})[k] || 0)).join(' · ') + '</div>' : '') +
      (plans || []).map((p) => '<div class="ops-plan"><div class="row wrap"><b>' + esc(p.day) + '</b><span class="tag">' + statusText(p.status) + '</span><span class="small">' + t('ops.progress', { done: p.qualified || 0, target: (p.goal || {}).target || 0 }) + '</span></div>' + '<p class="small muted">' + t('ops.snapshot') + ': ' + esc(((S.opsIcps || []).find((i) => i.id === (p.goal || {}).icp_id) || {}).name || '—') + ' · ' + esc((p.goal || {}).time_of_day || '—') + ' · ' + ((p.goal || {}).sources || []).map((k) => esc(t('source.' + k))).join(' / ') + '</p>' + (p.last_error ? '<p class="small text-2">' + blockedText(p.last_error) + '</p>' : '') + '<div class="row wrap" style="margin-top:8px">' + (p.tasks || []).map((task) => '<button class="btn btn-secondary btn-sm" data-action="go" data-route="run" data-q="task=' + encodeURIComponent(task.id) + '">' + t('ops.details') + ' · ' + statusText(task.status) + '</button>').join('') + '</div></div>').join('');
  }
  async function loadReport(root) {
    const input = root.querySelector('#ops-report-day'), box = root.querySelector('#ops-report-body');
    if (!input || !box || !input.reportValidity()) return;
    const day = input.value;
    const [report, plans] = await Promise.all([api('GET', '/api/manager/report?day=' + encodeURIComponent(day)), api('GET', '/api/manager/plan?day=' + encodeURIComponent(day))]);
    if (box.isConnected && input.value === day) box.innerHTML = reportHtml(report, plans);
  }
  function managerHtml(state, icps) {
    return '<section class="card ops-manager" id="ops-manager"><div class="card-header"><h3>' + U.avatar('manager', 'sm') + t('ops.title') + '</h3><div class="actions"><button class="btn btn-secondary btn-sm" data-action="opsCommand">' + icon('message') + t('ops.command') + '</button></div></div><div class="card-body"><p class="small text-2" style="margin-bottom:12px">' + t('ops.sub') + '</p><div id="ops-manager-state">' + notice(state.blocked, state.goal.enabled, 'ops.off', 'ops.ready') + '</div><form id="ops-goal-form"><label class="checkbox" style="margin:14px 0"><input type="checkbox" id="ops-enabled"' + (state.goal.enabled ? ' checked' : '') + '> ' + t('ops.enabled') + '</label>' + goalFields(state.goal, icps, 'ops') + '<p class="hint">' + t('ops.nextDay') + '</p><div class="row wrap" style="margin-top:14px"><button type="submit" class="btn btn-primary" id="ops-goal-save">' + t('c.save') + '</button></div></form><div id="ops-proposal-wrap" style="margin-top:14px">' + proposalHtml(S.opsProposal) + '</div></div></section><section class="card" style="margin:16px 0" id="ops-report"><div class="card-header"><h3>' + icon('calendar') + t('ops.report') + '</h3><div class="actions"><label class="xs" for="ops-report-day">' + t('ops.date') + '</label><input class="input input-sm" type="date" required id="ops-report-day" value="' + dayNow() + '"></div></div><div class="card-body" id="ops-report-body">' + U.skeleton(2) + '</div></section>';
  }
  const runRender = P.run.render, runMount = P.run.mount;
  P.run.render = async function (params) {
    const existing = await runRender.call(this, params);
    if (params.task) return existing;
    try {
      const [state, icps] = await Promise.all([api('GET', '/api/manager/state'), api('GET', '/api/icps')]);
      S.opsState = state; S.opsIcps = icps; S.opsProposal = (state.commands || []).find((p) => p.status === 'pending') || null;
      const headerEnd = existing.indexOf('<div class="grid grid-2"');
      return existing.slice(0, headerEnd) + managerHtml(state, icps) + '<h2 style="margin-bottom:6px">' + t('ops.manual') + '</h2><p class="small muted" style="margin-bottom:12px">' + t('ops.manualHint') + '</p>' + existing.slice(headerEnd);
    } catch (e) { return '<div class="alert alert-warning"><span>' + t('ops.unavailable') + '</span><button class="btn btn-secondary btn-sm" data-action="opsRetry">' + t('ops.retry') + '</button></div>' + existing; }
  };
  P.run.mount = async function (root, params) {
    await runMount.call(this, root, params);
    const form = root.querySelector('#ops-goal-form'); if (!form) return;
    const syncRequired = () => { form.querySelector('#ops-icp').required = form.querySelector('#ops-enabled').checked; };
    form.querySelector('#ops-enabled').onchange = syncRequired; syncRequired();
    form.onsubmit = async (event) => {
      event.preventDefault();
      const sources = [...form.querySelectorAll('[data-ops-source]:checked')].map((x) => x.dataset.opsSource);
      if (!sources.length) { toast(t('ops.invalidSources'), 'error'); return; }
      const data = { enabled: form.querySelector('#ops-enabled').checked, icp_id: form.querySelector('#ops-icp').value ? +form.querySelector('#ops-icp').value : null, target: +form.querySelector('#ops-target').value, time_of_day: form.querySelector('#ops-time').value, max_retries: +form.querySelector('#ops-retries').value, sources };
      try { await busy(form.querySelector('#ops-goal-save'), () => api('PUT', '/api/manager/goal', data)); toast(t('ops.applied')); S.opsProposal = null; U.refresh(); } catch (e) { toastErr(e); }
    };
    root.querySelector('#ops-report-day').onchange = () => loadReport(root).catch(toastErr);
    await loadReport(root).catch(toastErr);
    U.every(5000, async () => {
      try { const state = await api('GET', '/api/manager/state'); const box = root.querySelector('#ops-manager-state'); if (box && box.isConnected) box.innerHTML = notice(state.blocked, state.goal.enabled, 'ops.off', 'ops.ready'); await loadReport(root); } catch (e) { /* Keep the last report visible during a transient failure. */ }
    });
    if (params.command) A.opsCommand();
  };
  A.opsRetry = () => U.refresh();
  A.opsCommand = async () => {
    try { const [icps, state] = await Promise.all([api('GET', '/api/icps'), api('GET', '/api/manager/state')]); S.opsIcps = icps; S.opsProposal = (state.commands || []).find((p) => p.status === 'pending') || null; } catch (e) { toastErr(e); return; }
    const w = U.openModal({ title: t('ops.command'), body: '<p class="small text-2" style="margin-bottom:12px">' + t('ops.commandHint') + '</p><textarea class="textarea" id="ops-command-input" maxlength="2000" placeholder="' + esc(t('ops.commandPlaceholder')) + '"></textarea><div id="ops-command-result" style="margin-top:14px">' + proposalHtml(S.opsProposal) + '</div>', footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="ops-command-send">' + t('ops.propose') + '</button>' });
    w.querySelector('#ops-command-send').onclick = async (e) => {
      const input = w.querySelector('#ops-command-input'); if (!input.value.trim()) { input.focus(); return; }
      try { S.opsProposal = await busy(e.currentTarget, () => api('POST', '/api/manager/command', { message: input.value.trim() })); w.querySelector('#ops-command-result').innerHTML = proposalHtml(S.opsProposal); } catch (err) { toastErr(err); }
    };
  };
  A.opsConfirm = async (el) => {
    try { await busy(el, () => api('POST', '/api/manager/command/' + encodeURIComponent(el.dataset.id) + '/confirm', {})); S.opsProposal = null; U.closeModal(); toast(t('ops.applied')); if (S.route === 'run') U.refresh(); else U.go('run'); } catch (e) { toastErr(e); }
  };
  A.opsDismiss = async (el) => {
    try { await busy(el, () => api('POST', '/api/manager/command/' + encodeURIComponent(el.dataset.id) + '/reject', {})); S.opsProposal = null; document.querySelectorAll('[data-ops-proposal]').forEach((x) => x.remove()); } catch (e) { toastErr(e); }
  };

  function scoutHtml(settings, status, icps) {
    return '<section class="card ops-scout" id="ops-scout" style="margin-bottom:16px"><div class="card-header"><h3>' + U.avatar('scout', 'sm') + t('scout.title') + '</h3></div><div class="card-body"><p class="small text-2" style="margin-bottom:12px">' + t('scout.sub') + '</p><div id="ops-scout-status">' + scoutStatus(status, settings) + '</div><form id="scout-form"><label class="checkbox" style="margin:14px 0"><input type="checkbox" id="scout-enabled"' + (settings.enabled ? ' checked' : '') + '> ' + t('scout.enabled') + '</label><div class="form-grid"><div class="field"><label class="label" for="scout-icp">' + t('tasks.template') + '</label><select class="select" id="scout-icp" required>' + icpOptions(icps, settings.icp_id) + '</select></div><div class="field"><label class="label" for="scout-time">' + t('ops.time') + '</label><input class="input" type="time" required id="scout-time" value="' + esc(settings.time_of_day || '10:00') + '"></div></div><label class="checkbox"><input type="checkbox" id="scout-auto-queue"' + (settings.auto_queue ? ' checked' : '') + '> ' + t('scout.autoQueue') + '</label><p class="hint">' + t('scout.queueHint') + '</p><label class="checkbox" style="margin-top:14px"><input type="checkbox" id="scout-auto-retire"' + (settings.auto_retire ? ' checked' : '') + '> ' + t('scout.autoRetire') + '</label><p class="hint">' + t('scout.retireHint') + '</p><button type="submit" class="btn btn-primary" id="scout-save" style="margin-top:14px">' + t('c.save') + '</button></form></div></section>';
  }
  function scoutStatus(status, settings) {
    const last = (status.runs || [])[0], r = (last || {}).result || {};
    return notice(status.blocked, settings.enabled, 'scout.off', 'scout.ready') + '<div class="row wrap xs muted" style="margin-top:10px"><span>' + t('scout.last') + ': ' + (last ? esc(last.day) + ' · ' + statusText(last.status) : t('scout.none')) + '</span>' + (last ? '<span>' + t('scout.summary', { added: r.added || 0, queued: r.queued || 0, retired: (r.retired || []).length }) + '</span>' : '') + '</div>' + (last && last.error ? '<p class="small text-2">' + blockedText(last.error) + '</p>' : '') + ((status.suggestions || []).length ? '<p class="xs muted">' + t('scout.retireSuggestions', { n: status.suggestions.length }) + '</p>' : '');
  }
  const channelRender = P.channels.render, channelMount = P.channels.mount;
  P.channels.render = async function (params) {
    const existing = await channelRender.call(this, params);
    try {
      const [settings, status, icps] = await Promise.all([api('GET', '/api/scout/settings'), api('GET', '/api/scout/status'), api('GET', '/api/icps')]);
      S.scoutSettings = settings;
      const pos = existing.indexOf('<div class="tabs');
      if (pos >= 0) return existing.slice(0, pos) + scoutHtml(settings, status, icps) + existing.slice(pos);
      return scoutHtml(settings, status, icps) + existing;
    } catch (e) { return '<div class="alert alert-warning"><span>' + t('scout.unavailable') + '</span><button class="btn btn-secondary btn-sm" data-action="opsRetry">' + t('ops.retry') + '</button></div>' + existing; }
  };
  P.channels.mount = async function (root, params) {
    if (channelMount) await channelMount.call(this, root, params);
    const form = root.querySelector('#scout-form'); if (!form) return;
    const syncRequired = () => { form.querySelector('#scout-icp').required = form.querySelector('#scout-enabled').checked; };
    form.querySelector('#scout-enabled').onchange = syncRequired; syncRequired();
    form.onsubmit = async (event) => {
      event.preventDefault();
      const data = { enabled: form.querySelector('#scout-enabled').checked, icp_id: form.querySelector('#scout-icp').value ? +form.querySelector('#scout-icp').value : null, time_of_day: form.querySelector('#scout-time').value, auto_queue: form.querySelector('#scout-auto-queue').checked, auto_retire: form.querySelector('#scout-auto-retire').checked };
      const save = async () => { try { await busy(form.querySelector('#scout-save'), () => api('PUT', '/api/scout/settings', data)); toast(t('c.saved')); U.refresh(); } catch (e) { toastErr(e); } };
      if (data.auto_queue) {
        const q = await api('GET', '/api/tg/join').catch((e) => { toastErr(e); return null; }); if (!q) return;
        if (!S.scoutSettings.auto_queue || !q.assisted) {
          const risks = [1, 2, 3].map((i) => 'riskf.assisted_join.r' + i).filter(U.has);
          const accepted = await U.confirmDialog({ title: t('scout.queueTitle'), body: t('scout.queueRisk'),
            extra: '<ul class="risk-list">' + risks.map((k) => '<li>' + icon('alert') + '<span>' + t(k) + '</span></li>').join('') + '</ul><p class="xs muted">' + t('risk.safeguard') + '</p><label class="checkbox risk-confirm"><input type="checkbox" id="scout-risk-agree" required> ' + t('risk.agree') + '</label>',
            validate: (w) => w.querySelector('#scout-risk-agree').reportValidity(),
            onOk: async () => { data.confirmed = true; await api('PUT', '/api/scout/settings', data); },
          });
          if (accepted) { toast(t('c.saved')); U.refresh(); }
          return;
        }
      }
      await save();
    };
    U.every(10000, async () => { try { const status = await api('GET', '/api/scout/status'); const box = root.querySelector('#ops-scout-status'); if (box && box.isConnected) box.innerHTML = scoutStatus(status, S.scoutSettings); } catch (e) { /* Keep the last status. */ } });
  };
})();

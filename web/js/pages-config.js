/* 页面（三）：账号池（TG 账号 / TG 群 / 邮箱 / 平台登录）、TG 群管理（隐藏路由）、设置（基本 + 收起的高级）、快速开始（全屏 5 步） */
(function () {
  'use strict';
  const S = window.STATE, U = window.UI, A = window.ACTIONS, P = window.PAGES;
  const { esc, avatar, sw, empty, money, api, toast, toastErr, busy } = U;
  const AG = () => U.agentKeys();   // v1：不含销售员 / 跟进员（后端 FEATURES["outreach"]）

  // Shared company editor: confirmation and later corrections use the same fields.
  const COMPANY_FIELDS = ['name', 'summary', 'oem_odm', 'advantages', 'certifications', 'factory', 'cases', 'markets', 'moq', 'price_range', 'payment_terms', 'lead_time', 'competitors'];
  const companySources = (v) => Array.isArray((v || {}).sources) ? v.sources.filter((x) => typeof x === 'string') : [];
  const companySourceChips = (sources) => [...new Set(sources)].map((src) => {
    if (src === 'interview') return '<span class="src-chip">' + icon('message') + t('ob.srcInterview') + '</span>';
    if (src.startsWith('input:')) return '<span class="src-chip">' + icon('user') + t('ob.srcYou') + '</span>';
    if (src.startsWith('pdf:')) return '<span class="src-chip">' + icon('file') + esc(src.slice(4)) + '</span>';
    if (/^https?:\/\//i.test(src)) return '<a class="src-chip" href="' + esc(src) + '" target="_blank" rel="noopener noreferrer">' + icon('link') + esc(src) + '</a>';
    return '';
  }).join(' ');
  const companyText = (co, k) => typeof co[k] === 'string' ? co[k] : (co[k] || {}).text || '';
  U.companyForm = (co) => '<div class="company-form"><p class="xs muted">' + t('company.editHint') + '</p>' +
    COMPANY_FIELDS.map((k) => '<div class="field"><label class="label">' + t(k === 'name' ? 'company.name' : 'qs.f.' + k) + (k === 'summary' ? ' <span class="req">*</span>' : '') +
      '<textarea class="textarea" data-company-field="' + k + '" maxlength="2000"' + (k === 'summary' ? ' required' : '') + ' style="min-height:62px">' + esc(companyText(co, k)) + '</textarea></label>' +
      companySourceChips(companySources(co[k])) + '</div>').join('') +
    '<div class="field"><label class="label">' + t('qs.f.product_lines') + '<textarea class="textarea" data-company-field="product_lines" style="min-height:84px">' + esc((co.product_lines || []).map((p) => p.name).join('\n')) + '</textarea></label><span class="hint">' + t('company.productsHint') + '</span>' + companySourceChips((co.product_lines || []).flatMap(companySources)) + '</div></div>';
  U.companyPatch = (root, co) => {
    const patch = {};
    COMPANY_FIELDS.forEach((k) => { const v = U.$('[data-company-field="' + k + '"]', root).value.trim(); if (v !== companyText(co, k)) patch[k] = v; });
    const products = U.$('[data-company-field="product_lines"]', root).value.split('\n').map((v) => v.trim()).filter(Boolean);
    if (JSON.stringify(products) !== JSON.stringify((co.product_lines || []).map((p) => p.name))) patch.product_lines = products;
    return patch;
  };
  U.companyUpdated = (co, patch) => {
    const next = Object.assign({}, co);
    COMPANY_FIELDS.forEach((k) => { if (k in patch) next[k] = Object.assign({}, typeof co[k] === 'object' ? co[k] : {}, { text: patch[k], sources: [...new Set(['input:customer'].concat(companySources(co[k])))] }); });
    if (patch.product_lines) next.product_lines = patch.product_lines.map((name) => { const old = (co.product_lines || []).find((p) => p.name === name) || {}; return Object.assign({}, old, { name, sources: [...new Set(['input:customer'].concat(companySources(old)))] }); });
    return next;
  };
  U.companyValid = (root) => {
    const summary = U.$('[data-company-field="summary"]', root);
    summary.setCustomValidity(summary.value.trim() ? '' : t('company.summaryRequired'));
    if (!summary.reportValidity()) return false;
    const products = U.$('[data-company-field="product_lines"]', root);
    const lines = products.value.split('\n').map((v) => v.trim()).filter(Boolean);
    products.setCustomValidity(lines.length > 50 || lines.some((v) => v.length > 200) ? t('company.productsLimit') : '');
    return products.reportValidity();
  };

  /* =================================================================
     ⑦ 账号池
     ================================================================= */
  const TG_ST = { active: 'dot-ok', new: 'dot-off', code_sent: 'dot-warn', need_password: 'dot-warn', need_relogin: 'dot-error', restricted: 'dot-error', flood_wait: 'dot-warn', disabled: 'dot-off' };
  const stars = (n) => '<span class="stars">' + '★★★★★'.slice(0, n) + '<span class="muted">' + '★★★★★'.slice(n) + '</span></span>';
  P.accounts = {
    async render() {
      const tab = ['tg', 'groups'].includes(S.tabs.accounts) ? S.tabs.accounts : 'tg';
      S.tabs.accounts = tab; // Old mail/platform links fall back to the usable TG page.
      const [d, cfg, pool] = await Promise.all([api('GET', '/api/tg/accounts'), api('GET', '/api/tg/config'), api('GET', '/api/source-pool')]);
      S.socialPlatforms = pool.platforms || [];
      S.accData = d; S.tgCfg = cfg;
      let body;
      if (tab === 'tg' || tab === 'groups') body = tgTab(d, cfg);
      else if (tab === 'mail') body = '<div class="card">' + empty({ icon: 'mail', title: t('acc2.mailSoon'), body: t('acc.mailSub') }) + '</div>';
      else body = '<p class="small text-2" style="margin-bottom:12px">' + icon('shield') + ' ' + t('acc.pSub') + '</p><div class="card">' + empty({ icon: 'globe', title: t('acc2.platformSoon'), body: t('acc2.platformSoonSub') }) + '</div>';
      return '<div class="page-head"><div><h1>' + t('acc.title') + '</h1><p>' + t('acc.sub') + '</p></div><div class="row"><button class="btn btn-primary" data-action="addTg">' + icon('plus') + t('acc.addTg') + '</button></div></div>' +
        d.warnings.map((w) => '<div class="alert alert-warning">' + icon('alert') + '<span>' + t('warn.' + w.key, w.params) + '</span></div>').join('') +
        U.tabs('accounts', [['tg', t('acc.tg'), 'telegram', d.accounts.length]], 'tg') + body + platformCards(S.socialPlatforms);
    },
  };
  P.accounts.mount = (root) => U.every(3000, async () => {
    if (!(S.socialPlatforms || []).some((p) => ['starting', 'open'].includes(p.status))) return;
    try { S.socialPlatforms = await Promise.all(SOCIAL_PLATFORM_IDS.map((id) => api('GET', '/api/social/' + id + '/status'))); const box = U.$('#social-platform-cards', root); if (box && box.isConnected) box.outerHTML = platformCards(S.socialPlatforms); } catch (e) { /* Preserve the last known platform status. */ }
  });
  function tgTab(d, cfg) {
    const configured = cfg.backend === 'mock' || (cfg.api_id && cfg.api_hash_set);
    const cred = (!configured || S.tgCredOpen)
      ? '<div class="card" style="margin-bottom:16px"><div class="card-header"><h3>' + icon('key') + t('acc.tg_api') + '</h3><span class="sub">' + t('acc.tg_api_sub') + '</span></div><div class="card-body"><div class="form-grid">' +
        '<div class="field"><label class="label">api_id</label><input class="input mono" id="tg-id" value="' + esc(cfg.api_id || '') + '" placeholder="1234567"></div>' +
        '<div class="field"><label class="label">api_hash ' + (cfg.api_hash_set ? '<span class="tag tag-success">' + t('models.key_saved') + '</span>' : '') + '</label><input class="input mono" type="password" id="tg-hash" placeholder="' + esc(cfg.api_hash_set ? t('models.key_keep') : 'abcdef0123…') + '"></div>' +
        '<div class="field span-2"><span class="hint">' + t('acc.tg_api_hint') + '</span></div>' + (cfg.dev || cfg.backend === 'mock' ? '<label class="checkbox small span-2"><input type="checkbox" id="tg-mock" ' + (cfg.backend === 'mock' ? 'checked' : '') + '>' + t('acc.tg_mock') + '</label>' : '') + '</div></div>' +
        '<div class="card-footer" style="justify-content:flex-end"><button class="btn btn-primary btn-sm" data-action="tgCfgSave">' + t('c.save') + '</button></div></div>'
      : '<div class="row small text-2" style="margin-bottom:12px">' + icon('key') + t('acc.tg_api') + '：' + (cfg.backend === 'mock' ? '<span class="tag tag-warning">' + t('models.test_only') + '</span>' : '<span class="mono">api_id ' + esc(cfg.api_id) + '</span> <span class="tag tag-success">' + t('models.key_saved') + '</span>') + '<button class="btn btn-ghost btn-sm" data-action="tgCredOpen">' + icon('edit') + t('c.edit') + '</button></div>';
    const row = (a) => '<div class="plan-item" style="align-items:center"><span class="text-2">' + icon(a.role === 'notify' ? 'bell' : 'telegram', 'lg') + '</span><div class="body"><div class="title"><span class="mono">' + esc(a.phone) + '</span>' +
      '<span class="status"><span class="dot ' + (TG_ST[a.status] || 'dot-idle') + '"></span>' + t('tgs.' + a.status) + '</span>' + (a.role === 'notify' ? '<span class="tag tag-primary">' + icon('bell') + t('acc.r.notify') + '</span>' : '') + (a.in_warmup ? '<span class="tag">' + t('acc4.warmLeft', { n: a.warmup_left_days || 1 }) + '</span>' : '') + '</div>' +
      '<div class="desc xs">' + [a.label ? esc(a.label) : '', a.tg_user && a.tg_user.username ? '@' + esc(a.tg_user.username) : '', a.role === 'notify' ? '' : '<a href="#/channels/joined" data-action="grpOfAcc" data-id="' + a.id + '" title="' + esc(t('grp3.seeGroups')) + '">' + t('acc.groupsN', { n: a.chats }) + '</a> · ' + t('acc2.monitored', { n: a.monitored }), a.proxy && a.proxy.host ? t('acc.proxyOk') + ' ' + esc(a.proxy.host) : t('acc.proxyNo'),
        t('acc2.today', { join: a.today.join || 0, jl: a.in_warmup ? Math.min(1, a.daily_join_limit) : a.daily_join_limit, send: a.today.send || 0, sl: a.daily_send_limit }),
        a.role === 'notify' || !a.caps ? '' : t('acc2.capsToday', { search: a.today.search || 0, sl: a.caps.search, preview: a.today.preview || 0, pl: a.caps.preview, read: a.today.read || 0, rl: a.caps.read }),
        a.holding ? t('acc2.holding', { agent: t('agent.' + a.holding) }) : '', a.status === 'flood_wait' && a.flood_until ? t('acc.flood_until', { time: U.fmtTime(a.flood_until) }) : ''].filter(Boolean).join(' · ') + '</div>' +
      (syncFailed(a) ? '<div class="xs" style="color:var(--color-warning)">' + icon('alert') + ' ' + esc(t('acc4.syncFailed', { reason: syncFailed(a) })) + '</div>' : '') + '</div>' +
      '<button class="btn btn-secondary btn-sm" data-action="tgDetail" data-id="' + a.id + '">' + icon('eye') + t('acc4.detail') + '</button>' +
      (a.in_warmup ? '<button class="btn btn-ghost btn-sm" data-action="tgSkipWarm" data-id="' + a.id + '">' + t('acc4.skipWarm') + '</button>' : '') +
      (['new', 'need_relogin', 'code_sent', 'need_password'].includes(a.status) ? '<button class="btn btn-primary btn-sm" data-action="tgLogin" data-id="' + a.id + '">' + t('acc.loginBtn') + '</button>' : '') +
      (a.status === 'disabled' ? '<button class="btn btn-secondary btn-sm" data-action="tgResume" data-id="' + a.id + '">' + icon('play') + t('acc5.resume') + '</button>' : '') +
      (a.status === 'active' ? '<button class="btn btn-ghost btn-sm" data-action="tgSync" data-id="' + a.id + '">' + icon('refresh') + t('acc.sync') + '</button><button class="btn btn-ghost btn-sm" data-action="tgCheck" data-id="' + a.id + '">' + t('acc2.check') + '</button>' : '') +
      '<button class="btn btn-ghost btn-icon btn-sm" data-action="tgEdit" data-id="' + a.id + '" title="' + esc(t('c.edit')) + '">' + icon('settings') + '</button><button class="btn btn-ghost btn-icon btn-sm" data-action="tgDel" data-id="' + a.id + '" title="' + esc(t('c.delete')) + '">' + icon('trash') + '</button></div>';
    return cred + '<div class="card">' + (d.accounts.length ? d.accounts.map(row).join('') : empty({ icon: 'users', title: t('acc.empty'), body: t('acc.empty_sub'), actions: '<button class="btn btn-primary" data-action="addTg">' + icon('plus') + t('acc.addTg') + '</button>' })) + '</div>' +
      '<p class="xs muted" style="margin-top:10px">' + icon('info') + ' ' + t('acc.safety_hint') + '</p>';
  }
  A.tgCredOpen = () => { S.tgCredOpen = true; U.refresh(); };
  A.tgCfgSave = async (el) => {
    const mk = U.$('#tg-mock');
    await busy(el, () => api('PUT', '/api/tg/config', { api_id: U.$('#tg-id').value.trim(), api_hash: U.$('#tg-hash').value.trim(), backend: mk && mk.checked ? 'mock' : 'telethon' }));
    S.tgCredOpen = false; toast(t('c.saved')); U.refresh();
  };
  A.addTg = () => accountDialog();
  A.tgEdit = (el) => accountDialog(S.accData.accounts.find((a) => a.id === +el.dataset.id));
  A.tgDel = async (el) => { if (await U.confirmDialog({ title: t('acc.del_title'), body: t('acc.del_body'), ok: t('c.delete'), danger: true, onOk: () => api('DELETE', '/api/tg/accounts/' + el.dataset.id) })) U.refresh(); };
  A.tgLogin = (el) => {
    if (!tgCredsOk()) { S.tgCredOpen = true; toast(t('acc3.needCreds'), 'warn'); U.refresh(); return; }   // 没有凭证时不打开登录（会报 tg_not_configured）
    loginDialog(S.accData.accounts.find((a) => a.id === +el.dataset.id));
  };
  A.tgSync = async (el) => { const r = await busy(el, () => api('POST', '/api/tg/accounts/' + el.dataset.id + '/sync')); toast(t('acc4.synced', { total: r.total, added: r.added }) + (r.left ? ' ' + t('acc4.syncedLeft', { n: r.left }) : '')); U.refresh(); };
  const syncFailed = (a) => (a.status === 'active' && (a.status_detail || '').startsWith('sync_failed')) ? (a.status_detail.slice(12).trim() || '?') : '';
  /* 跳过养号（PRD 6.3）：客户确认风险后立刻结束养号 */
  A.tgSkipWarm = async (el) => {
    const id = +el.dataset.id;
    if (await U.confirmDialog({ title: t('acc4.skipWarmTitle'), body: t('acc4.skipWarmBody'), ok: t('acc4.skipWarm'), onOk: () => api('POST', '/api/tg/accounts/' + id + '/skip-warmup') })) {
      toast(t('acc4.skipWarmDone')); U.closeModal(); U.refresh();
    }
  };
  const membersTxt = (c) => (!c.info_at && !c.members) ? '<span class="muted" title="' + esc(t('acc4.membersPending')) + '">—</span>' : U.fmtNum(c.members);
  /* ---- 账号「详情」（PRD 6.4.1）：只看这个账号自己的信息。打开时通过账号调度现读一次 get_me（和「检查状态」一样）；
     暂停中 / 未登录的号不去读。详情里可以暂停使用 / 恢复使用 / 退出登录 */
  const hasNotify = (x) => x.role === 'notify' || (x.roles || []).includes('notify');
  function detailDialog(a, note) {
    const paused = a.status === 'disabled';
    const kv = (k, v) => '<span class="k">' + k + '</span><span>' + v + '</span>';
    const info = (a) => { const u = a.tg_user || {}; return '<div class="kv small">' +
      kv(t('acc4.tgName'), esc(u.name || '—')) + kv(t('acc4.username'), u.username ? '@' + esc(u.username) : '—') +
      kv(t('acc.phone'), '<span class="mono">' + esc(a.phone) + '</span>') + kv(t('acc5.userId'), u.id ? '<span class="mono">' + esc(String(u.id)) + '</span>' : '—') +
      kv(t('acc4.status'), '<span class="status"><span class="dot ' + (TG_ST[a.status] || 'dot-idle') + '"></span>' + t('tgs.' + a.status) + '</span>' +
        (a.in_warmup ? ' · ' + t('acc4.warmLeft', { n: a.warmup_left_days || 1 }) + ' <button class="btn btn-ghost btn-sm" data-action="tgSkipWarm" data-id="' + a.id + '">' + t('acc4.skipWarm') + '</button>' : '')) +
      kv(t('acc4.lastOk'), a.last_ok_at ? esc(U.fmtTime(a.last_ok_at)) : '—') + '</div>'; };
    const box = (cls, ic, txt) => '<div class="alert ' + cls + '" style="margin-bottom:10px">' + icon(ic) + '<span>' + esc(txt) + '</span></div>';
    const live = a.logged_in && !paused;
    const w = U.openModal({ title: t('acc4.detailTitle', { phone: a.phone }), sub: '',
      body: (note ? box(note.cls, note.icon, note.text) : '') +
        (paused ? box('alert-warning', 'pause', t('acc5.pausedNote')) : '') +
        (!a.logged_in && !paused ? box('alert-info', 'info', t('acc5.notLoggedIn')) : '') +
        (syncFailed(a) ? box('alert-warning', 'alert', t('acc4.syncFailed', { reason: syncFailed(a) })) : '') +
        '<div id="ad-live" class="xs muted" style="margin-bottom:8px">' + (live ? '<span class="spinner" style="width:12px;height:12px;border-width:2px"></span> ' + t('acc5.reading') : '') + '</div>' +
        '<div id="ad-info">' + info(a) + '</div>',
      footer: (a.logged_in ? '<button class="btn btn-ghost" style="color:var(--color-danger);margin-right:auto" data-action="tgLogout" data-id="' + a.id + '">' + t('acc.logout') + '</button>' : '') +
        (paused ? '<button class="btn btn-primary" data-action="tgResume" data-id="' + a.id + '">' + icon('play') + t('acc5.resume') + '</button>'
          : (a.logged_in ? '<button class="btn btn-secondary" data-action="tgPause" data-id="' + a.id + '">' + icon('pause') + t('acc5.pause') + '</button>' : '')) +
        '<button class="btn btn-secondary" data-action="closeModal">' + t('c.close') + '</button>' });
    if (!live) return;
    api('GET', '/api/tg/accounts/' + a.id + '/info').then((r) => {
      if (!w.isConnected) return;
      U.$('#ad-live', w).textContent = t('acc5.readOk');
      U.$('#ad-info', w).innerHTML = info(Object.assign({}, a, { status: r.status, last_ok_at: r.last_ok_at, tg_user: r.user }));
      U.refresh();
    }).catch((e) => {
      if (!w.isConnected) return;
      U.$('#ad-live', w).outerHTML = box('alert-warning', 'alert', t('acc5.readFailed', { reason: U.errText(e) }));
      U.refresh();
    });
  }
  const accOf = (el) => S.accData.accounts.find((x) => x.id === +el.dataset.id);
  A.tgDetail = (el) => detailDialog(accOf(el));
  /* 暂停使用：保留登录；调度不再借出（不读群、不加群、不发通知）。是唯一的通知号时在确认框里提醒 */
  A.tgPause = async (el) => {
    const a = accOf(el);
    const onlyNotify = hasNotify(a) && !S.accData.accounts.some((x) => x.id !== a.id && hasNotify(x) && x.status === 'active');
    let r;
    if (await U.confirmDialog({ title: t('acc5.pauseTitle'), body: t('acc5.pauseBody'), ok: t('acc5.pause'),
      extra: onlyNotify ? '<div class="alert alert-warning" style="margin-top:10px">' + icon('bell') + '<span>' + esc(t('acc5.pauseNotify')) + '</span></div>' : '',
      onOk: async () => { r = await api('PUT', '/api/tg/accounts/' + a.id, { disabled: true }); } })) {
      toast(t('acc5.paused')); detailDialog(r); U.refresh();
    } else detailDialog(a);
  };
  /* 恢复使用：后端先现读一次 get_me，能用才改回正常；读失败把原因写在详情里 */
  A.tgResume = async (el) => {
    const a = accOf(el);
    try {
      const r = await busy(el, () => api('PUT', '/api/tg/accounts/' + a.id, { disabled: false }));
      toast(t('acc5.resumed'));
      if (U.$('#modal')) detailDialog(r);
    } catch (e) {
      if (U.$('#modal')) detailDialog(a, { cls: 'alert-warning', icon: 'alert', text: t('acc5.resumeFailed', { reason: U.errText(e) }) });
      else toastErr(e);
    }
    U.refresh();
  };
  /* 退出登录：真正在 TG 退出（手机「设备」里消失）；群列表保留。TG 那边没退成 → 提醒去手机上手动结束 */
  A.tgLogout = async (el) => {
    const a = accOf(el);
    let r;
    if (await U.confirmDialog({ title: t('acc5.logoutTitle'), body: t('acc5.logoutBody'), ok: t('acc.logout'), danger: true,
      onOk: async () => { r = await api('POST', '/api/tg/accounts/' + a.id + '/logout'); } })) {
      if (r.remote) { toast(t('acc5.loggedOut')); detailDialog(r.account); }
      else detailDialog(r.account, { cls: 'alert-warning', icon: 'alert', text: t('acc5.logoutRemoteFailed') });
      U.refresh();
    } else detailDialog(a);
  };
  A.tgCheck = async (el) => { await busy(el, () => api('POST', '/api/tg/accounts/' + el.dataset.id + '/check')); U.refresh(); };
  A.grpMon = async (el) => {
    const on = el.type === 'checkbox' ? el.checked : el.querySelector('input').checked;
    try { await api('POST', '/api/tg/chats/monitor', { ids: [+el.dataset.id], on }); toast(on ? t('grp5.monOn') : t('grp.mon_off')); } catch (e) { toastErr(e); }
    if (U.$('#grp-list')) U.refresh();      // 渠道池：频道连同讨论区、同一个群只由一个账号监控 → 重新取一次，计数跟着变
  };
  A.monRun = async (el) => { const r = await busy(el, () => api('POST', '/api/monitor/run')); U.go('run', { task: r.id }); };
  A.grpFind = async (el) => {
    const icps = await api('GET', '/api/icps');
    if (!icps.length) { toast(t('icp.empty'), 'warn'); return; }
    const r = await busy(el, () => api('POST', '/api/tg/discover', { icp_id: icps[0].id }));
    toast(t('grp.disc_done', { n: r.added })); U.refresh();
  };
  A.grpJoin = async (el) => {
    const q = await api('GET', '/api/tg/join');
    const doIt = async () => { const r = await api('POST', '/api/tg/discoveries/approve', { ids: [+el.dataset.id] }); toast(t('grp.queued', { n: r.queued })); U.refresh(); };
    if (q.assisted) return doIt();
    U.riskDialog('assisted_join', doIt);
  };

  const tgCredsOk = () => { const c = S.tgCfg || {}; return c.backend === 'mock' || !!(c.api_id && c.api_hash_set); };
  /* 还没填 api_id / api_hash（PRD 6.1：客户在 my.telegram.org 自行申请）：添加账号的弹窗里先填这两项，附三步说明 */
  const credStep = () => '<div class="row small" style="font-weight:600;gap:6px;margin-bottom:4px">' + icon('key') + t('acc3.credTitle') + '</div><p class="xs muted" style="margin:0 0 8px">' + t('acc3.credWhy') + '</p>' +
    '<ol class="small" style="margin:0 0 10px 18px;line-height:1.7"><li>' + t('acc3.how1') + '</li><li>' + t('acc3.how2') + '</li><li>' + t('acc3.how3') + '</li></ol>' +
    '<div class="form-grid" style="margin-bottom:14px"><div class="field"><label class="label">App api_id <span class="req">*</span></label><input class="input mono" id="a-tgid" placeholder="1234567" autocomplete="off"></div>' +
    '<div class="field"><label class="label">App api_hash <span class="req">*</span></label><input class="input mono" type="password" id="a-tghash" placeholder="abcdef0123…" autocomplete="off"></div></div>' +
    '<div class="row small" style="font-weight:600;gap:6px;margin-bottom:6px">' + icon('telegram') + t('acc3.phoneStep') + '</div>';
  function accountDialog(a) {
    const p = (a && a.proxy) || {}, dv = (a && a.device) || {};
    const needCreds = !a && !tgCredsOk();
    const proxyStr = p.host ? (p.type || 'socks5') + '://' + (p.username ? p.username + '@' : '') + p.host + ':' + p.port : '';
    const w = U.openModal({ title: a ? t('acc.edit') : t('acc.addTgTitle'), sub: esc(t('acc.warmup')),
      body: (needCreds ? credStep() : '') + '<div class="form-grid"><div class="field"><label class="label">' + t('acc.phone') + ' <span class="req">*</span></label><input class="input mono" id="a-phone" value="' + esc(a ? a.phone : '') + '" ' + (a ? 'disabled' : '') + ' placeholder="+48 600 000 000"></div>' +
        '<div class="field"><label class="label">' + t('acc.label') + '</label><input class="input" id="a-label" value="' + esc(a ? a.label : '') + '"></div>' +
        '<div class="field span-2"><label class="label">' + t('acc.role') + '</label><div class="seg" id="a-role"><button class="' + (!a || a.role === 'monitor' ? 'active' : '') + '" data-r="monitor">' + t('acc.r.monitor') + '</button><button class="' + (a && a.role === 'notify' ? 'active' : '') + '" data-r="notify">' + t('acc.r.notify') + '</button></div><span class="hint">' + t('acc.role_hint') + '</span>' +
          '<label class="checkbox small" id="a-scout-wrap" style="margin-top:8px;' + (a && a.role === 'notify' ? 'display:none' : '') + '"><input type="checkbox" id="a-scout" ' + (!a || (a.roles || []).includes('scout') ? 'checked' : '') + '>' + t('acc2.rolesScout') + '</label></div>' +
        (a ? '' : '<label class="checkbox small span-2"><input type="checkbox" id="a-oldacc">' + t('acc4.oldAccount') + '<span class="xs muted" style="margin-left:6px">' + t('acc4.oldAccountHint') + '</span></label>') +
        '<div class="field span-2"><label class="label">' + t('acc.proxy') + '</label><input class="input mono" id="a-proxy" value="' + esc(proxyStr) + '" placeholder="socks5://user:pass@host:port"><span class="hint">' + t('acc.proxyHint') + (p.username ? ' · ' + t('acc2.proxyPwKeep') : '') + '</span></div>' +
        '<div class="field"><label class="label">' + t('acc.device') + '</label><input class="input" id="a-dev" value="' + esc(dv.device_model || '') + '" placeholder="' + esc(t('acc.deviceAuto')) + '"></div>' +
        '<div class="field"><label class="label">' + t('acc.sysver') + '</label><input class="input" id="a-sys" value="' + esc(dv.system_version || '') + '" placeholder="iOS 18.2"></div>' +
        (a ? '<div class="field"><label class="label">' + t('acc.join_limit') + ' ' + U.lockTag() + '</label><input class="input num" id="a-jl" type="number" value="' + a.daily_join_limit + '"></div><div class="field"><label class="label">' + t('acc.send_limit') + ' ' + U.lockTag() + '</label><input class="input num" id="a-sl" type="number" value="' + a.daily_send_limit + '"></div>' : '') +
        '</div><div class="alert alert-info" style="margin-top:14px">' + icon('lock') + '<span>' + t('acc.sessionNote') + '</span></div>',
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="a-ok">' + (a ? t('c.save') : t('acc2.addAndLogin')) + '</button>' });
    U.$$('#a-role button', w).forEach((b) => { b.onclick = () => { U.$$('#a-role button', w).forEach((x) => x.classList.toggle('active', x === b)); U.$('#a-scout-wrap', w).style.display = b.dataset.r === 'notify' ? 'none' : ''; }; });
    U.$('#a-ok', w).onclick = async (e) => {
      const v = (s) => (U.$(s, w) ? U.$(s, w).value.trim() : '');
      let proxy = {};
      const ps = v('#a-proxy');
      if (ps) {
        const m = ps.match(/^(socks5|http|https)?:?\/?\/?(?:([^:@]+)(?::([^@]*))?@)?([^:@/]+):(\d+)/i);
        if (!m) { U.$('#a-proxy', w).classList.add('is-error'); return; }
        proxy = { type: (m[1] || 'socks5').toLowerCase().replace('https', 'http'), username: m[2] || '', password: m[3] || '', host: m[4], port: m[5] };
      }
      const role = U.$('#a-role .active', w).dataset.r;
      const roles = role === 'notify' ? ['notify'] : ['monitor'].concat(U.$('#a-scout', w).checked ? ['scout'] : []);   // 用途可以重叠（PRD 6.2.1）
      const data = { label: v('#a-label'), role, roles, proxy, device: { device_model: v('#a-dev'), system_version: v('#a-sys') } };
      if (!a && U.$('#a-oldacc', w) && U.$('#a-oldacc', w).checked) data.skip_warmup = true;   // 老号：不养号
      try {
        await busy(e.currentTarget, async () => {
          if (a) { if (U.$('#a-jl', w)) { data.daily_join_limit = v('#a-jl'); data.daily_send_limit = v('#a-sl'); } await api('PUT', '/api/tg/accounts/' + a.id, data); U.closeModal(); toast(t('c.saved')); U.refresh(); }
          else {
            if (needCreds) {      // 先保存凭证（填好才能下一步），再添加号码，再打开登录
              if (!v('#a-tgid') || !v('#a-tghash')) { ['#a-tgid', '#a-tghash'].forEach((s) => { if (!v(s)) U.$(s, w).classList.add('is-error'); }); throw new U.ApiError('tg_creds_missing', {}); }
              S.tgCfg = Object.assign(S.tgCfg || {}, await api('PUT', '/api/tg/config', { api_id: v('#a-tgid'), api_hash: v('#a-tghash'), backend: 'telethon' }));
            }
            const r = await api('POST', '/api/tg/accounts', Object.assign({}, data, { phone: v('#a-phone') })); U.closeModal(); U.refresh();
            if (tgCredsOk()) setTimeout(() => loginDialog(r), 80);
          }
        });
      } catch (err) { toastErr(err); }
    };
  }
  /* 手机号 + 验证码（+ 两步验证密码）登录；验证码只有客户自己能收到；密码只用于这次登录，不保存 */
  async function loginDialog(a) {
    try { a = await api('POST', '/api/tg/accounts/' + a.id + '/login'); } catch (e) { toastErr(e); return; }
    let needPw = false;
    const w = U.openModal({ cls: 'sm', title: t('acc.login_title', { phone: a.phone }), sub: t('acc.login_sub'),
      body: '<div class="field"><label class="label">' + t('acc.code') + '</label><input class="input mono" id="l-code" inputmode="numeric" autocomplete="one-time-code"></div>' +
        '<div class="field hidden" id="l-pw-wrap"><label class="label">' + t('acc.2fa') + '</label><input class="input" type="password" id="l-pw"><span class="hint">' + t('acc.password_hint') + '</span></div>' +
        (S.tgCfg && S.tgCfg.backend === 'mock' ? '<div class="hint">' + t('acc.mock_code') + '</div>' : ''),
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="l-ok">' + t('acc.loginBtn') + '</button>' });
    U.$('#l-ok', w).onclick = async (e) => {
      try {
        const r = await busy(e.currentTarget, () => api('POST', '/api/tg/accounts/' + a.id + '/code', { code: U.$('#l-code', w).value, password: U.$('#l-pw', w).value }));
        if (r.status === 'need_password' && !needPw) { needPw = true; U.$('#l-pw-wrap', w).classList.remove('hidden'); U.$('#l-pw', w).focus(); toast(t('acc.need_pw'), 'warn'); return; }
        if (r.status !== 'active') throw new U.ApiError('tg_need_password');
        U.closeModal(); toast(t('acc.logged_in')); U.refresh();
      } catch (err) { toastErr(err); }
    };
  }

  /* =================================================================
     渠道池（范围决定 #5）：全部渠道（TG 群 / 频道，以后 FB 群、论坛、名录、展会名单……）+ TG 群：已加入 / AI 找群推荐 / 辅助进群
     渠道拓展员第 3 步完整上线；第 2 步由“AI 找群”驱动
     ================================================================= */
  const CH_IC = { tg_group: 'telegram', tg_channel: 'telegram', fb_group: 'users', forum: 'message', b2b_board: 'inbox', tender_site: 'file', directory: 'database', expo_list: 'calendar', youtube_channel: 'play', news_source: 'globe' };
  const CH_ST = { monitored: 'tag-success', candidate: 'tag-info', retired: '' };
  /* ---- 渠道池 → TG 群：按账号 / 状态筛选、按群名搜索（PRD 6.3「按账号分类」）。账号和状态记在本机（每个看的人自己的） */
  const GRP_KEY = 'st.grpFilter';
  function grpFilter() {
    if (!S.grpF) {
      let f = {};
      try { f = JSON.parse(localStorage.getItem(GRP_KEY) || '{}') || {}; } catch (e) { f = {}; }
      S.grpF = { acc: f.acc === 'all' || f.acc == null ? 'all' : +f.acc, st: ['all', 'on', 'off', 'left'].includes(f.st) ? f.st : 'all', q: '' };
    }
    return S.grpF;
  }
  function saveGrpFilter() { try { localStorage.setItem(GRP_KEY, JSON.stringify({ acc: S.grpF.acc, st: S.grpF.st })); } catch (e) { /* 隐私模式等：不记也能用 */ } }
  /* 账号的短名字：备注名 → @用户名 → 号码后 4 位 */
  const accShort = (a) => a ? (a.label || (a.tg_user && a.tg_user.username ? '@' + a.tg_user.username : t('grp3.tail', { n: String(a.phone || '').slice(-4) }))) : '';
  window.tgAccShort = accShort;
  function drawJoined() {
    const box = U.$('#grp-list');
    if (!box || !S.grp) return;
    const f = grpFilter(), { chats, accs } = S.grp;
    const byAcc = {}; accs.forEach((a) => { byAcc[a.id] = a; });
    const live = (c) => !c.left_at;
    const inAcc = chats.filter((c) => f.acc === 'all' || c.account_id === f.acc);
    // 账号筛选（每个账号后面是它的群数，不算已退出的）
    const chip = (v, label, n) => '<span class="chip chip-sm' + (f.acc === v ? ' active' : '') + '" data-action="grpAcc" data-v="' + v + '">' + label + ' <span class="xs muted">' + n + '</span></span>';
    U.$('#grp-accs').innerHTML = '<span class="xs muted" style="align-self:center">' + t('grp3.account') + '</span>' + chip('all', esc(t('grp3.allAcc')), chats.filter(live).length) +
      accs.map((a) => chip(a.id, icon(a.role === 'notify' ? 'bell' : 'telegram') + esc(accShort(a)), chats.filter((c) => live(c) && c.account_id === a.id).length)).join('');
    const nOn = inAcc.filter((c) => c.monitored).length, nOff = inAcc.filter((c) => live(c) && !c.monitored).length, nLeft = inAcc.filter((c) => !live(c)).length;
    const seg = (v, label, n) => '<button class="' + (f.st === v ? 'active' : '') + '" data-action="grpSt" data-v="' + v + '">' + label + (n == null ? '' : ' <span class="xs muted">' + n + '</span>') + '</button>';
    U.$('#grp-st').innerHTML = seg('all', t('c.all')) + seg('on', t('grp3.on'), nOn) + seg('off', t('grp3.off'), nOff) + seg('left', t('grp2.left'), nLeft);
    U.$('#grp-badge').textContent = t('grp.monitoring', { n: nOn, m: inAcc.filter(live).length });
    const q = (f.q || '').trim().toLowerCase();
    const rows = inAcc.filter((c) => (f.st === 'all' || (f.st === 'on' && c.monitored) || (f.st === 'off' && live(c) && !c.monitored) || (f.st === 'left' && !live(c))) &&
      (!q || (c.title || '').toLowerCase().includes(q) || (c.username || '').toLowerCase().includes(q)));
    S.grpRows = rows.filter(live).map((c) => c.id);
    /* 每行一个监控开关（唯一的家，U25）；可以多选后批量开 / 关（U27）。不再给每个群选模板（一个任务 = 一个模板，PRD §3） */
    box.innerHTML = rows.length ? '<div class="row" style="gap:8px;padding:0 16px 8px"><span class="xs muted" id="grp-selN">' + t('grp5.selN', { n: (S.grpPicked || new Set()).size }) + '</span><button class="btn btn-secondary btn-sm" data-action="grpBatch" data-on="1">' + t('grp5.batchOn') + '</button><button class="btn btn-ghost btn-sm" data-action="grpBatch" data-on="0">' + t('grp5.batchOff') + '</button></div>' +
      '<div class="table-wrap"><table class="table"><thead><tr><th class="w-check"><input type="checkbox" data-change="grpSelAll" title="' + esc(t('grp5.selAll')) + '"></th><th class="w-check">' + t('grp.monitor') + '</th><th>' + t('grp.name') + '</th><th class="num">' + t('grp.members') + '</th><th>' + t('grp.account') + '</th><th>' + t('grp.last') + '</th><th class="num">' + t('grp.col_read') + '</th><th class="num">' + t('grp.col_hits') + '</th></tr></thead><tbody>' +
      rows.map((c) => '<tr' + (c.left_at ? ' style="opacity:.6"' : '') + '><td>' + (c.left_at ? '' : '<input type="checkbox" data-change="grpPick" data-id="' + c.id + '" ' + ((S.grpPicked || new Set()).has(c.id) ? 'checked' : '') + '>') + '</td><td>' + (c.left_at ? '<span title="' + esc(t('grp2.leftHint')) + '">' + sw(false, 'style="pointer-events:none;opacity:.5"') + '</span>' : sw(c.monitored, 'data-change="grpMon" data-id="' + c.id + '"')) + '</td><td><div class="cell-main">' + U.txt(c.title) + (c.left_at ? ' <span class="tag" title="' + esc(t('grp2.leftHint')) + '">' + t('grp2.left') + '</span>' : '') + '</div><div class="cell-sub">' + t('chatkind.' + c.kind) + (c.username ? ' · @' + esc(c.username) : '') + ' · ' + t('join.m_' + c.join_method) + (c.backfill ? ' · ' + t('grp.backfilling') : '') + '</div></td>' +
        '<td class="num">' + membersTxt(c) + '</td><td class="xs nowrap" title="' + esc(c.phone || '') + '">' + esc(accShort(byAcc[c.account_id]) || c.phone || '') + '</td><td class="xs muted">' + (c.last_read_at ? U.fmtTime(c.last_read_at) : '-') + '</td><td class="num">' + U.fmtNum(c.msgs_read) + '</td><td class="num">' + (c.hits ? '<b style="color:var(--color-success)">' + c.hits + '</b>' : '<span class="muted">0</span>') + '</td></tr>').join('') + '</tbody></table></div>'
      : '<div class="card-body small muted">' + t('grp3.none') + '</div>';
  }
  A.grpAcc = (el) => { const f = grpFilter(); f.acc = el.dataset.v === 'all' ? 'all' : +el.dataset.v; saveGrpFilter(); drawJoined(); };
  A.grpSt = (el) => { const f = grpFilter(); f.st = el.dataset.v; saveGrpFilter(); drawJoined(); };
  /* 账号池「群 N」→ 渠道池 TG 群，选好这个账号 */
  A.grpOfAcc = (el) => { const f = grpFilter(); f.acc = +el.dataset.id; f.st = 'all'; f.q = ''; saveGrpFilter(); S.tabs.channels = 'joined'; U.go('channels/joined'); };
  const SOCIAL_PLATFORM_IDS = ['facebook', 'instagram', 'youtube', 'x', 'reddit'];
  function platformCards(platforms) {
    const byId = Object.fromEntries((platforms || []).map((p) => [p.id, p]));
    return '<div class="grid grid-3" id="social-platform-cards" style="margin-top:16px">' + SOCIAL_PLATFORM_IDS.map((id) => {
      const p = byId[id] || { status: 'none', configured: false };
      const displayStatus = p.status === 'none' && p.configured ? 'done' : p.status;
      return '<section class="card" data-social-platform="' + id + '"><div class="card-header"><h3>' + icon(['x', 'reddit'].includes(id) ? 'globe' : id) + t('social.' + id) + '</h3></div><div class="card-body"><p class="small">' + t('social.state.' + (['none', 'starting', 'open', 'done', 'error'].includes(displayStatus) ? displayStatus : 'none')) + '</p><p class="hint">' + t('social.sessionHint') + '</p>' + (p.error ? '<p class="small text-2">' + (U.has('errors.' + p.error) ? t('errors.' + p.error, {}) : esc(p.error)) + '</p>' : '') + '</div><div class="card-footer row wrap"><button class="btn btn-secondary btn-sm" data-action="socialLogin" data-platform="' + id + '">' + t('social.login') + '</button>' + (p.status === 'open' ? '<button class="btn btn-primary btn-sm" data-action="socialDone" data-platform="' + id + '">' + t('social.done') + '</button>' : '') + (p.configured ? '<button class="btn btn-ghost btn-sm" data-action="socialClear" data-platform="' + id + '">' + t('social.clear') + '</button>' : '') + '</div></section>';
    }).join('') + '</div>';
  }
  A.socialLogin = async (el) => { await busy(el, () => api('POST', '/api/social/' + el.dataset.platform + '/login', {})); U.refresh(); };
  A.socialDone = async (el) => { await busy(el, () => api('POST', '/api/social/' + el.dataset.platform + '/login/done', {})); U.refresh(); };
  A.socialClear = async (el) => { if (await U.confirmDialog({ title: t('social.clear'), body: t('social.clearHint'), onOk: () => api('DELETE', '/api/social/' + el.dataset.platform + '/session') })) U.refresh(); };
  const SOURCE_KINDS = ['fb_group', 'instagram_account', 'youtube_channel', 'forum', 'directory', 'news_source', 'x_account', 'reddit_community'];
  function sourceReadError(key, item) {
    if (['login_required', 'social_login_required'].includes(key)) return t('spool.loginRequired');
    if (key === 'social_content_unavailable') return t('spool.readUnavailable');
    if (String(key).startsWith('ev.')) return U.has(key) ? t(key, { url: (item || {}).url || '', reason: '', limit: '' }) : t('spool.readUnavailable');
    return U.has('errors.' + key) ? t('errors.' + key, {}) : t('spool.readUnavailable');
  }
  function sourceReadState(item) {
    if (item.last_error || String(item.last_status || '').startsWith('ev.')) return sourceReadError(item.last_error || item.last_status, item);
    if (item.last_status === 'done') return t('spool.readDone');
    return item.last_status ? (U.has('ops.state.' + item.last_status) ? t('ops.state.' + item.last_status) : t('spool.readUnavailable')) : t('scout.none');
  }
  function observedCount(value) { return value == null ? '—' : U.fmtNum(value); }
  function sourceLineage(item) {
    if (!item.parent_url) return '';
    return '<div class="cell-sub" data-parent-source>' + t('spool.parentSource') + ': ' + (/^https?:\/\//i.test(item.parent_url) ? '<a href="' + esc(item.parent_url) + '" target="_blank" rel="noopener noreferrer">' + esc(item.parent_url) + '</a>' : esc(item.parent_url)) + (item.link_text ? ' · ' + esc(item.link_text) : '') + '</div>';
  }
  function incrementalCounts(item) {
    return '<div class="cell-sub">' + t('spool.newItems') + ': <b data-source-count="new">' + observedCount(item.last_new_items == null ? item.new_items : item.last_new_items) + '</b> · ' + t('spool.duplicatesSkipped') + ': <b data-source-count="duplicates">' + observedCount(item.last_duplicate_items == null ? item.unchanged_items : item.last_duplicate_items) + '</b> · ' + t('spool.changedItems') + ': <b data-source-count="changed">' + observedCount(item.last_changed_items == null ? item.changed_items : item.last_changed_items) + '</b></div>';
  }
  function sourcePoolReport(report) {
    if (!report || !(report.status || report.error || report.blocked)) return '';
    const stats = report.stats || {}, error = report.error || report.blocked;
    return '<div id="source-pool-report" class="ops-plan"><b>' + t('spool.latest') + '</b><p class="small">' + (report.day ? esc(report.day) + ' · ' : '') + (error ? sourceReadError(error, {}) : report.status === 'done' ? t('spool.taskEnded') : U.has('ops.state.' + report.status) ? t('ops.state.' + report.status) : esc(report.status)) + '</p><div class="row wrap xs muted">' + '<span>' + t('spool.addedSources') + ': <b>' + (report.added_sources == null ? '—' : U.fmtNum(report.added_sources)) + '</b></span>' + ['search_results', 'pages', 'qualified', 'errors'].map((key) => '<span>' + t(key === 'search_results' ? 'run.f.search_results' : key === 'pages' ? 'run.f.pages_read' : 'kpi.' + key) + ': <b>' + observedCount(stats[key]) + '</b></span>').join('') + '</div><p class="hint">' + t('spool.incrementalHint') + '</p><div class="row wrap xs muted">' + [['new', 'source_new_items', 'spool.newItems'], ['duplicates', 'source_unchanged_items', 'spool.duplicatesSkipped'], ['incremental', 'source_changed_items', 'spool.changedItems']].map(([name, key, label]) => '<span>' + t(label) + ': <b data-report-count="' + name + '">' + observedCount(stats[key]) + '</b></span>').join('') + '</div>' + (report.task_id ? '<button class="btn btn-secondary btn-sm" style="margin-top:8px" data-action="go" data-route="run" data-q="task=' + encodeURIComponent(report.task_id) + '">' + t('ops.details') + '</button>' : '') + '</div>';
  }
  function sourcePoolCard(data, icps) {
    const cfg = data.settings || {}, items = data.items || [], report = data.report || {};
    const keyword = (entry) => typeof entry === 'string' ? entry : entry.keyword || entry.word || entry.text || '';
    const source = (entry) => typeof entry === 'object' ? entry.source_url || entry.url || '' : '';
    return '<section class="card" id="source-pool" style="margin-bottom:16px"><div class="card-header"><h3>' + icon('globe') + t('spool.title') + '</h3><div class="actions"><button class="btn btn-secondary btn-sm" data-action="sourceAdd">' + icon('plus') + t('spool.add') + '</button></div></div><div class="card-body"><p class="small text-2">' + t('spool.hint') + '</p><form id="source-pool-form"><label class="checkbox" style="margin:12px 0"><input type="checkbox" id="source-pool-enabled"' + (cfg.enabled ? ' checked' : '') + '> ' + t('spool.enabled') + '</label><div class="form-grid"><div class="field"><label class="label" for="source-pool-icp">' + t('tasks.template') + '</label><select class="select" id="source-pool-icp"><option value="">' + t('ops.selectIcp') + '</option>' + icps.map((i) => '<option value="' + i.id + '"' + (i.id === cfg.icp_id ? ' selected' : '') + '>' + esc(i.name) + '</option>').join('') + '</select></div><div class="field"><label class="label" for="source-pool-time">' + t('ops.time') + '</label><input class="input" type="time" id="source-pool-time" required value="' + esc(cfg.time_of_day || '09:00') + '"></div></div><div class="field"><label class="label" for="source-pool-keywords">' + t('spool.keywords') + '</label><textarea class="textarea" id="source-pool-keywords" placeholder="' + esc(t('spool.keywordsHint')) + '">' + esc((cfg.keywords || []).join('\n')) + '</textarea></div><button class="btn btn-primary btn-sm" type="submit" id="source-pool-save" style="margin-top:12px">' + t('c.save') + '</button></form>' + ((cfg.discovered_keywords || []).length ? '<details style="margin-top:12px"><summary class="small">' + t('spool.discoveredKeywords') + '</summary><div class="row wrap" style="margin-top:8px">' + cfg.discovered_keywords.map((entry) => '<span class="tag">' + esc(keyword(entry)) + (source(entry) && /^https?:\/\//i.test(source(entry)) ? ' <a href="' + esc(source(entry)) + '" target="_blank" rel="noopener noreferrer">' + t('spool.evidence') + '</a>' : '') + '</span>').join('') + '</div></details>' : '') + sourcePoolReport(report) + '</div>' + (items.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>' + t('spool.source') + '</th><th>' + t('spool.access') + '</th><th>' + t('spool.latest') + '</th><th>' + t('grp.monitor') + '</th></tr></thead><tbody>' + items.map((item) => '<tr data-source-id="' + item.id + '"><td><div class="cell-main">' + esc(item.name || item.url) + '</div><div class="cell-sub">' + esc(U.has('spool.kind.' + item.kind) ? t('spool.kind.' + item.kind) : item.kind) + ' · ' + esc(item.url || '') + '</div>' + sourceLineage(item) + '</td><td><select class="select input-sm" style="min-width:140px" data-change="sourceAccess" data-id="' + item.id + '" data-access="' + esc(item.access || 'public') + '">' + (item.platform ? ['public', 'login'] : ['public']).map((mode) => '<option value="' + mode + '"' + (item.access === mode ? ' selected' : '') + '>' + t('spool.access.' + mode) + '</option>').join('') + '</select></td><td class="small">' + sourceReadState(item) + incrementalCounts(item) + '</td><td>' + sw(item.status === 'monitored', 'data-change="sourceToggle" data-id="' + item.id + '"') + '</td></tr>').join('') + '</tbody></table></div>' : '<p class="small muted" style="padding:0 20px 16px">' + t('spool.empty') + '</p>') + '</section>';
  }
  function bindSourcePool(root) {
    const form = U.$('#source-pool-form', root); if (!form) return;
    const sync = () => { U.$('#source-pool-icp', form).required = U.$('#source-pool-enabled', form).checked; };
    U.$('#source-pool-enabled', form).onchange = sync; sync();
    form.onsubmit = async (event) => {
      event.preventDefault(); const id = U.$('#source-pool-icp', form).value;
      try { await busy(U.$('#source-pool-save', form), () => api('PUT', '/api/source-pool/settings', { enabled: U.$('#source-pool-enabled', form).checked, icp_id: id ? +id : null, time_of_day: U.$('#source-pool-time', form).value, keywords: U.$('#source-pool-keywords', form).value.split('\n').map((s) => s.trim()).filter(Boolean) }));
      toast(t('c.saved')); U.refresh(); } catch (e) { toastErr(e); }
    };
  }
  A.sourceAdd = async () => {
    const w = U.openModal({ title: t('spool.add'), body: '<form id="source-add-form"><div class="field"><label class="label" for="source-add-url">URL</label><input class="input" type="url" required id="source-add-url" placeholder="https://..."></div><div class="field"><label class="label" for="source-add-name">' + t('spool.source') + '</label><input class="input" id="source-add-name" maxlength="200"></div><div class="form-grid"><div class="field"><label class="label" for="source-add-kind">' + t('ch.kind') + '</label><select class="select" id="source-add-kind">' + SOURCE_KINDS.map((kind) => '<option value="' + kind + '">' + t('spool.kind.' + kind) + '</option>').join('') + '</select></div><div class="field"><label class="label" for="source-add-access">' + t('spool.access') + '</label><select class="select" id="source-add-access">' + ['public', 'login'].map((mode) => '<option value="' + mode + '">' + t('spool.access.' + mode) + '</option>').join('') + '</select></div></div></form>', footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="source-add-save">' + t('c.save') + '</button>' });
    const syncMode = () => { const access = U.$('#source-add-access', w), allowed = ['fb_group', 'instagram_account', 'youtube_channel', 'x_account', 'reddit_community'].includes(U.$('#source-add-kind', w).value); access.querySelector('[value=login]').disabled = !allowed; if (!allowed) access.value = 'public'; };
    U.$('#source-add-kind', w).onchange = syncMode; syncMode();
    U.$('#source-add-save', w).onclick = async (event) => {
      if (!U.$('#source-add-form', w).reportValidity()) return;
      try { await busy(event.currentTarget, () => api('POST', '/api/source-pool', { url: U.$('#source-add-url', w).value.trim(), name: U.$('#source-add-name', w).value.trim(), kind: U.$('#source-add-kind', w).value, access: U.$('#source-add-access', w).value }));
      U.closeModal(); toast(t('c.saved')); U.refresh(); } catch (e) { toastErr(e); }
    };
  };
  A.sourceAccess = async (el) => { try { await api('PUT', '/api/source-pool/' + el.dataset.id, { access: el.value }); el.dataset.access = el.value; toast(t('c.saved')); } catch (e) { el.value = el.dataset.access; toastErr(e); } };
  A.sourceToggle = async (el) => { const input = el.querySelector('input'); try { await api('PUT', '/api/source-pool/' + el.dataset.id, { status: input.checked ? 'monitored' : 'retired' }); toast(t('c.saved')); } catch (e) { input.checked = !input.checked; toastErr(e); } };

  async function allChannels() {
    const d = await api('GET', '/api/channels');
    const rows = d.items;
    const lang = (l) => l ? U.langName(l) : '';
    /* U25：这里只看各渠道的产出（只读）；群的监控开关只在「TG 群」里 */
    return '<p class="xs muted" style="margin-bottom:10px">' + icon('info') + ' ' + t('ch2.readonly') + '</p>' +
      '<div class="card">' + (rows.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>' + t('ch.name') + '</th><th>' + t('ch.kind') + '</th><th>' + t('ch.where') + '</th><th class="num">' + t('grp.members') + '</th><th>' + t('grp.activity') + '</th><th class="num">' + t('ch.leads') + '</th><th class="num">' + t('ch.qualified') + '</th><th class="num">' + t('ch.bad') + '</th><th>' + t('grp.col_status') + '</th></tr></thead><tbody>' +
        rows.map((c) => '<tr' + (c.status === 'retired' ? ' style="opacity:.6"' : '') + '><td><div class="cell-main">' + icon(CH_IC[c.kind] || 'globe') + ' ' + U.txt(c.name) + '</div>' + (c.url ? '<div class="cell-sub mono">' + esc(c.url) + '</div>' : '') + '</td><td class="small nowrap">' + t('chk.' + c.kind) + '</td>' +
          '<td class="small nowrap">' + [c.country ? esc(U.countryName(c.country)) : '', lang(c.language)].filter(Boolean).join(' · ') + '</td><td class="num">' + (c.members ? U.fmtNum(c.members) : '-') + '</td><td class="xs nowrap">' + (c.activity ? t('grp.perDay', { n: c.activity }) : '-') + '</td>' +
          '<td class="num">' + (c.leads || '<span class="muted">0</span>') + '</td><td class="num">' + (c.qualified ? '<b style="color:var(--color-success)">' + c.qualified + '</b>' : '<span class="muted">0</span>') + '</td><td class="num">' + (c.bad ? '<b style="color:var(--color-danger)">' + c.bad + '</b>' : '<span class="muted">0</span>') + '</td>' +
          '<td>' + U.tag(t('chs.' + c.status), CH_ST[c.status]) + '</td></tr>').join('') + '</tbody></table></div>'
        : '<div class="card-body">' + empty({ icon: 'globe', title: t('ch.empty'), body: t('ch.emptySub'), actions: '<button class="btn btn-primary" data-action="go" data-route="accounts">' + t('nav.accounts') + '</button>' }) + '</div>') + '</div>' +
      '<p class="xs muted" style="margin-top:10px">' + icon('info') + ' ' + t('ch.foot') + '</p>';
  }
  function discoveryEvidence(row) {
    if (!(S.meta.edition && S.meta.edition.id === 'my_igaming')) return '';
    const agent = row.agent_signal, source = row.source_signal;
    const industry = !((agent || {}).evidence || []).length && !!((source || {}).evidence || []).length;
    const value = industry ? source : agent || source;
    if (!value) return '';
    return '<div class="xs muted" style="margin-top:6px">' + t('scout.sourceCharacteristics') + (industry ? ' · ' + t('scout.industrySource') : '') + '</div>' + (value.evidence || []).slice(0, 2).map((e) => '<div class="xs quote" style="margin-top:4px">' + esc(e.quote || '') + '<div class="muted" style="overflow-wrap:anywhere">' + esc(e.url || '') + '</div></div>').join('');
  }
  P.channels = {
    title: () => t('nav.channels'),
    mount(root) {
      bindSourcePool(root);
      const q = U.$('#grp-q', root);
      if (!q) return;
      drawJoined();
      q.addEventListener('input', () => { grpFilter().q = q.value; drawJoined(); });
    },
    async render(params) {
      const tab = S.tabs.channels || 'all';
      const [chats, ds, q, icps, ms, accs] = await Promise.all([api('GET', '/api/tg/chats'), api('GET', '/api/tg/discoveries'), api('GET', '/api/tg/join'), api('GET', '/api/icps'), api('GET', '/api/monitor/status'), api('GET', '/api/tg/accounts')]);
      S.grpSel = new Set();
      let body;
      if (tab === 'all') { const pool = await api('GET', '/api/source-pool'); body = sourcePoolCard(pool, icps) + await allChannels(); }
      else if (tab === 'joined') {
        S.grp = { chats, icps, accs: accs.accounts };
        const f = grpFilter();
        if (params && params.acc) { f.acc = +params.acc; saveGrpFilter(); }
        if (f.acc !== 'all' && !accs.accounts.some((a) => a.id === f.acc)) f.acc = 'all';
        /* U26：监控员不能用时固定红条（文字说明去哪配置，不放跳转按钮） */
        body = (ms.monitor_ready ? '' : '<div class="alert alert-danger">' + icon('alert') + '<span>' + t(ms.monitor_off ? 'grp5.monitorOff' : 'grp5.monitorUnconfigured') + '</span></div>') +
          '<div class="card"><div class="card-header"><h3>' + icon('telegram') + t('grp.joined') + '</h3><span class="tag tag-primary" id="grp-badge"></span><span class="sub">' + t('grp.joined_hint', { iv: t('iv.' + ms.settings.interval) }) + '</span>' +
          '<div class="actions">' + (ms.tg_tasks && ms.tg_tasks.length ? '<button class="btn btn-secondary btn-sm" data-action="monRun">' + icon('activity') + t('grp.run_now') + '</button>'
            : '<span class="xs" style="color:var(--color-warning);max-width:260px">' + icon('info') + ' ' + t('grp4.needTaskText') + '</span>') + '</div></div>' +
          (chats.length ? '<div class="card-body" style="padding-bottom:0"><div class="row wrap" id="grp-accs" style="gap:6px;margin-bottom:10px"></div>' +
            '<div class="row wrap" style="gap:8px;margin-bottom:12px"><div class="seg" id="grp-st"></div><span class="grow"></span><div class="input-group" style="width:240px">' + icon('search') + '<input class="input input-sm" id="grp-q" placeholder="' + esc(t('grp3.searchPh')) + '" value="' + esc(f.q) + '"></div></div></div>' +
            '<div id="grp-list"></div>'
            : '<div class="card-body">' + empty({ icon: 'telegram', title: t('grp.empty'), body: t('grp.empty_sub'), actions: '<button class="btn btn-primary" data-action="go" data-route="accounts">' + t('nav.accounts') + '</button>' }) + '</div>') + '</div>';
      } else if (tab === 'recommend') {
        body = (!q.assisted ? '<div class="alert alert-warning">' + icon('lock') + '<span>' + t('grp.joinNeedRisk') + '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="enableAssisted">' + t('grp.enableNow') + '</button></div></div>' : '') +
          '<div class="card"><div class="card-header"><h3>' + avatar('scout', 'sm') + t('grp.recommend') + '</h3><span class="sub">' + t('grp.discover_sub') + '</span><div class="actions">' +
          (icps.length ? '<select class="select input-sm" id="g-icp" style="width:auto">' + icps.map((i) => '<option value="' + i.id + '">' + esc(i.name) + '</option>').join('') + '</select>' : '') +
          '<button class="btn btn-ai btn-sm" data-action="grpDiscover">' + icon('sparkles') + t('grp.findMore') + '</button></div></div>' +
          (ds.length ? '<div class="table-wrap"><table class="table"><thead><tr><th class="w-check"></th><th>' + t('grp.name') + '</th><th class="num">' + t('grp.members') + '</th><th>' + t('grp.activity') + '</th><th>' + t('grp.relevance') + '</th><th>' + t('grp.aiComment') + '</th><th>' + t('grp.col_status') + '</th></tr></thead><tbody>' +
            ds.map((x) => '<tr' + (x.ad_prob > 0.5 && !(S.meta.edition && S.meta.edition.id === 'my_igaming') ? ' style="opacity:.7"' : '') + '><td>' + (x.status === 'suggested' ? '<input type="checkbox" data-change="grpSel" data-id="' + x.id + '">' : '') + '</td><td><div class="cell-main">' + esc(x.title) + (x.ad_prob > 0.5 ? ' <span class="tag tag-outline">' + t('grp.adFlag') + '</span>' : '') + '</div><div class="cell-sub mono">' + esc(x.link) + ' · ' + t('disc.src_' + (x.source || 'tg_search')) + '</div></td>' +
              '<td class="num">' + U.fmtNum(x.members) + '</td><td class="xs nowrap">' + (x.activity ? t('grp.perDay', { n: x.activity }) : '-') + '</td>' +
              '<td style="min-width:120px"><div class="row" style="gap:8px"><div class="progress sm grow ' + (x.relevance < 0.5 && !(S.meta.edition && S.meta.edition.id === 'my_igaming') ? 'danger' : '') + '"><span style="width:' + Math.round(x.relevance * 100) + '%"></span></div><b class="xs num">' + Math.round(x.relevance * 100) + '</b></div><div class="cell-sub">' + t('grp.rel_ad', { r: Math.round(x.relevance * 100), a: Math.round(x.ad_prob * 100) }) + '</div></td>' +
              '<td class="small text-2" style="max-width:340px">' + esc(x.reason) + discoveryEvidence(x) + '</td><td>' + U.tag(t('disc.' + x.status), x.status === 'joined' ? 'tag-success' : x.status === 'approved' ? 'tag-primary' : x.status === 'rejected' ? '' : 'tag-info') + '</td></tr>').join('') + '</tbody></table></div>'
            : '<div class="card-body">' + empty({ icon: 'search', title: t('grp.disc_empty'), body: t('grp.disc_empty_sub') }) + '</div>') +
          '<div class="card-footer"><span class="grow small muted">' + t('grp.disc_hint') + '</span><button class="btn btn-secondary btn-sm" data-action="grpReject" id="g-rej" disabled>' + t('grp.reject') + '</button><button class="btn btn-primary btn-sm" data-action="grpApprove" id="g-app" disabled>' + t('grp.approve') + '</button></div></div>';
      } else {
        body = (!q.assisted ? '<div class="alert alert-warning">' + icon('lock') + '<span>' + t('grp.joinNeedRisk') + '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="enableAssisted">' + t('grp.enableNow') + '</button></div></div>'
          : '<div class="alert alert-warning">' + icon('alert') + '<span>' + t('grp2.assistedOn') + '</span><div class="alert-actions"><button class="btn btn-ghost btn-sm" data-action="disableAssisted">' + t('c.off') + '</button></div></div>') +
          '<div class="card"><div class="card-header"><h3>' + icon('link') + t('grp.pasteTitle') + '</h3></div><div class="card-body"><textarea class="textarea mono" id="g-links" style="min-height:140px" placeholder="https://t.me/solar_buyers&#10;https://t.me/+AbCdEf123456"></textarea><p class="hint" style="margin-top:6px">' + t('grp.assisted_sub') + '</p></div>' +
          '<div class="card-footer"><span class="grow small muted">' + t('grp.queue_hint') + '</span><button class="btn btn-primary" data-action="grpEnqueue">' + t('grp.pasteBtn') + '</button></div></div>' +
          '<div class="card" style="margin-top:16px"><div class="card-header"><h3>' + t('grp.queue') + '</h3></div>' + (q.queue.length ? '<div class="table-wrap"><table class="table"><tbody>' +
            q.queue.map((x) => '<tr><td class="small mono">' + esc(x.link) + '</td><td>' + esc(x.title || '') + '</td><td>' + U.tag(t('jq.' + x.status), x.status === 'joined' ? 'tag-success' : x.status === 'failed' ? 'tag-danger' : '') + '</td><td class="small mono">' + esc(x.account_label || x.phone || '') + '</td><td class="small muted">' + t('join.src_' + x.source) + '</td><td>' + (x.status === 'pending' ? '<button class="btn btn-ghost btn-sm" data-action="grpCancel" data-id="' + x.id + '">' + t('c.cancel') + '</button>' : '') + '</td></tr>').join('') + '</tbody></table></div>'
            : '<div class="card-body small muted">' + t('grp.queue_empty') + '</div>') + '</div>';
      }
      return '<div class="page-head"><div><h1>' + t('nav.channels') + '</h1><p>' + t('ch.sub') + '</p></div></div>' +
        U.tabs('channels', [['all', t('ch.all'), 'globe'], ['joined', t('ch.tgJoined'), 'telegram', chats.filter((c) => !c.left_at).length], ['recommend', t('grp.recommend'), 'sparkles', ds.filter((x) => x.status === 'suggested').length], ['assist', t('grp.assist'), 'link', q.queue.filter((x) => x.status === 'pending').length]], 'all') + body;
    },
  };
  /* 批量开 / 关监控（U27）：勾选的群（没勾就是当前筛选出来的全部） */
  A.grpPick = (el) => { S.grpPicked = S.grpPicked || new Set(); el.checked ? S.grpPicked.add(+el.dataset.id) : S.grpPicked.delete(+el.dataset.id); const n = U.$('#grp-selN'); if (n) n.textContent = t('grp5.selN', { n: S.grpPicked.size }); };
  A.grpSelAll = (el) => { S.grpPicked = new Set(el.checked ? S.grpRows : []); drawJoined(); };
  A.grpBatch = async (el) => {
    const picked = S.grpPicked && S.grpPicked.size;
    const ids = picked ? [...S.grpPicked] : S.grpRows || [];
    if (!ids.length) return;
    const on = el.dataset.on === '1';
    if (!picked && !(await U.confirmDialog({ title: t(on ? 'grp5.batchOn' : 'grp5.batchOff'), body: t('grp5.allFiltered', { n: ids.length }) }))) return;
    await busy(el, () => api('POST', '/api/tg/chats/monitor', { ids, on }));
    toast(t(on ? 'grp5.batchOnDone' : 'grp5.batchOffDone', { n: ids.length })); S.grpPicked = new Set(); U.refresh();
  };
  A.grpSel = (el) => { el.checked ? S.grpSel.add(+el.dataset.id) : S.grpSel.delete(+el.dataset.id); ['#g-app', '#g-rej'].forEach((s) => { const b = U.$(s); if (b) b.disabled = !S.grpSel.size; }); };
  A.grpDiscover = async (el) => { const r = await busy(el, () => api('POST', '/api/tg/discover', { icp_id: +(U.$('#g-icp') || {}).value || undefined })); toast(t('grp.disc_done', { n: r.added })); U.refresh(); };
  A.grpApprove = async () => {
    const q = await api('GET', '/api/tg/join');
    const doIt = async () => { const r = await api('POST', '/api/tg/discoveries/approve', { ids: [...S.grpSel] }); toast(t('grp.queued', { n: r.queued })); U.refresh(); };
    if (q.assisted) doIt(); else U.riskDialog('assisted_join', doIt);
  };
  A.grpReject = async () => { await api('POST', '/api/tg/discoveries/reject', { ids: [...S.grpSel] }); U.refresh(); };
  A.enableAssisted = () => U.riskDialog('assisted_join', () => U.refresh());
  A.disableAssisted = async () => { await api('POST', '/api/settings/risk', { feature: 'assisted_join', enable: false }); toast(t('risk.offToast', { f: t('riskf.assisted_join') })); U.refresh(); };
  A.grpEnqueue = async (el) => { const r = await busy(el, () => api('POST', '/api/tg/join', { links: U.$('#g-links').value })); toast(t('grp.queued', { n: r.queued })); U.refresh(); };
  A.grpCancel = async (el) => { await api('DELETE', '/api/tg/join/' + el.dataset.id); U.refresh(); };

  /* =================================================================
     ⑧ 设置：基本（AI 模型方案 · 总预算 · 接收人 · 落款 · 语言主题 · 重新开始设置）+「▸ 高级」默认收起
     ================================================================= */
  P.settings = {
    async render() {
      if (S.tabs.settings && !['basic', 'models', 'keys', 'recipients'].includes(S.tabs.settings)) S.advOpen = true;
      const [rel, u, nt, co, pw, cfgs] = await Promise.all([api('GET', '/api/relay'), api('GET', '/api/usage'), api('GET', '/api/notify'), api('GET', '/api/company'), api('GET', '/api/power'), api('GET', '/api/agents/config')]);
      S.setData = { rel, u, nt, co };
      S.editionInfo = S.meta.edition && S.meta.edition.id === 'my_igaming' ? await api('GET', '/api/edition') : null;
      const tot = u.total, b = tot.budget, me = rel.eval || {}, n = nt.settings;
      const card = (ic, title, body, extra, foot) => '<div class="card"><div class="card-header"><h3>' + icon(ic) + title + '</h3>' + (extra || '') + '</div><div class="card-body">' + body + '</div>' + (foot ? '<div class="card-footer" style="justify-content:flex-end">' + foot + '</div>' : '') + '</div>';
      const rcp = (key, list) => '<div class="col">' + (list || []).map((r, i) => '<div class="row" style="padding:6px 10px;border:1px solid var(--color-border);border-radius:var(--radius-md)">' + icon(/^https?:|^\+|joinchat|t\.me\/\+/.test(r) ? 'users' : 'user') + '<span class="grow small mono">' + esc(r) + '</span><button class="btn btn-ghost btn-icon btn-sm" data-action="rmRecipient" data-k="' + key + '" data-i="' + i + '">' + icon('x') + '</button></div>').join('') +
        '<div class="row"><input class="input input-sm" id="add-' + key + '" placeholder="' + esc(t('set.addRecipient')) + '"><button class="btn btn-secondary btn-sm" data-action="addRecipient" data-k="' + key + '">' + icon('plus') + t('c.add') + '</button></div></div>';
      return '<div class="page-head"><div><h1>' + t('set.title') + '</h1><p>' + t('settings.sub') + '</p></div></div>' +
        '<div class="grid" style="max-width:920px">' +
        (tot.paused ? '<div class="alert alert-danger">' + icon('pause') + '<span><b>' + t('budget.paused_title') + '</b> ' + t('budget.paused_body', { period: '@period.' + tot.period }) + '</span></div>' : '') +
        /* U47：每个 AI 的模型表（含判断模型第 6 行）放在「基本」最上面；U16：搜索 Key 也在「基本」 */
        '<div id="basic-models">' + U.skeleton(3) + '</div><div id="basic-keys"></div>' +
        (S.editionInfo ? card('target', t('edition.title'), '<p class="small text-2">' + t('edition.scope') + '</p><p class="hint" style="margin:8px 0 12px">' + t('edition.searchHint') + '</p><div class="field"><label class="label" for="edition-product">' + t('edition.product') + '</label><textarea class="textarea" id="edition-product" maxlength="2000" placeholder="' + esc(t('edition.productPlaceholder')) + '"></textarea></div><div class="row wrap" style="margin-top:10px">' + (S.editionInfo.presets || []).filter((p) => p.key === 'channel_team').map((p) => '<label class="checkbox"><input type="checkbox" data-edition-preset="' + esc(p.key) + '" checked> ' + esc(t('edition.segment.' + p.key)) + '</label>').join('') + '</div><p class="hint" style="margin-top:10px">' + t('edition.confirmHint') + '</p>', '', '<button class="btn btn-primary btn-sm" data-action="editionPresets" id="edition-confirm">' + t('edition.create') + '</button>') : '') +
        card('coins', t('set.budgetTotal'), '<div class="form-grid"><div class="field"><label class="label">' + t('set.dayBudget') + ' ($)</label><input class="input num" id="b-d" type="number" min="0" step="1" value="' + b.daily + '"><div class="progress sm" style="margin-top:6px"><span style="width:' + (b.daily > 0 ? Math.min(100, tot.spent.today.cost / b.daily * 100) : 0) + '%"></span></div><span class="hint">' + t('c.today') + ' ' + money(tot.spent.today.cost) + '</span></div>' +
          '<div class="field"><label class="label">' + t('set.monthBudget') + ' ($)</label><input class="input num" id="b-m" type="number" min="0" step="1" value="' + b.monthly + '"><div class="progress sm" style="margin-top:6px"><span style="width:' + (b.monthly > 0 ? Math.min(100, tot.spent.month.cost / b.monthly * 100) : 0) + '%"></span></div><span class="hint">' + t('c.month') + ' ' + money(tot.spent.month.cost) + '</span></div></div>' +
          '<p class="xs muted" style="margin-top:10px">' + t('set.budgetHint') + ' ' + t('budget.hint') + '</p>', b.set_by_user ? U.lockTag() : '<span class="tag tag-outline">' + t('wb3.budgetDefault') + '</span>', '<button class="btn btn-primary btn-sm" data-action="budgetSave">' + t('c.save') + '</button>') +
        card('users', t('set.recipients'), (nt.notify_account ? '<div class="small" style="margin-bottom:12px">' + t('set.notifyAccount') + '：<b class="mono">' + esc(nt.notify_account.phone) + '</b> ' + (nt.ready ? U.tag(t('tgs.active'), 'tag-success') : U.tag(t('tgs.' + nt.notify_account.status), 'tag-danger')) + '</div>'
          : '<div class="alert alert-warning" style="margin-bottom:12px">' + icon('alert') + '<span>' + t('push.no_notify') + '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="go" data-route="accounts">' + t('nav.accounts') + '</button></div></div>') +
          '<div class="form-grid"><div class="field"><label class="label">' + t('set.leadRecipients') + '</label>' + rcp('lead_recipients', n.lead_recipients) + '<span class="hint">' + t('push.rcp_hint') + '</span></div>' +
          '<div class="field"><label class="label">' + t('push.cutoff') + ' ' + U.lockTag() + '</label><input class="input" type="time" id="n-cut" value="' + esc(n.cutoff) + '" style="width:140px"><span class="hint">' + t('push.cutoff_hint') + '</span></div>' +
          '<div class="field"><label class="label">' + t('set.leadPush') + '</label><label class="row small">' + sw(n.push_enabled, 'id="n-on"') + t('push.enabled') + '</label><span class="hint">' + t('set2.pushContent') + '</span></div>' +
          '<div class="field"><label class="label">' + t('set.alertRecipients') + '</label>' + rcp('alert_recipients', n.alert_recipients) + '<span class="hint">' + t('set.alertRecipientsHint') + '</span></div></div>',
          '', '<button class="btn btn-ghost btn-sm" data-action="pushTest">' + t('push.test') + '</button><button class="btn btn-ghost btn-sm" data-action="pushNow">' + t('push.now') + '</button><button class="btn btn-primary btn-sm" data-action="notifySave">' + t('c.save') + '</button>') +
        (U.feat('outreach') ?         card('edit', t('ob.q.signature'), '<textarea class="textarea" id="sb-sig" style="min-height:90px" placeholder="' + esc(t('qs.sig_ph')) + '">' + esc(co.signature || '') + '</textarea><span class="hint">' + t('sb.sig_sub') + '</span>', U.lockTag(), '<button class="btn btn-primary btn-sm" data-action="sigSave">' + t('c.save') + '</button>') : '') +
        '<div class="card"><div class="card-body row" style="gap:12px"><div class="grow"><b>' + t('set.restart') + '</b><div class="xs muted">' + t('sb.restart_sub') + '</div></div><button class="btn btn-secondary" data-action="go" data-route="onboarding" data-q="redo=1">' + icon('undo') + t('set.restart') + '</button></div></div>' +
        (pw.warn || pw.auto_sleep_minutes ? '<div class="alert ' + (pw.warn ? 'alert-warning' : 'alert-info') + '">' + icon('info') + '<span>' + t('sb.sleep', { m: pw.auto_sleep_minutes }) + (pw.active ? ' ' + t('sb.sleep_active') : '') + '</span></div>' : '') +
        card('file', t('diag.title'), '<p class="small text-2">' + t('diag.contents') + '</p><p class="xs muted" style="margin-top:8px">' + t('diag.excludes') + '</p><p class="hint" style="margin-top:8px">' + t('diag.local') + '</p>', '', '<a class="btn btn-secondary btn-sm" id="diagnostics-export" href="/api/diagnostics/export" download>' + icon('download') + t('diag.export') + '</a>') +
        '<button class="adv-toggle" data-action="toggleAdv">' + icon(S.advOpen ? 'chevronDown' : 'chevronRight') + '<b>' + t('set.advanced') + '</b><span class="xs muted">' + t('set2.advHint') + '</span></button>' +
        (S.advOpen ? '<div class="alert alert-info">' + icon('a_manager') + '<span>' + t('settings.adv_note') + '</span></div><div class="grid" id="adv"><div id="adv-usage"></div><div id="adv-sources"></div><div id="adv-risk"></div><div id="adv-notify"></div><div id="adv-data"></div><div id="adv-about"></div></div>' : '') + '</div>';
    },
    async mount(root) {
      for (const f of [advModels, basicKeys]) { try { await f(root); } catch (e) { toastErr(e); } }
      if (!S.advOpen) { const t0 = { models: '#basic-models', keys: '#basic-keys' }[S.tabs.settings]; if (t0 && U.$(t0, root)) U.$(t0, root).scrollIntoView({ block: 'start' }); return; }
      const tasks = [advUsage, advSources, advRisk, advNotify, advData, advAbout];
      for (const f of tasks) { try { await f(root); } catch (e) { toastErr(e); } }
      const sub = S.tabs.settings;
      const target = { models: '#basic-models', keys: '#basic-keys', budget: '#adv-usage', sources: '#adv-sources', notify: '#adv-notify', risk: '#adv-risk' }[sub];
      if (target && U.$(target, root)) U.$(target, root).scrollIntoView({ block: 'start' });
    },
  };
  A.editionPresets = async () => {
    const input = U.$('#edition-product');
    if (!input.reportValidity()) return;
    const product = input.value.trim();
    const keys = U.$$('[data-edition-preset]:checked').map((x) => x.dataset.editionPreset);
    if (!keys.length) { toast(t('edition.selectPreset'), 'error'); return; }
    if (await U.confirmDialog({ title: t('edition.create'), body: t(product ? 'edition.confirmBody' : 'edition.confirmDefault', { product }), onOk: async () => {
      await api('POST', '/api/edition/presets', { confirmed: true, keys, ...(product ? { product } : {}) });
    } })) { toast(t('edition.saved')); U.go('icp'); }
  };
  A.toggleAdv = () => { S.advOpen = !S.advOpen; if (!S.advOpen) S.tabs.settings = 'basic'; U.refresh(); };
  A.budgetSave = async (el) => { await busy(el, () => api('PUT', '/api/budget', { daily: U.$('#b-d').value, monthly: U.$('#b-m').value })); toast(t('budget.saved')); U.refreshBadges(); U.refresh(); };
  const notifyPayload = (extra) => Object.assign({ lead_recipients: S.setData.nt.settings.lead_recipients, alert_recipients: S.setData.nt.settings.alert_recipients, cutoff: U.$('#n-cut').value.trim(), push_enabled: U.$('#n-on input').checked }, extra || {});
  A.notifySave = async (el) => { await busy(el, () => api('PUT', '/api/notify', notifyPayload())); toast(t('c.saved')); U.refresh(); };
  A.addRecipient = async (el) => {
    const k = el.dataset.k, i = U.$('#add-' + k); const v = i.value.trim();
    if (!v) { i.classList.add('is-error'); return; }
    const list = (S.setData.nt.settings[k] || []).concat(v);
    await api('PUT', '/api/notify', notifyPayload({ [k]: list })); U.refresh();
  };
  A.rmRecipient = async (el) => { const k = el.dataset.k; const list = (S.setData.nt.settings[k] || []).filter((_, i) => i !== +el.dataset.i); await api('PUT', '/api/notify', notifyPayload({ [k]: list })); U.refresh(); };
  A.pushTest = async (el) => { await api('PUT', '/api/notify', notifyPayload()); await busy(el, () => api('POST', '/api/notify/test', { which: 'lead' })); toast(t('push.test_ok')); };
  A.pushNow = async (el) => { const r = await busy(el, () => api('POST', '/api/notify/push', {})); toast(t('push.now_ok', { n: r.leads }), r.ok ? 'success' : 'error'); };
  A.sigSave = async (el) => { const co = Object.assign({}, S.setData.co, { signature: U.$('#sb-sig').value.trim() }); await busy(el, () => api('PUT', '/api/company', co)); toast(t('c.saved')); };
  A.delRelay = async (el) => { if (await U.confirmDialog({ title: t('sb.del_relay'), body: t('sb.del_relay_body'), ok: t('c.delete'), danger: true, onOk: () => api('DELETE', '/api/relay/' + el.dataset.id) })) U.refresh(); };
  A.addRelay = () => relayDialog(() => U.refresh());
  /* 重新选模型：AI 实测 → 一个方案（够用就好：先过门槛，再比价格 / 速度 / 拒答），客户确认即可 */
  A.repickModels = async () => {
    const rel = await api('GET', '/api/relay');
    if (!rel.relays.length) { relayDialog(() => U.refresh()); return; }
    const tier = 'balanced';      // 只有一个方案（接口参数保留）
    const w = U.openModal({ cls: 'lg', title: t('set.repick'), sub: t('sb.ai_sub'), body: '<div id="rp"></div>', footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.close') + '</button><button class="btn btn-secondary" id="rp-eval">' + icon('refresh') + t('sb.reeval') + '</button><button class="btn btn-primary" id="rp-ok" disabled>' + t('c.save') + '</button>' });
    const draw = (me) => {
      const box = U.$('#rp', w); if (!box) return;
      U.$('#rp-ok', w).disabled = !(me.status === 'done' || me.status === 'applied');
      box.innerHTML = modelPlan(me, tier, true);
      const run = U.$('#mp-run', box); if (run) run.onclick = async () => { await api('POST', '/api/relay/evaluate', { confirmed: true }); toast(t('qs.models_running')); poll(); };
    };
    let timer = null;
    const poll = () => { clearInterval(timer); timer = setInterval(async () => { if (!U.$('#rp', w)) return clearInterval(timer); const r = await api('GET', '/api/relay'); draw(r.eval || {}); if (['done', 'applied', 'failed', 'awaiting_confirm'].includes((r.eval || {}).status)) clearInterval(timer); }, 2500); };
    draw(rel.eval || {});
    if (!['done', 'applied', 'failed', 'awaiting_confirm', undefined].includes((rel.eval || {}).status)) poll();
    U.$('#rp-eval', w).onclick = async (e) => { await busy(e.currentTarget, () => api('POST', '/api/relay/evaluate', {})); toast(t('qs.models_running')); poll(); };
    U.$('#rp-ok', w).onclick = async (e) => {
      try { const r = await busy(e.currentTarget, () => api('POST', '/api/relay/apply', { tier })); const bad = Object.entries(r.tests || {}).filter(([, v]) => !v.ok); U.closeModal(); toast(bad.length ? t('sb.apply_partial', { n: bad.length }) : t('sb.applied'), bad.length ? 'warn' : 'success'); U.refresh(); } catch (err) { toastErr(err); }
    };
  };
  /* 模型方案表（快速开始第 5 步和“重新选模型”共用） */
  function modelPlan(me, tier, withRun) {
    const s2 = me.status;
    if (s2 === 'done' || s2 === 'applied') {
      const rec = me.recommendation || {}, plan = (rec[tier] || {}).plan || {};
      return '<div class="row" style="margin-bottom:12px;align-items:flex-start"><span class="small text-2 grow">' + icon('info') + ' ' + t('qs.models_rule') + '</span><span class="xs muted">' + t('qs.models_sub', { usd: (me.actual_usd || 0).toFixed(2) }) + '</span></div>' +
        '<div class="table-wrap"><table class="table"><thead><tr><th>AI</th><th>' + t('set.model') + '</th><th class="num">' + t('qs.m_score') + '</th><th class="num">' + t('qs.m_refusal') + '</th><th class="num">' + t('qs.m_monthly') + '</th><th>' + t('qs.m_backup') + '</th></tr></thead><tbody>' +
        Object.entries(plan).map(([role, p]) => '<tr><td>' + (role === 'decision' ? '<span class="row">' + icon('zap') + t('ob.decision') + '</span>' : U.agentChip(role)) + '</td><td class="mono small">' + (p ? esc(p.norm) : '—') + '</td><td class="num">' + (p ? Math.round(p.score * 100) : '-') + '</td><td class="num">' + (p ? Math.round(p.refusal_rate * 100) + '%' : '-') + '</td><td class="num">' + (p && p.monthly_usd != null ? '$' + p.monthly_usd : '—') + '</td><td class="mono xs muted">' + (p && p.fallback ? esc(p.fallback.norm) : '') + '</td></tr>').join('') +
        '</tbody></table></div><div class="xs muted" style="margin-top:8px">' + t('qs.models_total', { usd: (rec[tier] || {}).monthly_usd || 0 }) + ((rec[tier] || {}).unknown_price ? ' · ' + t('qs.models_unknown') : '') + (s2 === 'applied' ? ' · ' + t('qs2.models_applied') : '') + '</div>';
    }
    if (s2 === 'awaiting_confirm') return '<div class="row"><div class="grow"><b>' + t('qs.models_confirm', { usd: me.estimate_usd }) + '</b><div class="small text-2">' + t('qs.models_confirm_sub') + '</div></div>' + (withRun ? '<button class="btn btn-primary btn-sm" id="mp-run">' + t('qs.models_run') + '</button>' : '') + '</div>';
    if (s2 === 'failed') return '<div class="alert alert-warning">' + icon('alert') + '<span>' + t('qs.models_failed', { e: t('errors.' + (me.error || 'unexpected')) }) + '</span></div>';
    if (!s2) return '<p class="small muted">' + t('me.st.idle') + '</p>';
    return '<div class="row">' + avatar('manager') + '<div class="grow"><b>' + t('qs.models_testing') + '</b><div class="small text-2">' + t('qs.models_testing_sub') + '</div></div><span class="thinking" data-agent="manager"><i></i><i></i><i></i></span></div>';
  }
  window.modelPlan = modelPlan;
  /* 添加中转站（快速开始第 2 步和设置共用）：选服务 → 粘贴 Key → 一键配置（自动识别、列模型、开始实测） */
  /* AI 接入（PRD 2.4.1）：默认就是“自定义 API”—— 填 API 地址 + Key（+ 模型名，可选），自动识别 OpenAI / Anthropic 兼容；
     常用平台是快捷按钮，只帮你填好地址（并记下是哪家，提示里显示它的名字）。任何中转站都能用。 */
  const relayName = (key, fallback) => U.has('relay.name.' + key) ? t('relay.name.' + key) : (fallback || key);
  /* 一个 API 给客户看的名字：常用平台 → 平台名（DeepSeek / OpenRouter …）；自定义 → 地址的主机名（如 api.example.com） */
  const apiName = (r) => {
    if (!r) return '';
    if (r.preset && r.preset !== 'custom' && U.has('relay.name.' + r.preset)) return t('relay.name.' + r.preset);
    try { const h = new URL(r.base_url).host; if (h) return h; } catch (e) { /* mock:// 之类 */ }
    return (r.base_url || '').replace(/^[a-z]+:\/\//, '') || r.name || r.id || '';
  };
  const agentApiName = (c, relays) => c.provider === 'relay' ? (apiName((relays || []).find((r) => r.id === c.preset)) || c.preset || '') : t('provider.' + c.provider);
  function relayForm(presets, opts) {
    const o = opts || {};
    const chips = presets.filter((p) => !p.test_only && p.key !== 'custom');
    return '<div class="field"><label class="label">' + t('relay2.common') + '</label><div class="row wrap" id="r-chips">' +
      chips.map((p) => '<span class="chip chip-sm" data-preset="' + p.key + '" data-kind="' + (p.kind === 'anthropic' ? 'claude' : 'openai') + '" data-url="' + esc(p.base_url || '') + '">' + esc(relayName(p.key, p.name)) + (p.recommended ? ' · ' + t('qs.recommended') : '') + '</span>').join('') + '</div></div>' +
      '<div class="form-grid" style="margin-top:4px"><div class="field span-2"><label class="label">' + t('relay2.protocol') + '</label><div class="seg" id="r-proto"><button type="button" class="active" data-p="openai">' + t('relay2.pOpenai') + '</button><button type="button" data-p="claude">' + t('relay2.pClaude') + '</button></div><span class="hint">' + t('relay2.protoHint') + '</span></div>' +
      '<div class="field span-2"><label class="label">' + t('relay2.url') + ' <span class="req">*</span></label><input class="input mono" id="r-url" placeholder="https://api.example.com/v1" autocomplete="off"><span class="hint">' + t('relay2.urlHint') + '</span></div>' +
      '<div class="field span-2"><label class="label">' + t('relay2.key') + ' <span class="req">*</span></label><input class="input mono" type="password" id="r-key" autocomplete="off" placeholder="' + esc(t('models.key_ph')) + '"></div>' +
      '<div class="field span-2"><label class="label">' + (o.modelLabel || t('relay3.model')) + ' <span class="xs muted">' + t('c.optional') + '</span></label><div class="row"><input class="input mono grow" id="r-models" list="r-model-list" placeholder="' + esc(o.modelPh || t('relay3.modelPh')) + '" autocomplete="off"><button type="button" class="btn btn-secondary btn-sm" id="r-load">' + icon('refresh') + t('relay3.loadModels') + '</button></div><datalist id="r-model-list"></datalist><span class="hint" id="r-models-hint">' + (o.modelHint || t('relay3.modelHint')) + '</span></div></div>' +
      '<input type="hidden" id="r-preset" value="">' + '<span class="hint">' + t('qs.relay_hint') + '</span>';
  }
  const setProto = (root, p) => U.$$('#r-proto button', root).forEach((b) => b.classList.toggle('active', b.dataset.p === p));
  function bindRelayForm(root) {
    U.$$('#r-proto button', root).forEach((b) => { b.onclick = () => setProto(root, b.dataset.p); });
    U.$$('#r-chips [data-preset]', root).forEach((c) => {
      c.onclick = () => {
        U.$$('#r-chips [data-preset]', root).forEach((x) => x.classList.toggle('active', x === c));
        U.$('#r-url', root).value = c.dataset.url; U.$('#r-preset', root).value = c.dataset.preset; setProto(root, c.dataset.kind); U.$('#r-key', root).focus();
      };
    });
    const load = U.$('#r-load', root);
    if (load) load.onclick = async (e) => {   // 读取模型列表（不保存），从里面选主管用的模型
      const proto = (U.$('#r-proto .active', root) || { dataset: { p: 'openai' } }).dataset.p;
      try {
        const r = await busy(e.currentTarget, () => api('POST', '/api/relay/models', { protocol: proto, base_url: U.$('#r-url', root).value.trim(), api_key: U.$('#r-key', root).value.trim() || (U.$('#r-url', root).value.trim().startsWith('mock://') ? 'test-key' : '') }));
        U.$('#r-model-list', root).innerHTML = r.models.map((m) => '<option value="' + esc(m) + '">').join('');
        U.$('#r-models-hint', root).textContent = r.models.length ? t('relay3.loaded', { n: r.models.length }) : t('relay2.modelsNeeded');
        U.$('#r-models', root).focus();
      } catch (err) { toastErr(err); }
    };
    const url = U.$('#r-url', root);
    url.oninput = () => {   // 改了地址：不再算作某个常用平台（除非和它的地址一样）
      const hit = U.$$('#r-chips [data-preset]', root).find((x) => x.dataset.url && x.dataset.url === url.value.trim());
      U.$('#r-preset', root).value = hit ? hit.dataset.preset : '';
      U.$$('#r-chips [data-preset]', root).forEach((x) => x.classList.toggle('active', x === hit));
    };
  }
  async function submitRelay(root, role) {   // role：给哪个 AI 用上（快速开始 = manager）；不传 = 只加到 API 列表
    const base = U.$('#r-url', root).value.trim();
    if (!base) { U.$('#r-url', root).classList.add('is-error'); throw new U.ApiError('relay_bad_url', {}); }
    const preset = U.$('#r-preset', root).value || 'custom';
    const models = U.$('#r-models', root).value.trim();
    try {
      const protocol = (U.$('#r-proto .active', root) || {}).dataset ? U.$('#r-proto .active', root).dataset.p : 'openai';
      return await api('POST', '/api/relay', Object.assign({ preset, protocol, base_url: base, api_key: U.$('#r-key', root).value.trim() || (base.startsWith('mock://') ? 'test-key' : ''), models }, role ? { role } : {}));
    } catch (e) {
      if (e.key === 'relay_models_needed') { const m = U.$('#r-models', root); m.classList.add('is-error'); m.focus(); U.$('#r-models-hint', root).textContent = t('relay2.modelsNeeded'); }
      throw e;
    }
  }
  /* 设置 →「添加 API」（PRD 2.4.1「可同时接多个中转站」）：只加到 API 列表，不改任何 AI 正在用的模型（包括主管）；
     新 API 的模型只作为备用 / 切换的候选。要让某个 AI 换用它：高级 → 每个 AI 的模型 → 编辑 */
  async function relayDialog(done) {
    const rel = await api('GET', '/api/relay');
    const w = U.openModal({ title: t('relay4.addTitle'), sub: t('relay4.addSub'), body: relayForm(rel.presets, { modelLabel: t('relay4.modelLabel'), modelPh: t('relay4.modelPh'), modelHint: t('relay4.modelHint') }), footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="r-go">' + icon('plus') + t('relay4.addBtn') + '</button>' });
    bindRelayForm(w);
    U.$('#r-go', w).onclick = async (e) => { try { const r = await busy(e.currentTarget, () => submitRelay(w)); U.closeModal(); toast(t('relay4.added', { name: apiName(r.relay) })); if (done) done(); } catch (err) { toastErr(err); } };
  }

  /* ---- 高级：每个 AI 的模型（表格 + 编辑弹窗）、一个 Key 用于全部 */
  async function advModels(root) {
    const [cfgs0, rel, dec] = await Promise.all([api('GET', '/api/agents/config'), api('GET', '/api/relay'), api('GET', '/api/decision')]);
    /* 判断模型（PRD 2.3）：表格第 6 行，和其他 AI 一样配置（同一个弹窗）；没配置 = 不使用 */
    const cfgs = cfgs0.concat([Object.assign({}, dec, { agent: 'decision', fallback: null, usage: Object.assign({ approx: {} }, dec.usage) })]);
    S.agentCfgs = cfgs; S.relayList = rel.relays || [];
    const keyTag = (c) => c.agent === 'decision' ? '<span class="xs muted">' + (c.configured && c.provider === 'relay' ? t('set2.viaRelay') : '—') + '</span>' : !S.meta.providers[c.provider] || !S.meta.providers[c.provider].needs_key ? '<span class="xs muted">' + (c.provider === 'relay' ? t('set2.viaRelay') : '—') + '</span>' : c.api_key_set ? U.tag(t('models.key_saved') + ' ' + c.api_key_hint, 'tag-success') : U.tag(t('models.key_none'), 'tag-warning');
    const mgr = cfgs.find((c) => c.agent === 'manager');
    const missing = cfgs.filter((c) => c.agent !== 'manager' && c.agent !== 'decision' && !c.configured);
    const rel2 = rel.relays || [];
    U.$('#basic-models', root).innerHTML = '<div class="card"><div class="card-header"><h3>' + icon('cpu') + t('set.models') + '</h3><span class="sub">' + t('set.modelsSub') + '</span>' +
      (mgr && mgr.configured && missing.length ? '<div class="actions"><button class="btn btn-primary btn-sm" data-action="allSameAsMgr">' + icon('copy') + t('set3.allSame', { n: missing.length }) + '</button></div>' : '') + '</div>' +
      '<div class="table-wrap"><table class="table"><thead><tr><th>AI</th><th>' + t('set.provider') + '</th><th>' + t('set.model') + '</th><th>' + t('set.apiKey') + '</th><th class="num">' + t('c.today') + '</th><th></th></tr></thead><tbody>' +
      cfgs.map((c) => '<tr><td><span class="row">' + agentIcon(c.agent) + '<b class="small">' + esc(agentLabel(c.agent)) + '</b></span></td><td class="small">' + (c.configured ? esc(agentApiName(c, S.relayList)) : U.tag(t('wb.s.unconfigured'), 'tag-warning')) + (c.provider === 'mock' ? ' ' + U.tag(t('models.test_only'), 'tag-warning') : '') + (c.via === 'decisions_api' ? ' ' + U.tag(t('decider.viaDecisions')) : '') + '</td>' +
        '<td class="mono small">' + (c.configured ? esc(c.model || '—') : '<span class="muted">—</span>') + (c.configured && ['reference', 'estimate'].includes(c.price_src) ? ' <span class="tag" title="' + esc(t('price.srcHint.' + c.price_src)) + '">' + t('price.src.' + c.price_src) + '</span>' : '') + backupLine(c) + '</td><td>' + keyTag(c) + '</td><td class="num small">' + U.moneyApprox(c.usage.spent.today.cost, (c.usage.approx || {}).today) + '</td>' +
        '<td class="nowrap"><button class="btn btn-secondary btn-sm" data-action="testAgent" data-key="' + c.agent + '">' + t('c.test') + '</button> <button class="btn btn-ghost btn-sm" data-action="editAgent" data-key="' + c.agent + '">' + icon('edit') + '</button><span id="test-' + c.agent + '"></span></td></tr>').join('') +
      '</tbody></table></div>' +
      /* API 列表、添加 API、重新选模型放在表格下面 */
      '<div class="card-body" style="border-top:1px solid var(--color-border)"><div class="row" style="gap:8px;margin-bottom:6px"><b class="small">' + t('set3.apis') + '</b><span class="grow"></span><button class="btn btn-ghost btn-sm" data-action="addRelay">' + icon('plus') + t('sb.add_relay') + '</button><button class="btn btn-secondary btn-sm" data-action="repickModels">' + icon('refresh') + t('set.repick') + '</button></div>' +
      (rel2.length ? rel2.map((r) => '<div class="row small" style="padding:4px 0"><b>' + esc(apiName(r)) + '</b><span class="mono muted xs">' + esc(r.base_url) + '</span>' + U.tag(t('relay.kind.' + r.kind)) + (r.key_set ? U.tag(t('models.key_saved') + ' ' + r.key_hint, 'tag-success') : '') + '<span class="grow"></span><button class="btn btn-ghost btn-icon btn-sm" data-action="delRelay" data-id="' + esc(r.id) + '">' + icon('trash') + '</button></div>').join('') : '<div class="xs muted">' + t('set2.noRelay') + '</div>') + '</div>' +
      '<div class="card-footer"><span class="xs muted">' + icon('lock') + ' ' + t('set.keyLocal') + '</span></div></div>';
  }
  /* U47：没配置的 AI 一次都用主管的 API（模型按规则挑；判断模型是选用的，不在里面） */
  A.allSameAsMgr = async (el) => {
    const r = await busy(el, () => api('POST', '/api/agents/unconfigured/same-as-manager', {}));
    toast(t('set3.allSameDone', { n: r.configured.length })); U.refresh();
  };
  /* U16：搜索 Key（Serper / Brave / YouTube）放在「基本」；改了就保存（U51） */
  async function basicKeys(root) {
    const s = await api('GET', '/api/settings/sources');
    const keyRow = (name, label, which, hint) => '<div class="field"><label class="label">' + label + ' ' + (s.keys[name].set ? U.tag(t('models.key_saved') + ' ' + s.keys[name].hint, 'tag-success') : '') + '</label><div class="row"><input class="input mono" type="password" autocomplete="off" data-key="' + name + '" data-change="keySave" placeholder="' + esc(s.keys[name].set ? t('models.key_keep') : t('set.keyPh')) + '"><button class="btn btn-secondary btn-sm" data-action="srcTest" data-which="' + which + '" data-name="' + name + '">' + t('c.test') + '</button>' + (s.keys[name].set ? '<button class="btn btn-ghost btn-sm" data-action="srcClear" data-name="' + name + '">' + t('models.key_clear') + '</button>' : '') + '</div><span class="hint">' + hint + '</span></div>';
    U.$('#basic-keys', root).innerHTML = '<div class="card"><div class="card-header"><h3>' + icon('key') + t('set3.keys') + '</h3><span class="sub">' + t('src.active', { p: '@search.' + s.active_search }) + '</span></div><div class="card-body"><p class="xs muted" style="margin-bottom:10px">' + t('set3.keysSub') + '</p><div class="form-grid">' +
      keyRow('serper_api_key', 'Serper API Key', 'serper', t('src.serper_hint')) + keyRow('brave_api_key', 'Brave Search API Key', 'brave', t('src.brave_hint')) +
      keyRow('youtube_api_key', 'YouTube Data API v3 Key', 'youtube', t('src.yt_hint')) + '</div></div></div>';
  }
  A.keySave = async (el) => { const v = el.value.trim(); if (!v) return; await api('PUT', '/api/settings/sources', { keys: { [el.dataset.key]: v } }); toast(t('c.saved')); S.meta = await api('GET', '/api/meta'); U.refresh(); };
  const agentIcon = (k) => k === 'decision' ? '<span class="agent-avatar sm" title="' + esc(t('agent.decider')) + '" style="font-size:13px">🎯</span>' : avatar(k, 'sm');
  const agentLabel = (k) => k === 'decision' ? t('agent.decider') : t('agent.' + k);
  /* 备用模型（PRD 2.4.1）：主模型拒答 / 没反应时换成它；客户手动选主模型后自动选，也可以自己改 */
  const noBackupText = () => (S.relayList || []).length ? t('set2.noBackupYet') : t('set2.noBackup');
  const backupLine = (c) => c.provider === 'mock' || !c.configured || c.agent === 'decision' ? '' : c.fallback ? '<div class="xs muted" style="font-family:var(--font-sans)">' + t('set2.backup', { m: c.fallback.model }) + (c.fallback.auto ? t('set2.backupAuto') : '') + '</div>' : '<div class="xs" style="color:var(--color-warning);font-family:var(--font-sans)">' + noBackupText() + '</div>';
  const providerOptions = (sel) => Object.keys(S.meta.providers).map((p) => '<option value="' + p + '" ' + (p === sel ? 'selected' : '') + '>' + t('provider.' + p) + '</option>').join('');
  const presetOptions = (sel) => Object.keys(S.meta.providers.openai_compat.presets).map((p) => '<option value="' + p + '" ' + (p === sel ? 'selected' : '') + '>' + t('preset.' + p) + '</option>').join('');
  const suggested = (provider, preset) => { const Pv = S.meta.providers[provider]; if (!Pv) return []; return provider === 'openai_compat' ? ((Pv.presets[preset || 'openai'] || {}).models || []) : (Pv.models || []); };
  A.testAgent = async (el) => {
    const out = U.$('#test-' + el.dataset.key);
    try { const r = await busy(el, () => api('POST', '/api/agents/' + el.dataset.key + '/test')); out.innerHTML = ' <span class="status" style="color:var(--color-success)">' + icon('checkCircle') + t('set.testOk', { ms: r.ms }) + '</span>'; }
    catch (e) { out.innerHTML = ' <span class="status" style="color:var(--color-danger)" title="' + esc(U.errText(e)) + '">' + icon('xCircle') + esc(U.errText(e).slice(0, 60)) + '</span>'; }
  };
  /* 每个 AI 的 API（PRD 2.4.1 / 3.0）：和主管同样的表单（协议 + API 地址 + Key + 模型名称），可以和主管不同；
     两个快捷方式：「和主管一样」「让主管帮我选模型」。备用模型可以在这里改。 */
  A.editAgent = async (el) => {
    const c = S.agentCfgs.find((x) => x.agent === el.dataset.key);
    const rel = await api('GET', '/api/relay');
    S.relayList = rel.relays || [];
    const cur = c.configured ? '<b class="mono">' + esc(c.model) + '</b> · ' + esc(agentApiName(c, rel.relays)) + (c.via === 'decisions_api' ? ' ' + U.tag(t('decider.viaDecisions')) : '') : '<span class="tag tag-warning">' + t('wb.s.unconfigured') + '</span>';
    const isMgr = c.agent === 'manager', isDec = c.agent === 'decision';
    const w = U.openModal({ cls: 'lg', title: agentLabel(c.agent) + ' · ' + t('set.models'), sub: isDec ? t('decider.sub') : t('agent_desc.' + c.agent),
      body: '<div class="small" style="margin-bottom:12px">' + t('relay3.current') + '：' + cur + backupLine(c) + '</div>' +
        (isMgr ? '' : '<div class="row wrap" style="gap:8px;margin-bottom:14px"><button class="btn btn-secondary btn-sm" id="ae-same">' + icon('copy') + t('relay3.same') + '</button><button class="btn btn-ai btn-sm" id="ae-pick">' + icon('sparkles') + t('relay3.pick') + '</button>' + (isDec && c.configured ? '<button class="btn btn-ghost btn-sm" id="ae-off">' + icon('pause') + t('decider.disable') + '</button>' : '') + '<span class="xs muted grow">' + (isDec ? t('decider.shortcutsHint') : t('relay3.shortcutsHint')) + '</span></div>') +
        (isDec ? '<p class="xs muted" style="margin:-4px 0 12px">' + t('decider.howCall') + '</p>' : '') +
        '<div class="section-title">' + t('relay3.ownApi') + '</div>' + relayForm(rel.presets, isMgr ? {} : { modelLabel: t('relay4.modelGeneric'), modelHint: t(isDec ? 'decider.model_hint' : 'relay4.modelHintAgent') }) +
        (c.provider !== 'mock' && c.configured && !isDec ? '<div class="field" style="margin-top:12px"><label class="label">' + t('set2.backupLabel') + '</label><select class="select" id="ae-fb"><option value="">' + esc(c.fallback ? c.fallback.model + (c.fallback.auto ? t('set2.backupAuto') : '') : noBackupText()) + '</option><option value="__auto">' + t('set2.backupReauto') + '</option></select><span class="hint">' + t('set2.backupHint') + '</span></div>' : ''),
      noFocus: true,                              // U48：打开时不把光标放进 API 地址框
      footer: '<span class="xs muted grow" id="ae-why"></span><button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="ae-save" disabled>' + t('c.save') + '</button>' });
    const pf = document.createElement('div'); pf.innerHTML = priceField(c); U.$('.modal-body', w).appendChild(pf);
    bindRelayForm(w);
    /* U48：没有要保存的（没填自己的 API、没改备用模型、没改单价）时「保存」是灰的，旁边写原因 —— 不再“什么都没改也说已保存” */
    const canSave = () => {
      const url = (U.$('#r-url', w) || {}).value, fb = U.$('#ae-fb', w), pin = U.$('#ae-pin', w), pout = U.$('#ae-pout', w);
      const p = c.effective_price || [], mine = c.price_src === 'customer';
      const priceChanged = pin && (mine ? (pin.value.trim() !== String(p[0]) || pout.value.trim() !== String(p[1])) : (pin.value.trim() || pout.value.trim()));
      return !!((url && url.trim()) || (fb && fb.value) || priceChanged);
    };
    const syncSave = () => { const ok = canSave(); U.$('#ae-save', w).disabled = !ok; U.$('#ae-why', w).textContent = ok ? '' : t('set3.saveWhy'); };
    w.addEventListener('input', syncSave); w.addEventListener('change', syncSave); syncSave();
    const fbSel = U.$('#ae-fb', w);
    if (fbSel) api('GET', '/api/agents/' + c.agent + '/fallback/options').then((opts) => { opts.forEach((o) => { const op = document.createElement('option'); op.value = o.relay + '::' + o.id; op.textContent = o.id + (o.vendor ? ' · ' + o.vendor : ''); fbSel.appendChild(op); }); }).catch(() => {});
    const done = (msg) => { U.closeModal(); toast(msg); U.refresh(); };
    const same = U.$('#ae-same', w);
    if (same) same.onclick = async (e) => { try { const r = await busy(e.currentTarget, () => api('POST', '/api/agents/' + c.agent + '/same-as-manager', { model: U.$('#r-models', w).value.trim() })); done(t('relay3.done', { model: r.norm || r.model || '' })); } catch (err) { toastErr(err); } };
    const off = U.$('#ae-off', w);
    if (off) off.onclick = async (e) => { try { await busy(e.currentTarget, () => api('PUT', '/api/decision', { provider: 'none' })); done(t('decider.disabled')); } catch (err) { toastErr(err); } };
    const pick = U.$('#ae-pick', w);
    if (pick) pick.onclick = async (e) => { try { await busy(e.currentTarget, () => api('POST', '/api/agents/' + c.agent + '/pick', {})); done(t('relay3.picking')); } catch (err) { toastErr(err); } };
    U.$('#ae-save', w).onclick = async (e) => {
      try {
        await busy(e.currentTarget, async () => {
          await savePrice(c, w);                  // 自己填的单价（唯一填价格的地方，PRD 2.4.1）
          const fbv = fbSel ? fbSel.value : '';
          if (U.$('#r-url', w).value.trim()) {   // 填了自己的 API：保存并给这个 AI 用上
            const proto = (U.$('#r-proto .active', w) || { dataset: { p: 'openai' } }).dataset.p, base = U.$('#r-url', w).value.trim();
            await api('POST', '/api/relay', { role: c.agent, preset: U.$('#r-preset', w).value || 'custom', protocol: proto, base_url: base, api_key: U.$('#r-key', w).value.trim() || (base.startsWith('mock://') ? 'test-key' : ''), models: U.$('#r-models', w).value.trim() });
          }
          if (fbv === '__auto') await api('PUT', '/api/agents/' + c.agent + '/fallback', { auto: true });
          else if (fbv) { const [relayId, ...m] = fbv.split('::'); await api('PUT', '/api/agents/' + c.agent + '/fallback', { relay: relayId, model: m.join('::') }); }
        });
        done(t('models.saved', { agent: agentLabel(c.agent) }));
      } catch (err) { toastErr(err); }
    };
  };
  /* 单价（选填）：只在这个 AI 的弹窗里填（包括判断模型那一行），存到 /relay/prices（按 API + 模型） */
  const priceRelay = (c) => (c.agent === 'decision' ? c.relay : c.preset) || '';
  function priceField(c) {
    if (!c.configured || c.provider !== 'relay' || !c.model) return '';
    const p = c.effective_price || [], mine = c.price_src === 'customer';
    const now = p.length ? t('price.nowFmt', { i: p[0], o: p[1], src: t('price.src.' + (c.price_src || 'estimate')) }) : '—';
    return '<div class="field" style="margin-top:14px"><label class="label">' + t('price.mine') + '</label><div class="row" style="gap:8px">' +
      '<input class="input num" id="ae-pin" type="number" min="0" step="0.001" style="width:130px" placeholder="' + esc(t('price.in')) + '" value="' + (mine ? p[0] : '') + '">' +
      '<input class="input num" id="ae-pout" type="number" min="0" step="0.001" style="width:130px" placeholder="' + esc(t('price.out')) + '" value="' + (mine ? p[1] : '') + '"></div>' +
      '<span class="hint">' + esc(t('price.mineHint', { now })) + '</span></div>';
  }
  async function savePrice(c, w) {
    const pin = U.$('#ae-pin', w), pout = U.$('#ae-pout', w);
    if (!pin) return;
    const mine = c.price_src === 'customer', p = c.effective_price || [];
    const vi = pin.value.trim(), vo = pout.value.trim();
    if (mine ? (vi === String(p[0]) && vo === String(p[1])) : (!vi && !vo)) return;     // 没改
    await api('PUT', '/api/relay/prices', { relay: priceRelay(c), model: c.model, price_in: vi === '' ? '' : +vi, price_out: vo === '' ? 0 : +vo });
  }
  /* ---- 用量看板 */
  async function advUsage(root) {
    const u = S.setData.u, view = S.usageView || 'today';
    const rows = u.agents.map((a) => ({ key: a.agent, cost: a.spent[view].cost, tokens: a.spent[view].input_tokens + a.spent[view].output_tokens, calls: a.spent[view].calls, approx: (a.approx || {})[view] }));
    const max = Math.max.apply(null, rows.map((r) => r.cost).concat([0.0001])), total = u.total.spent[view].cost;
    U.$('#adv-usage', root).innerHTML = '<div class="card"><div class="card-header"><h3>' + icon('chart') + t('set.usageBoard') + '</h3><span class="sub">' + t('set.byAgent') + '</span><div class="actions"><div class="seg"><button class="' + (view === 'today' ? 'active' : '') + '" data-action="usageView" data-v="today">' + t('c.today') + '</button><button class="' + (view === 'month' ? 'active' : '') + '" data-action="usageView" data-v="month">' + t('c.month') + '</button></div></div></div>' +
      '<div class="card-body">' + rows.map((r) => '<div class="hbar" data-agent="' + (AG().includes(r.key) ? r.key : 'manager') + '"><span class="row">' + (AG().includes(r.key) ? avatar(r.key, 'sm') : icon(r.key === 'decider' ? 'zap' : 'cpu')) + esc(t('agent.' + r.key)) + '</span><div class="track"><span style="width:' + (r.cost / max * 100) + '%"></span></div><span class="small num">' + U.moneyApprox(r.cost, r.approx) + ' <span class="muted xs">· ' + U.fmtTokens(r.tokens) + ' · ' + U.fmtNum(r.calls) + ' ' + t('c.calls') + '</span></span></div>').join('') +
      '<div class="hbar" style="border-top:1px solid var(--color-border);margin-top:6px;padding-top:10px"><b>' + t('set.total') + '</b><span></span><b class="num">' + U.moneyApprox(total, (u.total.approx || {})[view]) + '</b></div><p class="xs muted" style="margin-top:8px">' + t('usage.note') + '</p></div></div>';
  }
  A.usageView = (el) => { S.usageView = el.dataset.v; advUsage(document); };
  /* ---- 数据源与采集 */
  async function advSources(root) {
    const s = await api('GET', '/api/settings/sources');
    const keyRow = (name, label, which, hint) => '<div class="field"><label class="label">' + label + ' ' + (s.keys[name].set ? U.tag(t('models.key_saved') + ' ' + s.keys[name].hint, 'tag-success') : '') + '</label><div class="row"><input class="input mono" type="password" autocomplete="off" data-key="' + name + '" placeholder="' + esc(s.keys[name].set ? t('models.key_keep') : t('set.keyPh')) + '"><button class="btn btn-secondary btn-sm" data-action="srcTest" data-which="' + which + '" data-name="' + name + '">' + t('c.test') + '</button>' + (s.keys[name].set ? '<button class="btn btn-ghost btn-sm" data-action="srcClear" data-name="' + name + '">' + t('models.key_clear') + '</button>' : '') + '</div><span class="hint">' + hint + '</span></div>';
    U.$('#adv-sources', root).innerHTML = '<div class="card" id="src-card"><div class="card-header"><h3>' + icon('search') + t('src.search_title') + '</h3><span class="sub">' + t('src.active', { p: '@search.' + s.active_search }) + '</span></div><div class="card-body"><div class="form-grid">' +
      '<div class="field"><label class="label">' + t('src.search_provider') + '</label><select class="select" id="sp">' + ['auto', 'claude', 'serper', 'brave', 'free'].map((x) => '<option value="' + x + '" ' + (x === s.search.provider ? 'selected' : '') + '>' + t('search.' + x) + '</option>').join('') + '</select><span class="hint">' + t('src.search_hint') + '</span></div>' +
      '<div class="row" style="gap:10px;align-items:flex-start"><div class="field grow"><label class="label">' + t('src.queries_per_source') + '</label><input class="input num" type="number" min="1" max="30" id="sq" value="' + s.search.queries_per_source + '"></div><div class="field grow"><label class="label">' + t('src.results_per_query') + '</label><input class="input num" type="number" min="1" max="20" id="sr" value="' + s.search.results_per_query + '"></div></div>' +
      '<div class="row" style="gap:10px;align-items:flex-start"><div class="field grow"><label class="label">' + t('src.yt_videos') + '</label><input class="input num" type="number" min="1" max="25" id="yv" value="' + s.youtube.max_videos_per_query + '"></div><div class="field grow"><label class="label">' + t('src.yt_comments') + '</label><input class="input num" type="number" min="0" max="100" id="yc" value="' + s.youtube.max_comments_per_video + '"></div></div></div>' +
      '<div class="section-title" style="margin-top:16px">' + icon('globe') + t('src.crawler_title') + '</div><p class="xs muted">' + t('src.crawler_sub') + '</p><div class="grid grid-3" style="margin-top:8px">' +
      [['cd', 'src.delay', s.crawler.delay_seconds, 1, 60, 0.5], ['ct', 'src.max_task', s.crawler.max_pages_per_task, 1, 1000, 1], ['cs', 'src.max_site', s.crawler.max_pages_per_site, 1, 50, 1], ['cdl', 'src.daily', s.crawler.daily_page_limit, 1, 20000, 1], ['ac', 'src.analyst_conc', s.analyst.concurrency, 1, 4, 1]].map(([id, k, v, lo, hi, stp]) => '<div class="field"><label class="label">' + t(k) + '</label><input class="input num" type="number" id="' + id + '" min="' + lo + '" max="' + hi + '" step="' + stp + '" value="' + v + '"></div>').join('') +
      '<label class="checkbox small" style="align-self:end;padding-bottom:8px"><input type="checkbox" id="ae" ' + (s.analyst.enrich_website ? 'checked' : '') + '>' + t('src.enrich') + '</label></div>' +
      /* 隐身模式：测试阶段默认开启、按需使用；普通开关，关闭 / 开启都不弹风险确认 */
      '<div class="plan-item" style="align-items:center;margin-top:10px"><div class="body"><div class="title">' + t('src.stealth') + (s.crawler.stealth_mode !== false ? '<span class="tag tag-primary">' + t('c.on') + '</span>' : '<span class="tag">' + t('c.off') + '</span>') + '</div><div class="desc">' + t('src2.stealth_sub') + '</div></div>' +
      sw(s.crawler.stealth_mode !== false, 'class="switch" data-change="stealthToggle"') + '</div></div>' +
      '<div class="card-footer" style="justify-content:flex-end"><button class="btn btn-primary btn-sm" data-action="srcSave">' + t('c.save') + '</button></div></div>';
  }
  A.srcSave = async (el) => {
    const v = (s) => U.$(s).value;
    const keys = {}; U.$$('[data-key]').forEach((i) => { if (i.value.trim()) keys[i.dataset.key] = i.value.trim(); });
    await busy(el, () => api('PUT', '/api/settings/sources', { search: { provider: v('#sp'), queries_per_source: v('#sq'), results_per_query: v('#sr') }, youtube: { max_videos_per_query: v('#yv'), max_comments_per_video: v('#yc') },
      crawler: { delay_seconds: v('#cd'), max_pages_per_task: v('#ct'), max_pages_per_site: v('#cs'), daily_page_limit: v('#cdl') }, analyst: { concurrency: v('#ac'), enrich_website: U.$('#ae').checked }, keys }));
    toast(t('c.saved')); S.meta = await api('GET', '/api/meta'); U.refresh();
  };
  A.stealthToggle = async (el) => {
    const on = el.querySelector('input').checked;
    await api('PUT', '/api/settings/sources', { crawler: { stealth_mode: on } });
    toast(t(on ? 'src2.stealth_on' : 'src2.stealth_off')); U.refresh();
  };
  A.srcTest = async (el) => { const key = U.$('[data-key=' + el.dataset.name + ']').value.trim(); const r = await busy(el, () => api('POST', '/api/settings/sources/test', { which: el.dataset.which, key })); toast(t('src.test_ok', { n: r.results })); };
  A.srcClear = async (el) => { await api('PUT', '/api/settings/sources', { keys: { [el.dataset.name]: '' } }); toast(t('models.key_cleared')); U.refresh(); };
  /* ---- 安全与风险：所有高风险开关（默认关闭，开启要确认，记录开启时间） */
  async function advRisk(root) {
    const r = await api('GET', '/api/settings/risk');
    U.$('#adv-risk', root).innerHTML = '<div class="card"><div class="card-header"><h3>' + icon('shield') + t('set.risk') + '</h3><span class="sub">' + t('set.riskSub') + '</span></div>' + r.features.map((f) =>
      '<div class="plan-item" style="align-items:center"><div class="risk-icon" style="width:36px;height:36px;' + (f.on ? '' : 'background:var(--color-surface-3);color:var(--color-text-3)') + '">' + icon('alert') + '</div><div class="body"><div class="title">' + t('riskf.' + f.key) + (f.on ? '<span class="tag tag-warning">' + t('c.on') + '</span>' : '<span class="tag">' + t('set.defaultOff') + '</span>') + '</div>' +
      '<div class="desc">' + (U.has('riskf.' + f.key + '.desc') ? t('riskf.' + f.key + '.desc') : '') + '</div>' + (f.on && f.at ? '<div class="xs" style="color:var(--color-warning);margin-top:2px">' + icon('clock') + ' ' + t('set.enabledAt', { t: U.fmtTime(f.at) }) + '</div>' : '') + '</div>' +
      sw(f.on, 'class="switch risk" data-change="riskToggle" data-k="' + f.key + '"') + '</div>').join('') + '</div>';
  }
  A.riskToggle = async (el) => {
    const k = el.dataset.k, input = el.querySelector('input');
    if (input.checked) { input.checked = false; U.riskDialog(k, () => U.refresh()); }
    else { await api('POST', '/api/settings/risk', { feature: k, enable: false }); toast(t('risk.offToast', { f: t('riskf.' + k) })); U.refresh(); }
  };
  /* ---- 提醒与发件记录 */
  async function advNotify(root) {
    const ob = await api('GET', '/api/tg/outbox');
    U.$('#adv-notify', root).innerHTML = '<div class="card"><div class="card-header"><h3>' + icon('alert') + t('set.levels') + '</h3></div><div class="card-body">' +
      '<div class="row small" style="padding:6px 0;align-items:flex-start"><span class="tag tag-danger">' + t('al.urgent') + '</span><span class="xs muted grow">' + t('set2.urgentEx') + '</span></div>' +
      '<div class="row small" style="padding:6px 0;align-items:flex-start;border-top:1px solid var(--color-border)"><span class="tag tag-warning">' + t('al.normal') + '</span><span class="xs muted grow">' + t('set2.normalEx') + '</span></div>' +
      '<p class="xs muted" style="margin-top:6px">' + t('set2.levelsNote') + '</p></div></div>' +
      '<div class="card" style="margin-top:16px"><div class="card-header"><h3>' + icon('send') + t('push.outbox') + '</h3><span class="sub">' + t('push.outbox_sub') + '</span></div>' +
      (ob.length ? '<div class="table-wrap" style="max-height:340px"><table class="table"><tbody>' + ob.slice(0, 50).map((o) => '<tr><td class="nowrap xs muted">' + U.fmtTime(o.ts) + '</td><td class="small mono">' + esc(o.recipient) + '</td><td>' + U.tag(t('push.kind.' + o.kind)) + '</td><td class="small">' + (o.file_name ? icon('file') + esc(o.file_name) : esc((o.text || '').slice(0, 60))) + '</td><td>' + (o.status === 'failed' ? U.tag(t('push.failed'), 'tag-danger') : o.status === 'mock' ? U.tag(t('push.mock'), 'tag-warning') : U.tag(t('push.sent'), 'tag-success')) + '</td></tr>').join('') + '</tbody></table></div>'
        : '<div class="card-body small muted">' + t('push.outbox_empty') + '</div>') + '</div>';
  }
  /* ---- 数据 */
  async function advData(root) {
    U.$('#adv-data', root).innerHTML = '<div class="card"><div class="card-body"><div class="field"><label class="label">' + t('set.dataPath') + '</label><input class="input mono" value="' + esc(S.meta.data_dir) + '" readonly><span class="hint">' + t('data.local_note') + '</span></div></div></div>' +
      '<div class="card" style="margin-top:16px;border-color:color-mix(in srgb,var(--color-danger) 40%,var(--color-border))"><div class="card-body">' +
      [['business', 'btn-danger-ghost'], ['everything', 'btn-danger']].map(([scope, cls]) => '<div class="row" style="gap:16px;padding:8px 0"><div class="risk-icon" style="background:var(--color-danger-soft);color:var(--color-danger)">' + icon('trash') + '</div><div class="grow"><b>' + t('data.delete_' + scope) + '</b><div class="small text-2">' + t('data.delete_' + scope + '_sub') + '</div></div><button class="btn ' + cls + '" data-action="deleteData" data-scope="' + scope + '">' + t('data.delete_btn') + '</button></div>').join('') + '</div></div>';
  }
  A.deleteData = async (el) => {
    const scope = el.dataset.scope;
    const ok = await U.confirmDialog({ title: t('data.confirm_title'), body: t('data.confirm_' + scope), danger: true, ok: t('data.delete_btn'),
      extra: '<div class="field" style="margin-top:14px"><label class="label">' + t('data.type_delete') + '</label><input class="input" id="del-word" placeholder="DELETE"></div>',
      onOk: (w) => api('POST', '/api/data/delete-all', { confirm: U.$('#del-word', w).value.trim(), scope }) });
    if (ok) { toast(t('data.deleted')); S.meta = await api('GET', '/api/meta'); U.render(); }
  };
  /* ---- 关于 */
  async function advAbout(root) {
    let lic = []; try { lic = await (await fetch('/static/licenses.json')).json(); } catch (e) { /* ignore */ }
    U.$('#adv-about', root).innerHTML = '<div class="card"><div class="card-body"><div class="row" style="gap:14px"><img class="brand-logo" src="/static/logo.svg" alt="" style="width:48px;height:48px"><div><h3>' + t('app.name') + '</h3><div class="small text-2">' + t('app.tagline') + '</div><div class="small text-2">' + t('set.version') + ' ' + esc(S.meta.version) + '</div></div></div>' +
      '<ul class="pb-list small" style="margin-top:12px">' + [1, 2, 3, 4, 5].map((i) => '<li>' + t('about.p' + i) + '</li>').join('') + '</ul>' +
      '<div class="section-title" style="margin-top:16px">' + t('set.oss') + '</div><p class="xs muted">' + t('about.oss_sub') + '</p><div class="table-wrap" style="max-height:260px"><table class="table"><tbody>' + lic.map((x) => '<tr><td class="small">' + esc(x.name) + '</td><td class="mono xs">' + esc(x.version) + '</td><td class="small">' + esc(x.license) + '</td></tr>').join('') + '</tbody></table></div></div></div>';
  }

  /* =================================================================
     ⑤ 快速开始：1 协议 → 2 配置 AI → 3 读官网 → 4 补充访谈 → 5 确认（画像 + 模型方案）
     ================================================================= */
  const OB_STEPS = ['ob.s1', 'ob.s2', 'ob.s3', 'ob.s4', 'ob.s5'];
  function autoStep(st, params) {
    if (!st.agreement.accepted_at) return 0;
    const sk = st.skipped || [];
    if (!st.ai_ready && !sk.includes('ai')) return 1;
    const ob = st.latest;
    const over = !ob || ob.status === 'done' || ob.status === 'cancelled';   // cancelled = 跳过 / 重新填写
    if (params.redo && over) return 2;
    if (over) return st.first_launch ? 2 : 5;
    if (['reading', 'robots_blocked', 'failed', 'queued'].includes(ob.status)) return 2;
    if (ob.status === 'interview' && ob.next_question) return 3;
    return 4;
  }
  P.onboarding = {
    fullscreen: true,
    title: () => t('nav.onboarding'),
    async render(params) {
      const st = S.obState = await api('GET', '/api/quickstart');
      const auto = autoStep(st, params);
      let step = params.step != null ? +params.step : (S.tabs.onboarding != null && /^\d$/.test(S.tabs.onboarding) ? +S.tabs.onboarding : auto);
      const maxStep = auto >= 3 && auto < 5 ? 4 : auto;  // 访谈可以随时结束去确认；其他步骤不能跳过
      if (step > maxStep && auto < 5) step = maxStep;
      if (step >= 3 && (!st.latest || ['done', 'cancelled'].includes(st.latest.status)) && step < 5) step = auto;
      S.obStep = step;
      const stepper = '<div class="stepper">' + OB_STEPS.map((k, i) => '<div class="st' + (i < step ? ' done' : i === step ? ' cur' : '') + '"><span>' + (i < step ? icon('check') : i + 1) + '</span>' + t(k) + '</div>').join('<i></i>') + '</div>';
      let inner = '';
      if (step === 0) inner = stepAgree();
      else if (step === 1) inner = stepAi(st);
      else if (step === 2) inner = stepRead(st, params);
      else if (step === 3) inner = '<div id="ob-iv">' + U.skeleton(4) + '</div>';
      else if (step === 4) inner = '<div id="ob-cf">' + U.skeleton(6) + '</div>';
      else inner = '<div style="text-align:center">' + avatar('manager', 'lg', { solid: true }) + '<h1 style="margin-top:14px">' + t('company.doneTitle') + '</h1><p class="text-2" style="margin-top:8px">' + t(st.latest && st.latest.status === 'done' ? 'company.profileDone' : 'company.profilePending') + '</p><p class="text-2">' + t(['applied'].includes((st.model_eval || {}).status) ? 'company.modelsDone' : 'company.modelsPending') + '</p><p class="text-2">' + t('company.nextStep') + '</p>' +
        '<button class="btn btn-primary btn-lg" style="margin-top:18px" data-action="obExit">' + t('nav.dashboard') + icon('arrowRight') + '</button></div>';
      return '<div class="center-page"><div style="width:min(' + (step === 4 ? 1000 : 860) + 'px,100%)">' + (params.redo ? '<div class="alert alert-info" style="margin-bottom:12px">' + icon('info') + '<span>' + t('qs.redo_note') + '</span></div>' : '') + stepper +
        '<div class="card" style="padding:28px 32px;margin-top:18px" id="ob-card">' + inner + '</div>' +
        '<div class="row" style="justify-content:center;margin-top:14px">' + (!st.first_launch && step < 5 ? '<button class="btn btn-ghost btn-sm" data-action="obExit">' + icon('x') + t('qs2.exit') + '</button>' : '') +
        '<div class="seg">' + ['zh', 'en'].map((l) => '<button class="' + (I18N.lang === l ? 'active' : '') + '" data-action="setLang" data-lang="' + l + '">' + (l === 'zh' ? '中文' : 'EN') + '</button>').join('') + '</div></div></div></div>';
    },
    async mount(root, params) {
      const st = S.obState, step = S.obStep;
      if (step === 1) bindRelayForm(root);
      if (step === 2) await mountRead(root, st, params);
      if (step === 3) await mountInterview(root, st);
      if (step === 4) await mountConfirm(root, st);
    },
  };
  const obGo = (n, extra) => U.go('onboarding', Object.assign({ step: n }, S.params.redo ? { redo: 1 } : {}, extra || {}));
  function mgrIntro(text) { return '<div class="row" style="align-items:flex-start;gap:14px" data-agent="manager">' + avatar('manager', 'lg', { solid: true }) + '<div class="bubble-intro"><div class="xs muted">' + t('agent.manager') + ' · Atlas</div><div>' + text + '</div></div></div>'; }

  function stepAgree() {
    return '<div style="text-align:center"><img class="brand-logo" src="/static/logo.svg" alt="" style="width:64px;height:64px;margin:0 auto 14px;display:block"><h1>' + t('ob.welcome') + '</h1><div class="small muted" style="margin-top:4px">' + t('app.tagline') + '</div><p class="text-2" style="margin-top:8px">' + t('qs2.tagline') + '</p></div>' +
      '<div class="label" style="margin:18px 0 6px">' + t('ob.agreement') + ' <span class="hint">' + t('ob.readHint') + '</span></div>' +
      '<div class="agreement-text" style="height:240px">' + [1, 2, 4, 5].map((k, i) => '<h4>' + (i + 1) + '. ' + t('qs.agree.h' + k) + '</h4><p>' + t('qs.agree.p' + k) + '</p>').join('') + '</div>' +
      '<label class="checkbox" style="margin-top:14px;display:flex"><input type="checkbox" id="ob-a1" data-change="obAgree">' + t('ob.agree1') + '</label>' +
      '<div class="row" style="margin-top:20px;justify-content:flex-end"><button class="btn btn-primary btn-lg" id="ob-agree" data-action="obAgreeGo" disabled>' + t('ob.start') + icon('arrowRight') + '</button></div>';
  }
  A.obAgree = () => { U.$('#ob-agree').disabled = !U.$('#ob-a1').checked; };
  A.obAgreeGo = async (el) => { await busy(el, () => api('POST', '/api/quickstart/agree')); obGo(1); };

  function stepAi(st) {
    return '<h2>' + t('relay2.title') + '</h2><p class="text-2" style="margin:6px 0 14px">' + t('qs.ai_sub') + '</p>' +
      (st.relays.length ? '<div class="alert alert-success" style="margin-bottom:12px">' + icon('checkCircle') + '<span>' + t('qs.ai_have', { names: st.relays.map((r) => relayName(r.preset, r.name)).join(', ') }) + '</span></div>' : '') +
      relayForm(st.relay_presets) + '<div id="r-out" style="margin-top:14px"></div>' +
      '<div class="row" style="margin-top:20px"><button class="btn btn-ghost" data-action="obMock">' + t('qs.try_mock') + '</button><span class="grow"></span>' +
      (st.ai_ready ? '<button class="btn btn-secondary" data-action="obSkipAi">' + t('qs.skip_have') + '</button>' : '<button class="btn btn-ghost" data-action="obSkip" data-step="ai">' + t('qs3.skipAi') + '</button>') +
      '<button class="btn btn-ai btn-lg" data-action="obRelay">' + icon('zap') + t('ob.oneClick') + '</button></div>';
  }
  A.obRelay = async (el) => {
    const card = U.$('#ob-card');
    try { const r = await busy(el, () => submitRelay(card, 'manager')); U.$('#r-out', card).innerHTML = '<div class="alert alert-ai">' + icon('sparkles') + '<span>' + t('relay3.done', { model: r.configured.norm }) + '</span></div>'; setTimeout(() => obGo(2), 900); }
    catch (err) { toastErr(err); }
  };
  A.obSkipAi = () => obGo(2);
  /* 除了用户协议，每一步都可以跳过（PRD 3.0）：跳过配置 AI → 下一步；跳过读资料 / 确认 → 先进软件，以后再补 */
  A.obSkip = async (el) => {
    const step = el.dataset.step;
    await busy(el, () => api('POST', '/api/quickstart/skip', { step }));
    S.meta = await api('GET', '/api/meta');
    if (step === 'ai') obGo(2);
    else if (step === 'interview') obGo(4);
    else obGo(5);
  };
  A.obMock = async () => {
    if (!await U.confirmDialog({ title: t('tasks.use_mock'), body: t('tasks.use_mock_confirm'), onOk: () => api('POST', '/api/agents/apply-all', { provider: 'mock' }) })) return;
    toast(t('tasks.mock_on')); S.meta = await api('GET', '/api/meta'); obGo(2);
  };

  function stepRead(st, params) {
    const ob = st.latest;
    const reading = ob && !['done', 'cancelled'].includes(ob.status) && !params.fresh;
    if (!reading) {
      const prev = (ob && ob.inputs) || {};
      return mgrIntro(t('qs2.coIntro')) +
        '<div class="field" style="margin-top:16px"><label class="label">' + t('ob.website') + ' <span class="tag tag-primary">' + t('ob.primary') + '</span></label><div class="input-group">' + icon('globe') + '<input class="input" id="c-web" value="' + esc(prev.website || '') + '" placeholder="https://www.your-company.com"></div><span class="hint">' + t('ob.websiteHint') + '</span></div>' +
        '<div class="form-grid"><div class="field"><label class="label">' + t('ob.pdf') + ' <span class="xs muted">' + t('c.optional') + '</span></label><label class="dropzone" style="display:block;padding:14px">' + icon('upload') + '<div class="small" id="c-pdf-names">' + t('ob.pdfDrop') + '</div><input type="file" id="c-pdf" accept="application/pdf,.pdf" multiple style="display:none"></label><span class="hint">' + t('qs.pdfs_hint') + '</span></div>' +
        '<div class="field"><label class="label">' + t('ob.introText') + ' <span class="xs muted">' + t('c.optional') + '</span></label><textarea class="textarea" id="c-intro" style="min-height:96px" placeholder="' + esc(t('ob.introPh')) + '">' + esc(prev.intro || '') + '</textarea></div></div>' +
        '<div class="field"><label class="label">' + t('qs.oneliner') + '</label><input class="input" id="c-one" value="' + esc(prev.oneliner || '') + '" placeholder="' + esc(t('qs3.onelinerPh')) + '"></div>' +
        '<div class="row" style="margin-top:18px"><span class="small muted grow">' + icon('shield') + ' ' + t('qs.co_note') + '</span><button class="btn btn-ghost" data-action="obSkip" data-step="read">' + t('qs3.skip') + '</button><button class="btn btn-ai btn-lg" data-action="obStartRead">' + icon('sparkles') + t('qs.start_reading') + '</button></div>';
    }
    return mgrIntro('<span id="ob-intro">' + t('ob.researching') + '</span>') +
      '<div class="card" style="margin-top:16px;box-shadow:none"><div class="card-body"><div class="small" id="ob-stat"><span class="spinner" style="width:14px;height:14px;border-width:2px"></span> ' + t('c.loading') + '</div>' +
      '<div class="progress" style="margin-top:10px"><span id="ob-prog" style="width:0%"></span></div><div class="xs muted mono ellipsis" style="margin-top:8px" id="ob-cur">' + esc((ob.inputs || {}).website || '') + '</div></div></div>' +
      '<div class="read-box" style="margin-top:12px;max-height:180px;overflow:auto" id="ob-feed"></div><div id="ob-extra" style="margin-top:12px"></div>' +
      /* 任何状态都有出路：读的时候可以跳过 / 重新填写；读失败或被中断时还可以重试（PRD 3.0，0.2.2 白屏修复） */
      '<div class="row" id="ob-acts" style="margin-top:12px;justify-content:flex-end;gap:8px"><span class="xs muted grow">' + t('qs4.runningHint') + '</span>' + obActs(false) + '</div>' +
      '<p class="xs muted" style="margin-top:12px">' + icon('flag') + ' ' + t('qs2.alsoNote') + '</p>';
  }
  /* 网站被拦的类型：captcha 图片验证码 / browser_check 有验证（隐身模式关着）/ browser_check_stealth_failed 隐身重试也没过 / blocked 拒绝访问 */
  function blockKind(ob) {
    const c = (ob.login || {}).captcha;
    if (!c) return /captcha/.test(ob.error || '') ? 'captcha' : '';
    return typeof c === 'string' && ['captcha', 'browser_check', 'browser_check_stealth_failed', 'rate_limited', 'blocked'].includes(c) ? (c === 'rate_limited' ? 'blocked' : c) : 'captcha';
  }
  function obActs(retry) {
    return (retry ? '<button class="btn btn-primary btn-sm" data-action="obRetry">' + icon('refresh') + t('qs4.retry') + '</button>' : '') +
      '<button class="btn btn-ghost btn-sm" data-action="obSkip" data-step="read">' + t('qs4.skipStep') + '</button>' +
      '<button class="btn btn-ghost btn-sm" data-action="obRefill">' + icon('edit') + t('qs4.refill') + '</button>';
  }
  A.obRetry = async (el) => {
    const ob = S.obState.latest;
    try { const r = await busy(el, () => api('POST', '/api/quickstart/' + ob.id + '/retry')); obGo(2, { task: r.task_id, t: Date.now() }); } catch (err) { toastErr(err); }
  };
  A.obRefill = async (el) => {
    const ob = S.obState.latest;
    if (ob && ob.id) { try { await busy(el, () => api('POST', '/api/quickstart/' + ob.id + '/cancel')); } catch (err) { /* 停不了也照样让客户重新填写 */ } }
    obGo(2, { fresh: 1 });
  };
  A.obStartRead = async (el) => {
    const fd = new FormData();
    fd.append('website', U.$('#c-web').value.trim()); fd.append('intro', U.$('#c-intro').value.trim()); fd.append('oneliner', U.$('#c-one').value.trim());
    if (S.params.redo) fd.append('redo', '1');
    [...U.$('#c-pdf').files].forEach((x) => fd.append('files', x));
    try { await busy(el, () => api('POST', '/api/quickstart/start', fd)); obGo(2); } catch (err) { toastErr(err); }
  };
  const shortUrl = (u) => (u && u.length > 110 ? u.slice(0, 80) + '…' + u.slice(-20) : u);
  function feedLine(ev) {
    const p = Object.assign({}, ev.params || {});
    if (p.url) p.url = shortUrl(p.url);
    return '<div class="small" style="padding:3px 0"><span class="xs muted">' + U.hhmm(ev.ts) + '</span> · ' + U.evText(Object.assign({}, ev, { params: p })) + '</div>';
  }
  async function mountRead(root, st, params) {
    const f = U.$('#c-pdf', root);
    if (f) { f.onchange = () => { U.$('#c-pdf-names', root).textContent = [...f.files].map((x) => x.name).join(', ') || t('ob.pdfDrop'); }; return; }
    const ob = st.latest, taskId = params.task || ob.task_id;
    const s = Object.assign({ pages_read: 0, pages_total: 0, product_lines: 0, certifications: 0, cases: 0, pdfs: 0, facts: 0 }, ob.task_stats || {});
    const hasSite = !!((ob.inputs || {}).website);
    const stat = () => { const el = U.$('#ob-stat', root); if (el) el.innerHTML = hasSite ? t('qs2.readStat', { p: s.pages_read, t: s.pages_total || '?', pl: s.product_lines, c: s.certifications, cs: s.cases, pdf: s.pdfs, f: s.facts }) : t('qs2.readStatNoSite', { f: s.facts, pdf: s.pdfs }); const pg = U.$('#ob-prog', root); if (pg) pg.style.width = Math.round(100 * s.pages_read / Math.max(1, s.pages_total)) + '%'; };
    if (!ob.task_live) stat();       // 任务已经不在了：直接显示最后的进度，不再停在“加载中…”
    const cur = U.$('#ob-cur', root); if (cur) cur.textContent = shortUrl(cur.textContent);
    const finish = async () => {
      let fresh;
      try { fresh = await api('GET', '/api/quickstart/' + ob.id); } catch (e) { fresh = Object.assign({}, ob, { task_live: false }); }
      const extra = U.$('#ob-extra', root); if (!extra) return;
      if (fresh.status === 'reading' && fresh.task_live) { if (fresh.task_id !== taskId) U.refresh(); return; }   // 看门狗换了新任务：跟着新任务
      Object.assign(s, fresh.task_stats || {}); stat();
      U.$('#ob-prog', root).style.width = '100%';
      const acts = U.$('#ob-acts', root); if (acts) acts.remove();
      const failAlert = (cls, ic, msg, more) => '<div class="alert alert-' + cls + '">' + icon(ic) + '<span>' + msg + '</span></div><div class="row" style="margin-top:12px;justify-content:flex-end;gap:8px">' + (more || '') + obActs(true) + '</div>';
      if (fresh.status === 'robots_blocked') {
        U.$('#ob-intro', root).textContent = t('qs4.failedIntro');
        extra.innerHTML = failAlert('warning', 'alert', t('qs.robots_blocked'), '<button class="btn btn-secondary btn-sm" data-action="obOwnSite">' + t('qs.robots_own') + '</button>');
        return;
      }
      if (fresh.status !== 'interview' || !fresh.facts.length) {
        const block = blockKind(fresh);
        const msg = block ? t('qs4.k.' + block) + ' ' + t('qs4.failTail') : fresh.error === 'interrupted' || fresh.task_status === 'interrupted' ? t('qs4.interrupted')
          : fresh.error && !['failed', 'cancelled', 'error'].includes(fresh.error) && U.has('errors.' + fresh.error) ? t('errors.' + fresh.error, { url: (fresh.inputs || {}).website || '', to: (fresh.login || {}).offsite_to || '?' }) : t('qs.read_failed');
        U.$('#ob-intro', root).textContent = t('qs4.failedIntro');
        extra.innerHTML = failAlert(block || fresh.error === 'interrupted' ? 'warning' : 'danger', block ? 'shield' : 'alert', msg);
        return;
      }
      U.$('#ob-intro', root).textContent = t('qs2.readDone', { n: fresh.facts.length });
      const lg = fresh.login || {};
      const blk = blockKind(fresh);
      extra.innerHTML = (blk ? '<div class="alert alert-warning" style="margin-bottom:8px">' + icon('shield') + '<span>' + t('qs4.k.' + blk) + ' ' + t('qs4.partialTail') + '</span></div>' : '') +
        '<div class="alert alert-success">' + icon('checkCircle') + '<span>' + t('qs.read_done', { n: fresh.facts.length }) + '</span></div>' +
        ((lg.detected || []).length ? '<div class="card" style="margin-top:12px;box-shadow:none;border-color:color-mix(in srgb,var(--color-primary) 35%,var(--color-border))"><div class="card-body row" style="gap:12px">' + icon(lg.session ? 'unlock' : 'lock', 'lg') +
          '<div class="grow small"><b>' + t('ob.memberFound') + '</b> <span class="muted">' + t('ob.memberPwd') + '</span><div class="xs muted mono">' + esc(lg.detected[0].url) + '</div></div>' +
          (lg.session ? '<button class="btn btn-ghost btn-sm" data-action="obMemberClear">' + icon('trash') + t('ob.memberClear') + '</button>' : '') + '<button class="btn btn-primary btn-sm" data-action="obMemberLogin">' + icon('external') + t('ob.memberLogin') + '</button></div></div>' : '') +
        '<div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn btn-primary btn-lg" data-action="obGoStep" data-n="' + (fresh.next_question ? 3 : 4) + '">' + t('ob.next') + icon('arrowRight') + '</button></div>';
    };
    if (!taskId) return finish();
    if (!ob.task_live && !params.task) {   // 任务已经结束 / 被中断：显示最后几条记录 + 结果，不再等实时进度
      try { (await api('GET', '/api/tasks/' + encodeURIComponent(taskId) + '/events-tail')).forEach((ev) => { const box = U.$('#ob-feed', root); if (box) box.insertAdjacentHTML('afterbegin', feedLine(ev)); }); } catch (e) { /* 只是记录，读不到也不影响 */ }
      return finish();
    }
    const es = new EventSource('/api/tasks/' + encodeURIComponent(taskId) + '/events?after=0');
    U.onLeave(() => es.close());
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      const box = U.$('#ob-feed', root); if (!box) return;
      box.insertAdjacentHTML('afterbegin', feedLine(ev));
      if (box.children.length > 200) box.lastElementChild.remove();      // 记录只留最近 200 条，页面不会越来越重
      if (ev.key === 'ev.ob_reading' && ev.params.url) { const c = U.$('#ob-cur', root); if (c) c.textContent = shortUrl(ev.params.url); }
    };
    es.addEventListener('stats', (m) => { Object.assign(s, JSON.parse(m.data).stats || {}); stat(); });
    es.addEventListener('end', () => { es.close(); finish(); });
  }
  A.obGoStep = (el) => obGo(+el.dataset.n);
  A.obGoFresh = () => obGo(2, { fresh: 1 });
  A.obOwnSite = () => U.riskDialog('own_site_ignore_robots', () => obGo(2, { fresh: 1 }));
  A.obMemberClear = async () => { await api('POST', '/api/quickstart/' + S.obState.latest.id + '/login/clear'); toast(t('ob.memberCleared')); U.refresh(); };
  /* 官网会员区：打开真实浏览器窗口，客户本人登录；软件只保存登录状态，只读 */
  A.obMemberLogin = async () => {
    const ob = S.obState.latest;
    const open = () => api('POST', '/api/quickstart/' + ob.id + '/login', { url: ((ob.login || {}).detected || [{}])[0].url });
    const proceed = async () => {
      let st;
      try { st = await open(); } catch (e) { toastErr(e); return; }
      if (st.status === 'error') { toast(t('errors.' + (st.error || 'browser_missing')), 'error'); return; }
      const w = U.openModal({ cls: 'sm', title: t('ob.memberLogin'), sub: t('ob.memberAskSub'),
        body: '<div class="alert alert-ai"><span class="spinner"></span><span>' + t('acc.loginWaiting') + '</span></div><ol class="small text-2" style="padding-left:18px;margin:14px 0 0;line-height:1.9"><li>' + t('acc.lstep1') + '</li><li>' + t('acc.lstep2') + '</li><li>' + t('acc.lstep3') + '</li></ol>' +
          '<ul class="risk-list" style="margin-top:14px">' + ['ob.mSafe1', 'ob.mSafe2', 'ob.mSafe3'].map((k) => '<li><span style="color:var(--color-success)">' + icon('shieldCheck') + '</span><span>' + t(k) + '</span></li>').join('') + '</ul>',
        footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="mb-ok">' + icon('check') + t('acc.loginDone') + '</button>' });
      U.$('#mb-ok', w).onclick = async (e) => {
        try { const r = await busy(e.currentTarget, () => api('POST', '/api/quickstart/' + ob.id + '/login/done')); U.closeModal(); toast(t('qs.login_reading', { n: r.cookies })); obGo(2, { task: r.task_id }); } catch (err) { toastErr(err); }
      };
    };
    try { await open(); } catch (e) {
      if (e.key === 'risk_confirm_required') { U.riskDialog('site_login', proceed); return; }
      toastErr(e); return;
    }
    proceed();
  };

  async function mountInterview(root, st) {
    const box = U.$('#ob-iv', root);
    const draw = (o) => {
      const q = o.next_question;
      const msgs = o.chat.map((m) => m.role === 'user' ? '<div class="msg me"><div><div class="bubble">' + (m.content === '__skip__' ? t('qs.skipped') : U.para(m.content)) + '</div></div></div>' : '<div class="msg" data-agent="manager">' + avatar('manager', 'sm') + '<div><div class="bubble">' + U.para(m.content) + '</div></div></div>').join('');
      box.innerHTML = '<div class="row" style="gap:14px">' + '<div class="grow">' + mgrIntro(t('qs.iv_sub')) + '</div><div style="width:200px"><div class="small text-2">' + t('qs.completeness', { p: o.completeness.pct }) + '</div><div class="progress" style="margin-top:6px"><span style="width:' + o.completeness.pct + '%"></span></div></div></div>' +
        '<div class="chat-scroll" style="padding:16px 0;max-height:44vh" id="ob-chat">' + msgs +
        (q ? '<div class="msg" data-agent="manager">' + avatar('manager', 'sm') + '<div><div class="who">' + t('agent.manager') + '</div><div class="bubble"><b>' + t('ob.q.' + q) + '</b>' + (U.has('ob.qh.' + q) ? '<div class="small text-2">' + t('ob.qh.' + q) + '</div>' : '') + '</div></div></div>' : '<div class="msg" data-agent="manager">' + avatar('manager', 'sm') + '<div><div class="bubble">' + t('qs.iv_done') + '</div></div></div>') + '</div>' +
        (q ? '<div class="chat-input" style="border:1px solid var(--color-border);border-radius:var(--radius-md)"><textarea class="textarea" id="ob-in" placeholder="' + esc(t('ob.answerPh')) + '"></textarea><button class="btn btn-primary" id="ob-send">' + icon('send') + '</button></div>' +
          '<div class="row" style="margin-top:10px;justify-content:flex-end"><button class="btn btn-ghost btn-sm" id="ob-skip">' + t('qs.skip') + '</button><button class="btn btn-secondary btn-sm" data-action="obGoStep" data-n="4">' + t('qs.iv_later') + '</button></div>'
          : '<div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn btn-primary btn-lg" data-action="obGoStep" data-n="4">' + t('ob.toProfile') + icon('arrowRight') + '</button></div>');
      const c = U.$('#ob-chat', box); if (c) c.scrollTop = c.scrollHeight;
      const send = async (skip) => { const inp = U.$('#ob-in', box); const v = inp.value.trim(); if (!skip && !v) return; try { draw(await api('POST', '/api/quickstart/' + o.id + '/answer', { message: v, skip })); const n = U.$('#ob-in', box); if (n) n.focus(); } catch (e) { toastErr(e); } };
      const sb = U.$('#ob-send', box); if (sb) sb.onclick = () => send(false);
      const sk = U.$('#ob-skip', box); if (sk) sk.onclick = () => send(true);
      const ta = U.$('#ob-in', box); if (ta) ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(false); } });
    };
    draw(await api('GET', '/api/quickstart/' + st.latest.id));
  }

  async function mountConfirm(root, st) {
    const box = U.$('#ob-cf', root);
    const [ob, rel] = await Promise.all([api('GET', '/api/quickstart/' + st.latest.id), api('GET', '/api/relay')]);
    const prof = ob.profile, co = prof.company || {}, icp = prof.icp || {};
    const miss = ob.completeness.missing_required;
    const tier = 'balanced';      // 只有一个方案（接口参数保留）
    box.innerHTML = '<div class="row" style="gap:14px"><b class="nowrap">' + (miss.length ? t('ob.completeLine', { p: ob.completeness.pct, m: miss.map((m) => t('ob.q.' + m)).join(t('common.list_sep')) }) : t('ob.ready')) + '</b><div class="progress grow"><span style="width:' + ob.completeness.pct + '%"></span></div><b class="num">' + ob.completeness.pct + '%</b></div>' +
      (ob.diff && ob.diff.length ? '<div class="card" style="margin-top:14px;box-shadow:none"><div class="card-header"><h3>' + icon('clock') + t('qs.diff_title') + '</h3><span class="sub">' + t('qs.diff_sub') + '</span></div><div class="card-body tight">' +
        ob.diff.map((x) => '<div class="fact" style="grid-template-columns:140px 1fr 1fr"><div class="k">' + (U.has('qs.f.' + x.field) ? t('qs.f.' + x.field) : U.has('icp.' + x.field) ? t('icp.' + x.field) : esc(x.field)) + '</div><div class="small diff-del">' + esc(x.old || '—') + '</div><div class="small diff-add">' + esc(x.new || '—') + '</div></div>').join('') + '</div></div>' : '') +
      '<div class="grid grid-2" style="margin-top:14px"><div class="card" style="box-shadow:none"><div class="card-header"><h3>' + icon('building') + t('ob.ours') + '</h3></div><div class="card-body tight" id="cf-company">' + U.companyForm(co) + '</div></div>' +
      '<div class="card" style="box-shadow:none"><div class="card-header"><h3>' + icon('target') + t('ob.icp') + '</h3></div><div class="card-body">' +
      '<div class="field"><label class="label">' + t('ob.q.countries') + '</label><div id="cf-countries"></div></div>' +
      '<div class="field"><label class="label">' + t('ob.q.customer_types') + ' <span class="req">*</span></label><div id="cf-types"></div></div>' +
      '<div class="field"><label class="label">' + t('ob.q.best_customers') + '</label><div id="cf-refs"></div></div>' +
      '<div class="field"><label class="label">' + t('ob.q.unwanted_customers') + '</label><div id="cf-ex"></div></div>' +
      '<div class="field"><label class="label">' + t('ob.q.contact_titles') + '</label><div id="cf-titles"></div></div>' +
      '<div class="field"><label class="label">' + t('ob.q.order_size') + '</label><input class="input" id="cf-order" value="' + esc(icp.order_size || '') + '"></div></div></div></div>' +
      '<div class="card" style="margin-top:12px;box-shadow:none"><div class="card-header"><h3>' + icon('cpu') + t('ob.modelPlan') + '</h3></div><div class="card-body" id="cf-models"></div></div>' +
      '<div class="row" style="margin-top:20px"><button class="btn btn-ghost" data-action="obGoStep" data-n="3">' + icon('chevronLeft') + t('qs.back_interview') + '</button><span class="grow"></span><button class="btn btn-ghost" data-action="obSkip" data-step="confirm">' + t('qs3.skipConfirm') + '</button><button class="btn btn-primary btn-lg" id="cf-ok">' + icon('check') + t('company.confirmProfile') + '</button></div>';
    const W = {
      countries: window.W.countryField(icp.countries || []), types: window.W.tagInput(icp.customer_types || [], { format: U.ctypeName }),
      refs: window.W.tagInput(icp.reference_customers || [], { placeholder: 'https://…' }), ex: window.W.tagInput([].concat(icp.exclude_companies || [], icp.exclude_words || [])), titles: window.W.tagInput(icp.contact_titles || []),
    };
    ['countries', 'types', 'refs', 'ex', 'titles'].forEach((k) => U.$('#cf-' + k, box).appendChild(W[k]));
    const models = (me) => {
      const el = U.$('#cf-models', box); if (!el) return;
      if (!rel.relays.length) { el.innerHTML = '<div class="small text-2">' + icon('info') + ' ' + t('qs.models_none') + '</div>'; return; }
      el.innerHTML = modelPlan(me, tier, true);
      const run = U.$('#mp-run', el); if (run) run.onclick = async () => { await api('POST', '/api/relay/evaluate', { confirmed: true }); toast(t('qs.models_running')); poll(); };
    };
    const poll = () => { const id = U.every(2500, async () => { const r = await api('GET', '/api/relay'); models(r.eval || {}); if (['done', 'applied', 'failed', 'awaiting_confirm'].includes((r.eval || {}).status)) clearInterval(id); }); };
    models(rel.eval || {});
    if (rel.relays.length && !['done', 'applied', 'failed', 'awaiting_confirm'].includes((rel.eval || {}).status)) poll();
    const companyDirty = () => Object.keys(U.companyPatch(box, co)).length > 0;
    const initialIcp = JSON.stringify({ countries: W.countries.get(), types: W.types.get(), refs: W.refs.get(), ex: W.ex.get(), titles: W.titles.get(), order: U.$('#cf-order', box).value });
    S.leaveGuard = () => box.isConnected && (companyDirty() || initialIcp !== JSON.stringify({ countries: W.countries.get(), types: W.types.get(), refs: W.refs.get(), ex: W.ex.get(), titles: W.titles.get(), order: U.$('#cf-order', box).value }));
    U.$('#cf-ok', box).onclick = async (e) => {
      const exs = W.ex.get();
      const newIcp = Object.assign({}, icp, { countries: W.countries.get(), customer_types: W.types.get(), reference_customers: W.refs.get(), exclude_companies: exs.filter((x) => /\.|https?:/.test(x)), exclude_words: exs.filter((x) => !/\.|https?:/.test(x)), contact_titles: W.titles.get(), order_size: U.$('#cf-order', box).value.trim() });
      if (!U.companyValid(box)) return;
      const newCo = U.companyUpdated(co, U.companyPatch(box, co));
      try {
        await busy(e.currentTarget, async () => {
          await api('PUT', '/api/quickstart/' + ob.id + '/profile', { company: newCo, icp: newIcp });
          const r = await api('POST', '/api/quickstart/' + ob.id + '/confirm', { tier: rel.relays.length ? tier : null });
          toast(t('qs.confirmed', { v: r.version }));
          S.meta = await api('GET', '/api/meta');
          S.leaveGuard = null; obGo(5);
        });
      } catch (err) { toastErr(err); }
    };
  }
  A.obExit = async () => { S.meta = await api('GET', '/api/meta'); U.go('dashboard'); };
})();

/* =====================================================================
   App core（按 design/prototype/app.js 的结构移植，接真实接口）
   - 状态 STATE、路由（页面 render 可以是 async）、主题/语言、通用组件 UI.*、事件委托 ACTIONS
   - 文案：design 的 i18n.js（原样复用）+ 我们的 /static/i18n/{zh,en}.json（后端事件、错误等），设计稿的键优先
   - 约定：交互用 data-action="名字" + ACTIONS[名字](el, event)；页面定时器用 every()/after()，切页自动清理
   ===================================================================== */
(function () {
  'use strict';
  const S = window.STATE = {
    route: 'dashboard', params: {}, tabs: {}, theme: 'light', meta: null,
    mgrOpen: window.innerWidth >= 1440,      // 其他页面右侧“主管”面板（≥1440px 默认展开）
    chatTarget: 'manager', wbExpand: null, advOpen: false,
    badges: { review: 0, learning: 0, alerts: 0, urgent: 0, chat: 0 },
  };

  /* ---------- 文案 ---------- */
  const ext = { zh: {}, en: {} };
  async function loadDicts() {
    for (const l of ['zh', 'en']) {
      try { ext[l] = await (await fetch('/static/i18n/' + l + '.json', { cache: 'no-cache' })).json(); } catch (e) { ext[l] = {}; }
      const d = I18N.dict[l];
      Object.keys(ext[l]).forEach((k) => { if (d[k] == null) d[k] = ext[l][k]; });
    }
  }
  function has(k) { return I18N.dict[I18N.lang][k] != null || I18N.dict.en[k] != null; }
  /* 在 i18n.js 的 t() 之上增加：@引用（参数里写 "@agent.monitor" 会被翻译）、error / country / fields 参数、参数自动转义 */
  window.t = function (key, vars) {
    let s = I18N.dict[I18N.lang][key];
    if (s == null) s = I18N.dict.en[key];
    if (s == null) { if (window.console) console.warn('[i18n] missing key:', key); return key.includes('.') ? key.split('.').pop() : key; }
    if (s.indexOf('{app}') >= 0) s = s.split('{app}').join(I18N.dict[I18N.lang]['app.name']);
    if (!vars) return s;
    return s.replace(/\{(\w+)\}/g, (m, v) => (vars[v] == null ? '' : fmtParam(v, vars[v], vars)));
  };
  function fmtParam(name, v, all) {
    if (typeof v === 'string' && v.startsWith('@')) return v.slice(1).split(',').map((x) => t(x.replace(/^@/, ''))).join(t('common.list_sep'));
    if (typeof v === 'string' && v.includes(',@')) return v.split(',').map((x) => fmtParam(name, x, all)).join(t('common.list_sep'));
    if (name === 'country' && /^[A-Z]{2}$/.test(v)) return countryName(v);
    if (name === 'error' && typeof v === 'string') return t('errors.' + v, all);
    if (name === 'fields') return String(v).split(',').map((x) => fmtParam('', x)).join(t('common.list_sep'));
    return esc(v);
  }

  /* ---------- 小工具 ---------- */
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function fmtNum(n) { return Number(n || 0).toLocaleString(I18N.lang === 'zh' ? 'zh-CN' : 'en-US'); }
  function fmtTokens(n) { n = Number(n || 0); if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'; if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K'; return String(n); }
  function money(n) { n = Number(n || 0); return '$' + n.toFixed(n > 0 && n < 0.01 ? 4 : 2); }
  /* 费用里有按参考价 / 估算算的：显示「约 $x（参考价）」/「约 $x（估算）」（PRD 2.4.1） */
  function moneyApprox(n, src) { return src && Number(n || 0) > 0 ? t('price.approx', { m: money(n), src: t('price.src.' + src) }) : money(n); }
  function hhmm(ts) { if (!ts) return ''; const d = new Date(ts * 1000); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
  function fmtTime(ts) { if (!ts) return ''; return new Date(ts * 1000).toLocaleString(I18N.lang === 'zh' ? 'zh-CN' : 'en-GB', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  function ago(ts) { if (!ts) return ''; const d = (Date.now() / 1000 - ts) / 86400; return d >= 1 ? t('time.days_ago', { n: Math.floor(d) }) : t('time.today'); }
  function txt(s) { return esc(s).replace(/[؀-ۿ][؀-ۿ\s]*[؀-ۿ]|[؀-ۿ]/g, (m) => '<bdi class="ar" dir="rtl">' + m + '</bdi>'); }
  function para(s) { return String(s || '').split('\n').filter(Boolean).map((x) => '<p>' + txt(x) + '</p>').join(''); }
  function countryName(cc) { const c = S.meta && S.meta.geo && S.meta.geo.countries[cc]; return c ? (I18N.lang === 'zh' ? c.zh : c.en) : (cc || ''); }
  function countryCell(cc) { return cc ? '<span class="row" style="gap:6px"><span class="cc">' + esc(cc) + '</span>' + esc(countryName(cc)) + '</span>' : '<span class="muted">—</span>'; }
  function ctypeName(v) { return has('ctype.' + v) ? t('ctype.' + v) : v; }
  function langName(code) { const l = S.meta && S.meta.geo && S.meta.geo.languages[code]; return l ? (I18N.lang === 'zh' ? l.zh : l.en) : code; }
  function evText(ev) { return ev && ev.key ? t(ev.key, ev.params || {}) : ''; }

  /* ---------- 接口 ---------- */
  class ApiError extends Error { constructor(key, params, status) { super(key); this.key = key; this.params = params || {}; this.status = status; } }
  async function api(method, path, body) {
    const init = { method, headers: {} };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
    let resp;
    try { resp = await fetch(path, init); } catch (e) { netDown(); throw new ApiError('server_unreachable'); }
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) throw new ApiError((data && data.error) || 'unexpected', data && data.params, resp.status);
    return data;
  }
  /* 后台服务连不上（例如被系统结束后正在自动重启）：显示“连接中断，正在重新连接…”，连上后自动刷新当前页面，不白屏 */
  let netTimer = null;
  function netDown() {
    if (netTimer) return;
    let bar = document.getElementById('net-down');
    if (!bar) {
      bar = document.createElement('div'); bar.id = 'net-down';
      bar.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:color-mix(in srgb,var(--color-bg) 70%,transparent);backdrop-filter:blur(2px)';
      bar.innerHTML = '<div class="card" style="padding:18px 24px;display:flex;gap:12px;align-items:center"><span class="spinner" style="width:16px;height:16px;border-width:2px"></span><b>' + esc(t('net.reconnecting')) + '</b></div>';
      document.body.appendChild(bar);
    }
    netTimer = setInterval(async () => {
      try {
        const r = await fetch('/api/meta', { cache: 'no-store' });
        if (r.ok) { clearInterval(netTimer); netTimer = null; const b = document.getElementById('net-down'); if (b) b.remove(); try { toast(t('net.restarted')); } catch (e) { /* ignore */ } render(); }
      } catch (e) { /* 继续等 */ }
    }, 2000);
  }
  /* 窗口只显示本软件的页面：外部链接一律用系统浏览器打开（桌面版通过 pywebview 的 open_external） */
  function openExternal(href) {
    try { if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external) { window.pywebview.api.open_external(href); return; } } catch (e) { /* ignore */ }
    window.open(href, '_blank', 'noopener');
  }
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    let u; try { u = new URL(a.getAttribute('href'), location.href); } catch (err) { return; }
    if (u.origin !== location.origin && /^(https?|mailto):$/.test(u.protocol)) { e.preventDefault(); e.stopPropagation(); openExternal(u.href); }
  }, true);
  window.open = ((orig) => function (href, target, feat) {
    try { const u = new URL(href, location.href); if (u.origin !== location.origin && window.pywebview && window.pywebview.api) { openExternal(u.href); return null; } } catch (e) { /* ignore */ }
    return orig.call(window, href, target, feat);
  })(window.open);
  function errText(e) {
    if (e instanceof ApiError) return t('errors.' + e.key, e.params) + (e.params && e.params.said ? ' ' + t('errors.provider_said', { provider: e.params.provider || '', said: e.params.said }) : '');
    if (window.console) console.error(e);
    return t('errors.unexpected', { detail: String((e && e.message) || e) });
  }

  /* ---------- 组件：AI 身份 ---------- */
  function agentState(key) { return (S.overview && S.overview.agents || []).find((a) => a.key === key) || { key, enabled: true, status: 'idle' }; }
  function agentKeys() { return ((S.meta && S.meta.agents) || []).map((a) => a.key); }
  function agentNick(key) { return { manager: 'Atlas', researcher: 'Scout', monitor: 'Sentry', analyst: 'Prism', sales: 'Nova', followup: 'Loop' }[key] || ''; }
  function avatar(key, size, opts) {
    opts = opts || {};
    const a = agentState(key);
    const presence = opts.presence ? '<span class="presence ' + (a.enabled ? ({ working: 'working', error: 'error', stuck: 'error', paused: 'paused' }[a.status] || '') : '') + '"></span>' : '';
    const ic = key === 'client' ? icon('user') : icon('a_' + key);
    return '<span class="agent-avatar ' + (size || '') + (opts.solid ? ' solid' : '') + (!a.enabled && opts.dimOff ? ' is-off' : '') + '" data-agent="' + key + '" title="' + esc(t('agent.' + key)) + '">' + ic + presence + '</span>';
  }
  function agentChip(key, size) { return '<span class="agent-chip" data-agent="' + key + '">' + avatar(key, size || 'sm') + '<span>' + esc(t('agent.' + key)) + '</span></span>'; }
  function statusOf(a) {
    if (!a.enabled || a.status === 'off') return { cls: 'dot-off', label: t('st.off') };
    const map = { working: ['dot-live', 'st.working'], waiting: ['dot-warn', 'st.waiting'], paused: ['dot-warn', 'st.paused'], error: ['dot-error', 'st.error'], stuck: ['dot-error', 'astate.stuck'], idle: ['dot-idle', 'st.idle'] };
    const m = map[a.status] || map.idle;
    return { cls: m[0], label: t(m[1]) };
  }
  function statusPill(a) { const s = statusOf(a); return '<span class="status" data-agent="' + a.key + '"><span class="dot ' + s.cls + '"></span>' + esc(s.label) + '</span>'; }

  /* ---------- 组件：基础 ---------- */
  function grade(g, lg) { return '<span class="grade grade-' + esc(g) + (lg ? ' grade-lg' : '') + '" title="' + esc(g) + '">' + esc(g) + '</span>'; }
  function tagHtml(text, cls, ic) { return '<span class="tag ' + (cls || '') + '">' + (ic ? icon(ic) : '') + esc(text) + '</span>'; }
  function sw(checked, attrs) { return '<label class="switch" ' + (attrs || '') + '><input type="checkbox" ' + (checked ? 'checked' : '') + '><span class="track"></span></label>'; }
  function lockTag(label) { return '<span class="tag tag-outline" title="' + esc(t('lock.tip')) + '">' + icon('lock') + esc(label || t('lock.you')) + '</span>'; }
  function mgrTag() { return '<span class="tag" data-agent="manager" title="' + esc(t('lock.mgrTip')) + '">' + icon('a_manager') + esc(t('lock.mgr')) + '</span>'; }
  function empty(o) {
    return '<div class="empty ' + (o.error ? 'error' : '') + '"><div class="empty-art">' + icon(o.icon || 'inbox') + '</div><h4>' + esc(o.title) + '</h4>' +
      (o.body ? '<p>' + esc(o.body) + '</p>' : '') + (o.actions ? '<div class="row" style="justify-content:center">' + o.actions + '</div>' : '') + '</div>';
  }
  function skeleton(n) { return '<div class="card card-body">' + Array.from({ length: n || 4 }, (_, i) => '<div class="skeleton" style="width:' + [90, 70, 80, 60][i % 4] + '%;margin-bottom:10px"></div>').join('') + '</div>'; }
  function tabs(page, list, def) {
    const cur = S.tabs[page] || def;
    return '<div class="tabs">' + list.map((x) => '<button class="tab' + (cur === x[0] ? ' active' : '') + '" data-action="tab" data-page="' + page + '" data-tab="' + x[0] + '">' + (x[2] ? icon(x[2]) : '') + esc(x[1]) + (x[3] ? '<span class="count">' + x[3] + '</span>' : '') + '</button>').join('') + '</div>';
  }

  /* ---------- 弹窗 / 抽屉 / Toast ---------- */
  function openModal(o) {
    closeModal();
    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop'; wrap.id = 'modal';
    wrap.innerHTML = '<div class="modal ' + (o.cls || '') + '" role="dialog" aria-modal="true">' +
      '<div class="modal-header">' + (o.headIcon || '') + '<div class="grow"><h3>' + o.title + '</h3>' + (o.sub ? '<p>' + o.sub + '</p>' : '') + '</div>' +
      '<button class="btn btn-ghost btn-icon btn-sm" data-action="closeModal" aria-label="close">' + icon('x') + '</button></div>' +
      '<div class="modal-body">' + o.body + '</div>' + (o.footer !== false ? '<div class="modal-footer">' + (o.footer || '') + '</div>' : '') + '</div>';
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap && !o.sticky) closeModal(); });
    document.body.appendChild(wrap);
    if (o.onOpen) o.onOpen(wrap);
    const first = o.noFocus ? null : wrap.querySelector('textarea, input:not([type=checkbox]):not([type=radio]), select');
    if (first) setTimeout(() => first.focus(), 30);
    return wrap;
  }
  function closeModal() { const m = $('#modal'); if (m) m.remove(); }
  /* 确认框：返回 Promise<boolean>；onOk 可以是 async（抛错时显示错误、不关闭） */
  function confirmDialog(o) {
    return new Promise((resolve) => {
      const w = openModal({
        title: esc(o.title), cls: (o.cls || 'sm') + ' ' + (o.danger ? 'danger' : ''),
        headIcon: o.danger ? '<div class="risk-icon">' + icon('alert', 'lg') + '</div>' : (o.headIcon || ''),
        body: (o.html ? o.body : '<p class="text-2">' + esc(o.body) + '</p>') + (o.extra || ''),
        footer: '<button class="btn btn-secondary" data-x>' + t('c.cancel') + '</button><button class="btn ' + (o.danger ? 'btn-danger' : (o.okCls || 'btn-primary')) + '" id="confirm-ok">' + esc(o.ok || t('c.confirm')) + '</button>',
      });
      $('[data-x]', w).onclick = () => { closeModal(); resolve(false); };
      $('#confirm-ok', w).onclick = async (e) => {
        if (o.validate && !o.validate(w)) return;
        if (o.onOk) { try { await busy(e.currentTarget, () => o.onOk(w)); } catch (err) { toast(errText(err), 'error'); return; } }
        closeModal(); resolve(true);
      };
    });
  }
  /* 风险确认：勾选“我已了解风险，自行承担”后才能开启；开启时间记录在本机（后端 risk_log） */
  function riskDialog(feature, onOk) {
    const risks = [1, 2, 3].map((i) => 'riskf.' + feature + '.r' + i).filter(has);
    const w = openModal({
      title: t('risk.title', { f: t('riskf.' + feature) }), cls: 'risk',
      headIcon: '<div class="risk-icon">' + icon('alert', 'lg') + '</div>',
      sub: has('riskf.' + feature + '.desc') ? t('riskf.' + feature + '.desc') : '',
      body: '<p class="small text-2" style="margin-bottom:10px">' + t('risk.lead') + '</p>' +
        '<ul class="risk-list">' + risks.map((k) => '<li>' + icon('alert') + '<span>' + t(k) + '</span></li>').join('') + '</ul>' +
        '<div class="risk-box small text-2" style="margin-top:14px;display:flex;gap:8px">' + icon('shieldCheck') + '<span>' + t('risk.safeguard') + '</span></div>' +
        '<label class="checkbox risk-confirm" style="margin-top:14px;display:flex"><input type="checkbox" id="risk-agree"> ' + t('risk.agree') + '</label>',
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="risk-ok" disabled>' + t('risk.enable') + '</button>',
    });
    const cb = $('#risk-agree', w), ok = $('#risk-ok', w);
    cb.onchange = () => { ok.disabled = !cb.checked; };
    ok.onclick = async () => {
      try { await busy(ok, () => api('POST', '/api/settings/risk', { feature, enable: true, confirm: true })); } catch (err) { toast(errText(err), 'error'); return; }
      closeModal(); toast(t('risk.logged'), 'success'); if (onOk) onOk();
    };
  }
  function openDrawer(html) {
    closeDrawer();
    const bd = document.createElement('div'); bd.className = 'drawer-backdrop'; bd.id = 'drawer-bd'; bd.onclick = closeDrawer;
    const d = document.createElement('aside'); d.className = 'drawer'; d.id = 'drawer'; d.innerHTML = html;
    document.body.appendChild(bd); document.body.appendChild(d);
    return d;
  }
  function closeDrawer() { ['#drawer', '#drawer-bd'].forEach((s) => { const el = $(s); if (el) el.remove(); }); }
  function toast(msg, type) {
    const box = $('#toasts');
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.innerHTML = icon(type === 'error' ? 'xCircle' : type === 'warn' ? 'alert' : 'checkCircle') + '<span>' + msg + '</span>';
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 300); }, type === 'error' ? 6000 : 2800);
  }
  const toastErr = (e) => toast(errText(e), 'error');
  async function busy(btn, fn) {
    if (!btn) return fn();
    const html = btn.innerHTML;
    btn.disabled = true; btn.classList.add('is-loading');
    btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;border-width:2px"></span>' + html;
    try { return await fn(); } finally { if (btn.isConnected) { btn.disabled = false; btn.classList.remove('is-loading'); btn.innerHTML = html; } }
  }

  /* ---------- 定时器（切页清理） ---------- */
  let timers = [];
  function every(ms, fn) { const id = setInterval(fn, ms); timers.push(() => clearInterval(id)); return id; }
  function after(ms, fn) { const id = setTimeout(fn, ms); timers.push(() => clearTimeout(id)); return id; }
  function onLeave(fn) { timers.push(fn); }
  function clearTimers() { timers.forEach((f) => { try { f(); } catch (e) { /* ignore */ } }); timers = []; }

  /* ---------- 主题 ---------- */
  const mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  function applyTheme() {
    const resolved = S.theme === 'system' ? (mql && mql.matches ? 'dark' : 'light') : S.theme;
    document.documentElement.setAttribute('data-theme', resolved);
    const btn = $('#theme-btn'); if (btn) btn.innerHTML = icon(resolved === 'dark' ? 'sun' : 'moon');
  }
  if (mql && mql.addEventListener) mql.addEventListener('change', () => S.theme === 'system' && applyTheme());
  function loadPrefs() { try { const p = JSON.parse(localStorage.getItem('ja-prefs') || '{}'); if (p.theme) S.theme = p.theme; if (p.mgrOpen != null && window.innerWidth >= 1360) S.mgrOpen = p.mgrOpen; } catch (e) { /* ignore */ } }
  function savePrefs() { try { localStorage.setItem('ja-prefs', JSON.stringify({ theme: S.theme, mgrOpen: S.mgrOpen })); } catch (e) { /* ignore */ } }

  /* ---------- 导航（PRD §1：工作台 · 任务管理 · 线索库 · 模板管理 · 渠道池 · 会议室 · AI 记忆 · 账号池 · 设置） ---------- */
  /* 渠道拓展员（范围决定 #5）的图标：设计稿里还没有，这里补一个指南针（不改设计稿的 icons.js） */
  if (window.ICON_PATHS && !window.ICON_PATHS.a_scout) window.ICON_PATHS.a_scout = '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2.2 4.8-4.8 2.2 2.2-4.8z"/><path d="M12 3v1.5M12 19.5V21M3 12h1.5M19.5 12H21"/>';
  const NAV_ALL = [
    { r: 'dashboard', i: 'home' },
    { r: 'run', i: 'activity' },                       // 任务管理：唯一入口（别处只用文字提示“在左边「任务管理」里…”）
    { r: 'review', i: 'inbox', badge: () => S.badges.review, feature: 'review' },   // v1 没有审核环节（FEATURES.review）
    { r: 'leads', i: 'database' },
    { r: 'icp', i: 'target' },                         // 模板管理：唯一入口（快速开始里建第一个模板除外）
    { r: 'channels', i: 'globe' },
    { r: 'learning', i: 'brain', badge: () => S.badges.learning, soft: true },
    { r: 'accounts', i: 'users' },
    { r: 'settings', i: 'settings' },
  ];
  /* 不在导航里、只能用网址打开：chat 单点对话（→工作台 @某 AI）· onboarding 快速开始；groups → 渠道池。
     任务管理（run）和模板管理（icp）在导航里，而且只有导航这一个入口（老板要求，2026-10-14） */
  const ALIAS = { quickstart: 'onboarding', tasks: 'run', icps: 'icp', alerts: 'dashboard', about: 'settings' };
  const feat = (k) => !!(S.meta && S.meta.features && S.meta.features[k]);
  function renderNav() {
    const NAV_MAIN = NAV_ALL.filter((it) => !it.feature || feat(it.feature));
    $('#nav').innerHTML = NAV_MAIN.map((it) => {
      const b = it.badge ? it.badge() : 0;
      return '<button class="nav-item' + (S.route === it.r ? ' active' : '') + '" data-action="go" data-route="' + it.r + '">' + icon(it.i, 'lg') +
        '<span class="nav-label">' + t('nav.' + it.r) + '</span>' + (b ? '<span class="nav-badge' + (it.soft ? ' soft' : '') + '">' + b + '</span>' : '') + '</button>';
    }).join('');
    const ags = (S.overview && S.overview.agents) || [];
    const working = ags.filter((a) => a.enabled && a.status === 'working').length;
    $('#sidebar-foot').innerHTML = '<div class="team-pulse"><div class="mini-avatars">' + agentKeys().map((k) => avatar(k, 'sm', { dimOff: true })).join('') + '</div><span>' + t('foot.working', { n: working }) + '</span></div>' +
      browserFoot() +
      '<div class="xs muted" style="margin-top:8px">v' + esc(S.meta.version) + ' · ' + t('app.local') + '</div>';
  }
  /* 浏览器组件（安装包第一次打开时后台下载，第 5a 步）：下载中显示进度，失败可以重试；装好后不显示 */
  function browserFoot() {
    const b = S.browser;
    if (!b || !b.managed || b.state === 'ready') return '';
    if (b.state === 'downloading') return '<div class="xs" style="margin-top:8px" title="' + esc(t('br.waitHint')) + '">' + t('br.downloading', { p: b.progress || 0 }) + '<div class="progress sm" style="margin-top:4px"><span style="width:' + (b.progress || 0) + '%"></span></div></div>';
    return '<div class="xs" style="margin-top:8px;color:var(--color-warning)">' + t('br.failed') + ' <button class="btn btn-ghost btn-sm" data-action="browserRetry">' + t('br.retry') + '</button></div>';
  }
  function renderTop() {
    const page = window.PAGES[S.route] || {};
    const title = page.title ? page.title() : t('nav.' + S.route);
    $('#page-title').textContent = title;
    document.title = t('app.name') + ' · ' + title;
    const mt = $('#mgr-toggle');
    mt.classList.toggle('hidden', S.route === 'dashboard' || !!page.fullscreen);
    mt.classList.toggle('active', !!S.mgrOpen);
    mt.innerHTML = avatar('manager', 'sm') + '<span>' + t('mc.toggle') + '</span>' + (S.badges.chat ? '<span class="dot dot-error"></span>' : '');
    $('#bell-btn').innerHTML = icon('bell', 'lg') + (S.badges.alerts ? '<span class="dot ' + (S.badges.urgent ? 'dot-error' : 'dot-warn') + '" style="position:absolute;top:7px;right:8px"></span>' : '');
    $('#lang-seg').innerHTML = ['zh', 'en'].map((l) => '<button class="' + (I18N.lang === l ? 'active' : '') + '" data-action="setLang" data-lang="' + l + '">' + (l === 'zh' ? '中文' : 'EN') + '</button>').join('');
    applyI18n(document.querySelector('.topbar'));
    applyI18n(document.querySelector('.brand'));
  }

  /* ---------- 路由 ---------- */
  function parseHash() {
    const h = location.hash.replace(/^#\/?/, '');
    const [path, query] = h.split('?');
    const parts = path.split('/').filter(Boolean);
    return { parts, params: Object.fromEntries(new URLSearchParams(query || '')) };
  }
  async function go(route, params) {
    const q = params ? '?' + new URLSearchParams(params).toString() : '';
    const target = '#/' + route + q;
    if (location.hash === target) {
      if (S.leaveGuard && S.leaveGuard() && !(await confirmDialog({ title: t('c.unsavedTitle'), body: t('c.unsavedBody'), ok: t('c.leaveAnyway') }))) return;
      S.leaveGuard = null; render();
    } else location.hash = target;
  }
  let renderSeq = 0;
  async function render(opts) {
    const seq = ++renderSeq;
    const { parts, params } = parseHash();
    let route = ALIAS[parts[0]] || parts[0] || 'dashboard';
    if (route === 'review' && !feat('review')) route = 'leads';      // v1 没有审核台
    if (route === 'groups') { route = 'channels'; S.tabs.channels = S.tabs.channels || 'joined'; }
    if (S.meta.first_launch && route !== 'onboarding') { location.hash = '#/onboarding'; return; }
    if (!window.PAGES[route]) route = 'dashboard';
    if (parts[1]) S.tabs[route] = parts[1];
    const changed = S.route !== route;
    S.route = route; S.params = params;
    clearTimers(); if (!(opts && opts.keepDrawer)) closeDrawer();
    const page = window.PAGES[route];
    const full = !!page.fullscreen;
    document.body.classList.toggle('is-fullscreen', full);
    $('#fullscreen').classList.toggle('hidden', !full);
    const view = $('#view');
    const host = full ? $('#fullscreen') : view;
    if (!full) view.classList.toggle('content-fill', !!page.fill);
    renderNav(); renderTop();
    if (window.MGR) window.MGR.renderPanel();
    if (changed && !full) view.innerHTML = '<div class="content-inner">' + skeleton(4) + '</div>';
    let html;
    try { html = await page.render(params); } catch (e) { html = '<div class="alert alert-danger">' + icon('xCircle') + '<span>' + errText(e) + '</span></div>'; }
    if (seq !== renderSeq) return;
    if (full) { host.innerHTML = html; if (changed) host.scrollTop = 0; } else {
      const top = view.scrollTop;
      view.innerHTML = '<div class="content-inner">' + html + '</div>';
      view.scrollTop = changed ? 0 : top;
    }
    if (full) $('#view').innerHTML = '';
    if (page.mount) { try { await page.mount(host, params); } catch (e) { toastErr(e); } }
  }
  const refresh = () => render({ keepScroll: true });

  /* ---------- 角标（审核 / 记忆待确认 / 提醒 / 主管未读）与工作台概况 ---------- */
  let lastAlertId = 0;
  async function refreshBadges() {
    try {
      const [ov, al, un, lo, br] = await Promise.all([api('GET', '/api/workbench'), api('GET', '/api/alerts/since?after=' + lastAlertId), api('GET', '/api/chat/unread'), api('GET', '/api/learning/overview'), api('GET', '/api/browser').catch(() => null)]);
      S.overview = ov; S.browser = br;
      S.badges = { review: feat('review') ? ov.review_pending : 0, alerts: al.unread, urgent: al.urgent, chat: un.manager || 0, learning: (lo.pending || []).length + (lo.memos_pending || 0) };
      if (lastAlertId && 'Notification' in window && Notification.permission === 'granted') {
        al.items.filter((a) => a.level === 'urgent' || a.category === 'high_intent_round').forEach((a) => { try { new Notification(t('app.name'), { body: t('alert.' + a.category + '.title', a.params) }); } catch (e) { /* ignore */ } });
      }
      al.items.forEach((a) => { lastAlertId = Math.max(lastAlertId, a.id); });
      if (!lastAlertId) lastAlertId = 1;
      renderNav(); renderTop();
    } catch (e) { /* 服务暂时不可用：下次再试 */ }
  }

  /* ---------- 全局事件委托 ---------- */
  const ACTIONS = window.ACTIONS = {
    go: (el) => go(el.dataset.route, el.dataset.q ? Object.fromEntries(new URLSearchParams(el.dataset.q)) : null),
    closeModal: () => closeModal(),
    browserRetry: async () => { try { S.browser = await api('POST', '/api/browser/install'); renderNav(); } catch (e) { /* ignore */ } },
    closeDrawer: () => closeDrawer(),
    setLang: async (el) => {
      if (S.leaveGuard && S.leaveGuard() && !(await confirmDialog({ title: t('c.unsavedTitle'), body: t('c.unsavedBody'), ok: t('c.leaveAnyway') }))) return;
      S.leaveGuard = null;
      I18N.setLang(el.dataset.lang);
      try { await api('POST', '/api/settings/ui', { lang: el.dataset.lang }); } catch (e) { /* ignore */ }
      closeModal(); applyI18n(document); render();
    },
    toggleTheme: () => { const cur = document.documentElement.getAttribute('data-theme'); S.theme = cur === 'dark' ? 'light' : 'dark'; savePrefs(); applyTheme(); },
    setTheme: (el) => { S.theme = el.dataset.theme; savePrefs(); applyTheme(); refresh(); },
    tab: (el) => { S.tabs[el.dataset.page] = el.dataset.tab; location.hash = '#/' + el.dataset.page + '/' + el.dataset.tab; },
  };
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const fn = ACTIONS[el.dataset.action];
    if (fn) { e.preventDefault(); Promise.resolve(fn(el, e)).catch(toastErr); }
  });
  document.addEventListener('change', (e) => {
    const el = e.target.closest('[data-change]');
    if (!el) return;
    const fn = ACTIONS[el.dataset.change];
    if (fn) Promise.resolve(fn(el, e)).catch(toastErr);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { if ($('#modal')) closeModal(); else closeDrawer(); } });

  window.UI = {
    $, $$, esc, fmtNum, fmtTokens, money, moneyApprox, hhmm, fmtTime, ago, txt, para, countryName, countryCell, ctypeName, langName, evText, has,
    api, ApiError, errText, toast, toastErr, busy, avatar, agentChip, agentState, agentNick, agentKeys, statusOf, statusPill, grade, tag: tagHtml, sw, lockTag, mgrTag,
    empty, skeleton, tabs, openModal, closeModal, confirmDialog, riskDialog, openDrawer, closeDrawer, every, after, onLeave, go, render, refresh,
    renderNav, renderTop, refreshBadges, savePrefs, loadDicts, loadPrefs, applyTheme, feat,
  };
  window.PAGES = window.PAGES || {};
})();

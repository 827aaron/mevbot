/* 页面（一）：工作台（和主管对话为中心）、审核台、线索库 + 线索详情、「这条不好」；
   隐藏路由：run（任务实时视图 + 新任务 + 历史）、icp（理想客户模板）。小组件：标签输入、国家选择。 */
(function () {
  'use strict';
  const S = window.STATE, U = window.UI, A = window.ACTIONS, P = window.PAGES;
  const { esc, avatar, grade, sw, empty, money, api, toast, toastErr, busy } = U;

  /* =================================================================
     小组件：标签输入 / 国家选择（先选大区，再勾国家）
     ================================================================= */
  function tagInput(values, opts) {
    opts = opts || {};
    const fmt = opts.format || ((v) => v);
    const wrap = document.createElement('div');
    wrap.className = 'tag-input';
    const st = { values: (values || []).slice() };
    const listId = 'dl' + Math.random().toString(36).slice(2);
    const draw = () => {
      wrap.innerHTML = st.values.map((v, i) => '<span class="tag">' + esc(fmt(v)) + '<button type="button" data-i="' + i + '" aria-label="remove">×</button></span>').join('') +
        '<input type="text" placeholder="' + esc(st.values.length ? '' : (opts.placeholder || '')) + '" list="' + listId + '">' +
        (opts.suggestions && opts.suggestions.length ? '<datalist id="' + listId + '">' + opts.suggestions.map((s) => '<option value="' + esc(s) + '">' + esc(fmt(s)) + '</option>').join('') + '</datalist>' : '');
      const inp = wrap.querySelector('input');
      inp.addEventListener('keydown', (e) => {
        if ((e.key === 'Enter' || e.key === ',' || e.key === '，') && inp.value.trim()) { e.preventDefault(); add(inp.value); }
        else if (e.key === 'Backspace' && !inp.value && st.values.length) { st.values.pop(); draw(); wrap.querySelector('input').focus(); }
      });
      inp.addEventListener('change', () => { if (inp.value.trim() && (opts.suggestions || []).includes(inp.value.trim())) add(inp.value); });
      inp.addEventListener('blur', () => { if (inp.value.trim()) add(inp.value, false); });
    };
    const add = (raw, focus) => {
      raw.split(/[,，\n]/).map((x) => x.trim()).filter(Boolean).forEach((v) => { if (!st.values.includes(v)) st.values.push(v); });
      draw(); if (focus !== false) wrap.querySelector('input').focus();
    };
    wrap.addEventListener('click', (e) => { const b = e.target.closest('button[data-i]'); if (b) { st.values.splice(+b.dataset.i, 1); draw(); } else wrap.querySelector('input').focus(); });
    draw();
    wrap.get = () => { const inp = wrap.querySelector('input'); if (inp && inp.value.trim()) add(inp.value, false); return st.values.slice(); };
    wrap.set = (v) => { st.values = (v || []).slice(); draw(); };
    return wrap;
  }
  /* 目标国家（老板决定）：属于业务内容，没有国家选择器 —— 客户在补充访谈或对话里说，这里只显示；没说 = 不限国家 */
  function countryField(selected) {
    const el = document.createElement('div');
    let v = (selected || []).slice();
    const draw = () => {
      el.innerHTML = '<div class="row wrap">' + (v.length ? v.map((c) => '<span class="tag tag-primary">' + esc(U.countryName(c)) + '</span>').join('') : '<span class="tag">' + t('icp3.anyCountry') + '</span>') + '</div><span class="hint">' + t('icp3.countryHint') + '</span>';
    };
    draw();
    el.get = () => v.slice(); el.set = (x) => { v = (x || []).slice(); draw(); };
    return el;
  }
  function adaptation(codes) {
    if (!codes || !codes.length) return '';
    // 只显示搜索语言（PRD §3）；时区数据留在 countries.py，以后发消息时再用
    const langs = [];
    codes.forEach((c) => { const x = S.meta.geo.countries[c]; if (!x) return; x.langs.forEach((l) => { if (!langs.includes(l)) langs.push(l); }); });
    return '<div class="kv small"><span class="k">' + t('icp2.adLang') + '</span><span>' + langs.map(U.langName).join(t('common.list_sep')) + '</span></div>';
  }
  window.W = { tagInput, countryField, adaptation };

  /* =================================================================
     通用：线索的联系方式 / 状态 / 来源
     ================================================================= */
  const SRC_ICON = { google: 'search', web: 'globe', youtube: 'youtube', tg: 'telegram', facebook: 'facebook', instagram: 'instagram' };
  function srcTag(s) { return '<span class="tag">' + icon(SRC_ICON[s] || 'globe') + esc(t('source.' + s)) + '</span>'; }
  function contactsOf(l) {
    const cs = l.contacts || [];
    return {
      tg: l.primary_tg || (cs.find((c) => c.telegram) || {}).telegram || '',
      wa: l.primary_wa || (cs.find((c) => c.whatsapp && c.wa_explicit) || {}).whatsapp || '',
      waMaybe: (cs.find((c) => c.whatsapp && !c.wa_explicit) || {}).whatsapp || '',
      email: l.primary_email || (cs.find((c) => c.email) || {}).email || '',
    };
  }
  /* TG / WhatsApp 为主（必须有其一才算合格），邮箱为辅 */
  function contactLine(l, withEmail) {
    const c = contactsOf(l), parts = [];
    if (c.tg) parts.push('<span class="ct ct-tg">' + icon('telegram') + esc(c.tg) + '</span>');
    if (c.wa) parts.push('<span class="ct ct-wa">' + icon('whatsapp') + esc(c.wa) + '</span>');
    if (!c.wa && c.waMaybe) parts.push('<span class="ct ct-wa" title="' + esc(t('wa.maybe')) + '">' + icon('whatsapp') + esc(c.waMaybe) + ' <span class="tag tag-warning">' + t('wa.maybe') + '</span></span>');
    if (!c.tg && !c.wa) parts.push('<span class="tag tag-warning">' + icon('alert') + t('leads.noTgWa') + '</span>');
    if (withEmail && c.email) parts.push('<span class="ct ct-mail">' + icon('mail') + esc(c.email) + '</span>');
    return '<span class="row wrap" style="gap:4px 12px">' + parts.join('') + '</span>';
  }
  const ST_CLS = { qualified: 'tag-primary', pushed: 'tag-success', pending_review: 'tag-primary', approved: 'tag-success', rejected: 'tag-danger', backup: 'tag-outline', contacted: 'tag-info', replied: 'tag-success', unsubscribed: '', discarded: 'tag-danger', expired: 'tag-warning' };
  function statusTag(l) { return (l.do_not_contact ? '<span class="tag tag-danger">' + icon('hand') + t('lead.dnc') + '</span> ' : '') + (l.deal ? '<span class="tag tag-success">' + icon('flag') + t('leads.d.won') + '</span> ' : '') + '<span class="tag ' + (ST_CLS[l.status] || '') + '">' + esc(t('lstatus.' + l.status)) + '</span>'; }
  window.contactLine = contactLine;

  /* =================================================================
     「这条不好」→ 说原因 → 主管在对话里一句话回复 +【撤销】
     ================================================================= */
  function notFit(lead, title) {
    const picked = new Set();
    const reasons = (S.meta.feedback_reasons || []).filter((r) => r !== 'other');
    const w = U.openModal({
      cls: 'sm', title: esc(title || t('nf.title')), sub: esc(t('nf.sub')), headIcon: avatar('manager', 'lg'),
      body: '<div class="small text-2" style="margin-bottom:8px">' + grade(lead.grade) + ' <b>' + esc(lead.company_name) + '</b></div>' +
        '<div class="row wrap" id="nf-chips">' + reasons.map((r) => '<span class="chip chip-sm" data-k="' + r + '">' + esc(t('fbr.' + r)) + '</span>').join('') + '</div>' +
        '<textarea class="textarea" id="nf-text" style="margin-top:12px;min-height:72px" placeholder="' + esc(t('nf3.ph')) + '"></textarea>',
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="nf-ok" disabled>' + icon('send') + t('nf.send') + '</button>',
    });
    const ok = U.$('#nf-ok', w), ta = U.$('#nf-text', w);
    const upd = () => { ok.disabled = !picked.size && !ta.value.trim(); };
    U.$$('#nf-chips .chip', w).forEach((c) => { c.onclick = () => { c.classList.toggle('active'); c.classList.contains('active') ? picked.add(c.dataset.k) : picked.delete(c.dataset.k); upd(); }; });
    ta.oninput = upd;
    ok.onclick = async () => {
      const rs = [...picked]; if (!rs.length) rs.push('other');
      try { await busy(ok, () => api('POST', '/api/review/' + lead.id, { action: 'reject', reasons: rs, note: ta.value.trim() })); } catch (e) { toastErr(e); return; }
      U.closeModal(); U.closeDrawer();
      if (S.route === 'review') S.reviewDone[lead.id] = { act: 'reject', lead };
      toast(t('nf.recorded'));
      await window.MGR.managerUpdated();
      U.refreshBadges(); U.refresh();
    };
  }
  window.notFit = notFit;

  /* =================================================================
     ① 工作台：一行目标 · 6 个 AI（3×2，点开看实时动态）· 其余全部是和主管的对话
     ================================================================= */
  const AG = () => U.agentKeys();   // v1：不含销售员 / 跟进员（后端 FEATURES["outreach"]）
  function shortStatus(a) { return a.unconfigured ? t('wb3.cfgWhere') : a.short ? t(a.short.key, a.short.params) : t('st.' + (a.status || 'idle')); }
  function miniCard(a) {
    const s = U.statusOf(a), open = S.wbExpand === a.key, locked = a.step >= 3 && (a.key === 'sales' || a.key === 'followup');
    return '<div class="card agent-mini' + (a.enabled ? '' : ' is-off') + (open ? ' is-open' : '') + '" data-agent="' + a.key + '">' +
      '<div class="agent-mini-main" data-action="wbExpand" data-key="' + a.key + '" title="' + esc(t('wb.clickHint')) + '">' + avatar(a.key, 'sm', { dimOff: true }) +
      '<span class="dot ' + s.cls + '"></span><span class="ellipsis"><b>' + esc(t('agent.' + a.key)) + '</b><span class="muted"> · ' + shortStatus(a) + '</span></span></div>' +
      (locked ? '<span class="tag">' + t('wb2.step3') + '</span>'
        // U12：没配置的 AI 不放开关（开着又“未配置”是矛盾的），只写去哪里配置
        : a.unconfigured ? '<span class="tag tag-warning" title="' + esc(t('wb3.cfgWhere')) + '">' + t('wb.s.unconfigured') + '</span>'
          : sw(a.enabled, 'data-agent="' + a.key + '" data-change="toggleAgent" data-key="' + a.key + '"')) + '</div>';
  }
  function feedItem(e) {
    return '<div class="feed-item" data-agent="' + e.agent + '">' + avatar(e.agent === 'system' ? 'manager' : e.agent, 'sm') + '<div class="grow"><div class="meta"><b style="color:var(--agent)">' + esc(t('agent.' + e.agent)) + '</b><span>' + U.hhmm(e.ts) + '</span></div>' +
      '<div class="body">' + U.evText(e) + '</div></div></div>';
  }
  window.feedItem = feedItem;
  function expandPanel(a) {
    const key = a.key;
    return '<div class="card wb-expand" data-agent="' + key + '"><div class="card-header">' + avatar(key, 'sm', { presence: true }) + '<h3>' + esc(t('wb.liveOf', { name: t('agent.' + key) })) + '</h3>' + U.statusPill(a) +
      '<div class="actions"><button class="btn btn-ghost btn-sm" data-action="mgrTarget" data-k="' + key + '">' + icon('message') + t('dash.chatWith') + '</button><button class="btn btn-ghost btn-icon btn-sm" data-action="wbExpand" data-key="' + key + '">' + icon('chevronUp') + '</button></div></div>' +
      '<div class="agent-live" style="margin:12px 20px 0"><span class="dot ' + U.statusOf(a).cls + '"></span><span class="agent-live-text" id="live-' + key + '">' + (!a.enabled ? esc(t('offImpact.' + key)) : a.now ? U.evText(a.now) : esc(t('wb2.nothing'))) + '</span></div>' +
      '<div class="feed" id="wb-feed" style="max-height:220px;overflow:auto;margin-top:6px"><div class="row small muted" style="padding:12px 20px"><span class="spinner"></span>' + t('c.loading') + '</div></div></div>';
  }
  /* U54：预算没改过 → “默认值，可以改”；保存过 → 🔒你设定 */
  const budgetTag = (b) => b.custom ? U.lockTag() : '<span class="tag tag-outline">' + t('wb3.budgetDefault') + '</span>';
  /* U11：目标行只写“今天合格 N 个 · 待推送 M 个”，不和某个任务比（任务进度在「任务管理」里看） */
  function goalRow(ov) {
    const g = ov.goal, b = ov.budget;
    return '<div class="card wb-top"><span class="text-2">' + icon('target', 'lg') + '</span>' +
      '<b class="wb-goal ellipsis">' + t('wb3.today', { n: g.done, a: g.A, b: g.B }) + '</b><span class="muted small">' + t('wb3.pending', { n: g.pending_push || 0 }) + '</span>' +
      (g.running ? '' : '<span class="xs muted">' + t('wb3.startHint') + '</span>') + '<span class="grow"></span>' +
      '<span class="wb-sep"></span><span class="muted small">' + t('wb.cost') + '</span><b class="num">' + money(b.day_used) + '</b><span class="muted num small">/ ' + (b.day_limit > 0 ? money(b.day_limit) : '∞') + '</span>' + budgetTag(b) + '</div>';
  }
  /* v1 只有 4 个 AI（不含销售员 / 跟进员）：一行排开；以后 6 个时回到设计稿的 3×2 */
  function agentGrid() { const n = AG().length; return n && n <= 4 ? 'grid-template-columns:repeat(' + n + ',minmax(0,1fr))' : ''; }
  function pauseControls(ov) {
    return '<div class="alert ' + (ov.paused_all ? 'alert-warning' : 'alert-info') + '">' + icon(ov.paused_all ? 'pause' : 'play') +
      '<span class="grow"><b>' + t(ov.paused_all ? 'pause.banner' : 'pause.ready') + '</b> ' + t(ov.paused_all ? 'pause.bannerSub' : 'pause.readySub') +
      '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="toggleAllAi" data-on="' + (ov.paused_all ? '0' : '1') + '">' +
      icon(ov.paused_all ? 'play' : 'pause') + t(ov.paused_all ? 'pause.resume' : 'pause.stop') + '</button></div></div>';
  }
  P.dashboard = {
    fill: true,
    async render() {
      const ov = S.overview = await api('GET', '/api/workbench');
      const ags = ov.agents;
      const exp = S.wbExpand && ags.find((a) => a.key === S.wbExpand);
      return (ov.budget.paused ? '<div class="alert alert-danger">' + icon('pause') + '<span><b>' + t('budget.paused_title') + '</b> ' + t('budget.paused_body', { period: '@period.' + ov.budget.period }) + '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="go" data-route="settings">' + t('budget.adjust') + '</button></div></div>' : '') +
        (S.meta.setup_needed ? '<div class="alert alert-warning">' + icon('key') + '<span><b>' + t('tasks.setup_title') + '</b> ' + t('tasks.setup_body') + '</span><div class="alert-actions"><button class="btn btn-primary btn-sm" data-action="go" data-route="settings/models">' + t('qs3.goSetAi') + '</button></div></div>' : '') +
        '<div id="wb-pause">' + pauseControls(ov) + '</div>' +
        (!S.meta.setup_needed && ov.unconfigured ? '<div class="alert alert-warning">' + icon('key') + '<span><b>' + t('wb3.unconfigured', { n: ov.unconfigured }) + '</b> ' + t('wb3.unconfiguredSub') + '</span><div class="alert-actions"><button class="btn btn-primary btn-sm" data-action="go" data-route="settings/models">' + t('qs3.goSetAi') + '</button></div></div>' : '') +
        (ov.search_guide ? '<div class="alert alert-info">' + icon('search') + '<span><b>' + t('guide.search_title') + '</b> ' + t('guide.search_body') + '</span></div>' : '') +
        goalRow(ov) +
        '<div class="wb-agents" style="' + agentGrid() + '">' + AG().map((k) => miniCard(ags.find((a) => a.key === k) || { key: k })).join('') + '</div>' +
        (exp ? expandPanel(exp) : '') +
        '<div class="card wb-chat"><div class="card-header" data-agent="manager">' + avatar('manager', '', { presence: true }) + '<div class="grow"><h3>' + t('agent.manager') + ' <span class="agent-nick">Atlas</span></h3><div class="xs muted">' + esc(t('role.manager')) + '</div></div>' + U.statusPill(ags.find((a) => a.key === 'manager') || { key: 'manager', enabled: true }) + '</div>' +
        window.MGR.chatBlock() + '</div>';
    },
    async mount(root) {
      window.MGR.bind(root);
      window.MGR.ensure();
      U.renderNav();
      // 展开的 AI：实时动态（轮询事件）
      if (S.wbExpand) {
        let last = 0;
        const box = U.$('#wb-feed', root);
        const pull = async () => {
          try {
            const evs = await api('GET', '/api/workbench/feed?agent=' + S.wbExpand + '&after=' + last + '&limit=30');
            if (!box.isConnected) return;
            if (!last) box.innerHTML = evs.length ? evs.map(feedItem).join('') : '<div class="small muted" style="padding:12px 20px">' + t('run.feedEmpty') + '</div>';
            else if (evs.length) { box.insertAdjacentHTML('afterbegin', evs.map(feedItem).join('')); const live = document.getElementById('live-' + S.wbExpand); if (live && window.typeInto) window.typeInto(live, U.evText(evs[0]).replace(/<[^>]+>/g, '')); }
            if (evs.length) last = Math.max(last, evs[0].ts);
          } catch (e) { /* ignore */ }
        };
        pull(); U.every(3000, pull);
      }
      // 目标 / AI 状态：每 10 秒刷新卡片（不重画对话区）
      U.every(10000, async () => {
        try {
          const ov = S.overview = await api('GET', '/api/workbench');
          const wrap = root.querySelector('.wb-agents'); if (!wrap) return;
          wrap.innerHTML = AG().map((k) => miniCard(ov.agents.find((a) => a.key === k) || { key: k })).join('');
          const top = root.querySelector('.wb-top'); if (top) top.outerHTML = goalRow(ov);
          const pause = root.querySelector('#wb-pause'); if (pause) pause.innerHTML = pauseControls(ov);
        } catch (e) { /* ignore */ }
      });
    },
  };
  /* 打字机效果：体现“AI 正在做什么” */
  function typeInto(el, text) {
    el.classList.add('typing-caret'); el.textContent = '';
    let k = 0; const step = Math.max(1, Math.round(text.length / 40));
    const id = setInterval(() => {
      if (!el.isConnected) return clearInterval(id);
      k += step; el.textContent = text.slice(0, k);
      if (k >= text.length) { clearInterval(id); setTimeout(() => el.classList.remove('typing-caret'), 600); }
    }, 25);
  }
  window.typeInto = typeInto;
  A.wbExpand = (el) => { S.wbExpand = S.wbExpand === el.dataset.key ? null : el.dataset.key; U.refresh(); };
  A.toggleAllAi = async (el) => {
    const on = el.dataset.on === '1';
    if (on && !(await U.confirmDialog({ title: t('pause.stopTitle'), body: t('pause.stopBody'), ok: t('pause.stop'), danger: true }))) return;
    await busy(el, () => api('POST', '/api/ai/pause', { on }));
    toast(t(on ? 'pause.requested' : 'pause.resumed')); U.refresh();
  };
  A.toggleAgent = async (el) => {
    const key = el.dataset.key, input = el.querySelector('input'), name = t('agent.' + key);
    if (!input.checked) {
      input.checked = true;
      const ok = await U.confirmDialog({ title: t('toggle.offTitle', { name }), body: t('offImpact.' + key), ok: t('toggle.offOk'), extra: '<p class="xs muted" style="margin-top:10px">' + t('toggle.offNote') + '</p>',
        onOk: () => api('PUT', '/api/agents/' + key + '/enabled', { on: false }) });
      if (ok) { toast(t('toggle.offToast', { name })); U.refreshBadges(); U.refresh(); }
    } else {
      try { await api('PUT', '/api/agents/' + key + '/enabled', { on: true }); toast(t('sb.on_done', { a: name })); U.refreshBadges(); U.refresh(); } catch (e) { input.checked = false; toastErr(e); }
    }
  };

  /* =================================================================
     任务管理（导航 #/run，PRD §1 / 5.1.1）：新建任务（立即 / 定时 / 每天）+ 定时和每天的安排 + 全部任务；
     点某个任务看它的实时进度（#/run?task=…）。这是任务页唯一的入口
     ================================================================= */
  function statusBadge(x) {
    const cls = { running: 'tag-info', done: x.result === 'target_reached' ? 'tag-success' : 'tag-warning', cancelled: '', stopped: 'tag-danger', failed: 'tag-danger', interrupted: 'tag-warning', restarted: 'tag-warning' }[x.status] || '';
    const label = x.live ? t('status.running') : (x.status === 'done' ? t('result.' + (x.result || 'done')) : t('status.' + x.status));
    return '<span class="tag ' + cls + '">' + esc(label) + '</span>';
  }
  /* 数据源（PRD 6.4「所有渠道规矩一样：开始任务才工作」）：TG 群和 Google / 网页 / YouTube 一样是任务的一个来源；
     下面一行是现在开了监控的群数（0 个时用警告色，仍可开始）。FB 群第 4 步上线前不能选 */
  function srcRow(s, on, tgN) {
    // U15：没有 YouTube Key 时 YouTube 不能勾（旁边写去哪里填）；U16：Google 用免费搜索时提示一行
    const ytOff = s === 'youtube' && S.srcKeys && !S.srcKeys.youtube;
    const desc = s === 'tg'
      ? '<div class="xs" id="t-tg-line" style="' + (tgN ? 'color:var(--color-text-3)' : 'color:var(--color-warning)') + '">' + t('tasks3.tgLine', { n: tgN }) + ' <a href="#/channels/joined" data-action="go" data-route="channels/joined">' + t('tasks3.tgGo') + '</a></div>'
      : ytOff ? '<div class="xs" style="color:var(--color-warning)">' + t('tasks4.ytNoKey') + '</div>'
        : '<div class="xs muted">' + t('source_desc.' + s) + (s === 'google' && S.srcKeys && S.srcKeys.free ? '<div class="xs" style="color:var(--color-warning)">' + t('tasks4.freeSearch') + '</div>' : '') + '</div>';
    return '<label class="checkbox"' + (ytOff ? ' style="opacity:.7;cursor:not-allowed"' : '') + '><input type="checkbox" data-src="' + s + '" ' + (on && !ytOff ? 'checked' : '') + (ytOff ? ' disabled' : '') + '><span><b class="small">' + t('source.' + s) + '</b>' + desc + '</span></label>';
  }
  function newTaskCard(icps, tgN, preTg) {
    let last = ['google', 'web', 'youtube']; try { last = JSON.parse(localStorage.getItem('st.sources') || '["google","web","youtube"]'); } catch (e) { /* ignore */ }
    if (preTg && !last.includes('tg')) last = last.concat('tg');
    let urls = '', target = '20'; try { urls = localStorage.getItem('st.urls') || ''; target = localStorage.getItem('st.target') || '20'; } catch (e) { /* ignore */ }
    return '<div class="card"><div class="card-header"><h3>' + icon('play') + t('tasks.new') + '</h3><span class="sub">' + t('tasks.sub') + '</span></div><div class="card-body">' +
      (icps.length ? '<div class="field"><label class="label">' + t('tasks.template') + ' <span class="req">*</span></label><select class="select" id="t-icp">' + icps.map((i) => '<option value="' + i.id + '">' + esc(i.name) + '</option>').join('') + '</select></div>'
        : '<div class="alert alert-info">' + icon('target') + '<span>' + t('tasks.no_template') + '</span></div>') +
      '<div class="field"><label class="label">' + t('tasks.sources') + ' <span class="req">*</span></label><div class="col">' +
      S.meta.sources.map((s) => srcRow(s, last.includes(s), tgN)).join('') +
      '<label class="checkbox" style="opacity:.6;cursor:not-allowed"><input type="checkbox" disabled><span><b class="small">' + t('source.fb') + '</b> <span class="tag">' + t('tasks3.soon') + '</span><div class="xs muted">' + t('tasks3.fbSoon') + '</div></span></label>' + '</div></div>' +
      '<div class="field" id="urls-wrap"><label class="label">' + t('tasks.urls') + '</label><textarea class="textarea mono" id="t-urls" placeholder="https://www.example-directory.com/members">' + esc(urls) + '</textarea><span class="hint">' + t('tasks.urls_hint') + '</span></div>' +
      '<div class="field"><label class="label">' + t('tasks.target') + ' <span class="req">*</span></label><input class="input num" type="number" id="t-target" min="1" max="500" value="' + esc(target) + '" style="width:140px"><span class="hint" id="t-target-hint">' + t('tasks.target_hint') + '</span></div>' +
      whenField() + '</div>' +
      '<div class="card-footer" style="justify-content:flex-end"><button class="btn btn-primary" id="t-start" ' + (icps.length ? '' : 'disabled') + '>' + icon('play') + t('tasks.start') + '</button></div></div>';
  }
  /* 什么时候运行（PRD 5.1.1）：立即开始 / 定时（日期 + 时间，一次）/ 每天（时间）。本机时间 */
  const pad2 = (n) => String(n).padStart(2, '0');
  function localInput(ts) { const d = new Date(ts * 1000); return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  function tomorrow9() { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return d.getTime() / 1000; }
  function whenField(mode, at, tod) {
    mode = mode || 'now';
    return '<div class="field"><label class="label">' + t('sch.when') + '</label><div class="seg" id="t-when">' + ['now', 'once', 'daily'].map((m) => '<button class="' + (m === mode ? 'active' : '') + '" data-m="' + m + '">' + t('sch.m.' + m) + '</button>').join('') + '</div>' +
      '<div class="row" style="gap:8px;margin-top:8px"><input class="input" type="datetime-local" id="t-at" value="' + esc(localInput(at || tomorrow9())) + '" style="width:220px' + (mode === 'once' ? '' : ';display:none') + '">' +
      '<input class="input" type="time" id="t-tod" value="' + esc(tod || '09:00') + '" style="width:140px' + (mode === 'daily' ? '' : ';display:none') + '"></div>' +
      '<span class="hint" id="t-when-hint">' + t('sch.hint.' + mode) + '</span></div>';
  }
  function bindWhen(root, onChange) {
    let mode = (U.$('#t-when .active', root) || {}).dataset ? U.$('#t-when .active', root).dataset.m : 'now';
    U.$$('#t-when button', root).forEach((b) => { b.onclick = (e) => {
      e.preventDefault(); mode = b.dataset.m;
      U.$$('#t-when button', root).forEach((x) => x.classList.toggle('active', x === b));
      U.$('#t-at', root).style.display = mode === 'once' ? '' : 'none';
      U.$('#t-tod', root).style.display = mode === 'daily' ? '' : 'none';
      U.$('#t-when-hint', root).textContent = t('sch.hint.' + mode);
      if (onChange) onChange(mode);
    }; });
    return () => ({ mode, at: U.$('#t-at', root).value.replace('T', ' '), time: U.$('#t-tod', root).value });
  }
  /* ---- 全部任务（进行中 / 已完成 / 已停止 / 失败）+ 定时和每天的安排 */
  function st3(x) {
    if (x.live) return ['running', 'tag-info'];
    if (x.status === 'done') return ['done', x.result === 'target_reached' ? 'tag-success' : ''];
    if (x.status === 'failed') return ['failed', 'tag-danger'];
    return ['stopped', 'tag-warning'];
  }
  function taskRow(x) {
    const [st, cls] = st3(x), q = (x.stats && x.stats.qualified) || 0;
    const name = x.kind === 'research' ? (x.icp_name || '-') : t('kind.' + x.kind);
    const prog = x.kind === 'research' ? '<div class="row" style="gap:8px;min-width:120px"><div class="progress sm grow"><span style="width:' + Math.min(100, q / Math.max(1, x.target) * 100) + '%"></span></div><b class="xs num">' + q + ' / ' + x.target + '</b></div>' : '<span class="xs muted">' + (x.stats && x.stats.messages ? t('wb2.msgs', { n: x.stats.messages, c: x.stats.chats || 0 }) : '—') + '</span>';
    return '<tr class="clickable" data-action="go" data-route="run" data-q="task=' + esc(x.id) + '"><td class="nowrap small">' + U.fmtTime(x.created_at) + '</td>' +
      '<td><div class="cell-main">' + esc(name) + (x.schedule_id ? ' <span class="tag">' + icon('calendar') + t('sch.fromSchedule') + '</span>' : '') + '</div><div class="cell-sub">' + ((x.params && x.params.sources) || []).map((s) => t('source.' + s)).join(' · ') + '</div></td>' +
      '<td>' + prog + (x.live || !x.result ? '' : '<div class="xs muted" style="margin-top:2px">' + esc(t('result.' + x.result)) + '</div>') + '</td>' +
      '<td><span class="tag ' + cls + '">' + t('run3.st.' + st) + '</span></td>' +
      '<td class="nowrap">' + (x.live ? '<button class="btn btn-danger-ghost btn-sm" data-action="taskStop" data-id="' + esc(x.id) + '">' + icon('stop') + t('run.stop') + '</button>' : '') + '</td></tr>';
  }
  function tasksCard(list) {
    const f = S.runFilter || 'research';
    const rows = list.filter((x) => f === 'all' || x.kind === 'research');
    return '<div class="card" id="run-tasks"><div class="card-header"><h3>' + icon('list') + t('run3.all') + '</h3><div class="actions"><div class="seg">' +
      '<button class="' + (f === 'research' ? 'active' : '') + '" data-action="runFilter" data-v="research">' + t('run3.fResearch') + '</button><button class="' + (f === 'all' ? 'active' : '') + '" data-action="runFilter" data-v="all">' + t('run3.fAll') + '</button></div></div></div>' +
      (rows.length ? '<div class="table-wrap"><table class="table"><thead><tr><th>' + t('tasks.col_time') + '</th><th>' + t('run3.colTask') + '</th><th>' + t('run3.colProgress') + '</th><th>' + t('tasks.col_status') + '</th><th></th></tr></thead><tbody>' + rows.map(taskRow).join('') + '</tbody></table></div>'
        : '<div class="card-body small muted">' + t('tasks.no_history') + '</div>') + '</div>';
  }
  const schWhen = (x) => x.mode === 'daily' ? t('sch.daily', { time: x.time_of_day }) : t('sch.once', { time: U.fmtTime(x.run_at) });
  function schedulesCard(sch) {
    if (!sch.length) return '';
    return '<div class="card"><div class="card-header"><h3>' + icon('calendar') + t('sch.title') + '</h3><span class="sub">' + t('sch.sub') + '</span></div>' +
      sch.map((x) => {
        const stTxt = x.status === 'paused' ? '<span class="tag tag-warning">' + t('sch.st.paused') + '</span>' : x.status === 'active' ? (x.next_run_at ? '<span class="xs">' + t('sch.next', { time: U.fmtTime(x.next_run_at) }) + '</span>' : '') : '<span class="tag">' + t('sch.st.' + x.status) + '</span>';
        return '<div class="plan-item" style="align-items:center"><span class="text-2">' + icon('calendar') + '</span><div class="body"><div class="title">' + esc(x.icp_name || '-') + ' · ' + esc(schWhen(x)) + ' ' + stTxt + '</div>' +
          '<div class="desc xs">' + (x.params.sources || []).map((s) => t('source.' + s)).join(' · ') + ' · ' + t(x.mode === 'daily' ? 'sch.targetDaily' : 'sch.target', { n: x.params.target }) + ' · ' + t('sch.runs', { n: x.runs }) +
          (x.last_error ? ' · <span style="color:var(--color-warning)">' + esc(t('sch.lastError', { error: t('errors.' + x.last_error) })) + '</span>' : '') + '</div></div>' +
          (['active', 'paused'].includes(x.status) ? '<button class="btn btn-ghost btn-sm" data-action="schPause" data-id="' + x.id + '" data-p="' + (x.status === 'paused' ? 0 : 1) + '">' + icon(x.status === 'paused' ? 'play' : 'pause') + t(x.status === 'paused' ? 'sch.resume' : 'sch.pause') + '</button>' : '') +
          '<button class="btn btn-ghost btn-sm" data-action="schEdit" data-id="' + x.id + '">' + icon('edit') + t('sch.edit') + '</button>' +
          '<button class="btn btn-ghost btn-icon btn-sm" data-action="schDel" data-id="' + x.id + '" title="' + esc(t('c.delete')) + '">' + icon('trash') + '</button></div>';
      }).join('') + '</div>';
  }
  /* 只重画任务列表（不动左边正在填的新建任务表单） */
  async function refreshTasks() {
    const box = U.$('#run-tasks');
    if (!box) return;
    const list = await api('GET', '/api/tasks?limit=100' + (S.runFilter === 'all' ? '' : '&kind=research'));
    if (box.isConnected) box.outerHTML = tasksCard(list);
  }
  A.runFilter = (el) => { S.runFilter = el.dataset.v; refreshTasks(); };
  A.taskStop = async (el) => {
    if (await U.confirmDialog({ title: t('run.stopTitle'), body: t('board.cancel_confirm'), ok: t('run.stop'), danger: true, onOk: () => api('POST', '/api/tasks/' + el.dataset.id + '/cancel') })) { toast(t('run.stopped'), 'warn'); setTimeout(refreshTasks, 800); }
  };
  A.schPause = async (el) => { await busy(el, () => api('PUT', '/api/schedules/' + el.dataset.id, { paused: el.dataset.p === '1' })); toast(t(el.dataset.p === '1' ? 'sch.paused' : 'sch.resumed')); U.refresh(); };
  A.schDel = async (el) => { if (await U.confirmDialog({ title: t('sch.delTitle'), body: t('sch.delBody'), ok: t('c.delete'), danger: true, onOk: () => api('DELETE', '/api/schedules/' + el.dataset.id) })) { toast(t('common.deleted')); U.refresh(); } };
  /* 修改：模板、数据源、目标、网址、时间（定时 / 每天不能互换：删掉重建） */
  A.schEdit = async (el) => {
    const x = (S.schedules || []).find((s) => s.id === +el.dataset.id);
    const icps = await api('GET', '/api/icps');
    const p = x.params;
    const w = U.openModal({ title: t('sch.editTitle'), body:
      '<div class="field"><label class="label">' + t('tasks.template') + '</label><select class="select" id="se-icp">' + icps.map((i) => '<option value="' + i.id + '" ' + (i.id === p.icp_id ? 'selected' : '') + '>' + esc(i.name) + '</option>').join('') + '</select></div>' +
      '<div class="field"><label class="label">' + t('tasks.sources') + '</label><div class="col">' + S.meta.sources.map((s) => srcRow(s, (p.sources || []).includes(s), S.tgMonN || 0)).join('') + '</div></div>' +
      '<div class="field"><label class="label">' + t('tasks.target') + '</label><input class="input num" type="number" id="se-target" min="1" max="500" value="' + p.target + '" style="width:140px"><span class="hint">' + t(x.mode === 'daily' ? 'sch.targetDailyHint' : 'tasks.target_hint') + '</span></div>' +
      '<div class="field"><label class="label">' + t('tasks.urls') + '</label><textarea class="textarea mono" id="se-urls">' + esc((p.urls || []).join('\n')) + '</textarea></div>' +
      '<div class="field"><label class="label">' + t('sch.m.' + x.mode) + '</label>' + (x.mode === 'daily' ? '<input class="input" type="time" id="se-tod" value="' + esc(x.time_of_day) + '" style="width:140px">' : '<input class="input" type="datetime-local" id="se-at" value="' + esc(localInput(x.run_at)) + '" style="width:220px">') + '</div>',
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="se-ok">' + t('c.save') + '</button>' });
    U.$('#se-ok', w).onclick = async (e) => {
      const data = { params: { icp_id: +U.$('#se-icp', w).value, sources: U.$$('[data-src]', w).filter((c) => c.checked).map((c) => c.dataset.src), target: +U.$('#se-target', w).value, urls: U.$('#se-urls', w).value.split(/\s+/).map((v) => v.trim()).filter(Boolean) } };
      if (x.mode === 'daily') data.time = U.$('#se-tod', w).value; else data.at = U.$('#se-at', w).value.replace('T', ' ');
      try { await busy(e.currentTarget, () => api('PUT', '/api/schedules/' + x.id, data)); U.closeModal(); toast(t('c.saved')); U.refresh(); } catch (err) { toastErr(err); }
    };
  };
  P.run = {
    title: () => t('nav.run'),
    async render(params) {
      if (params.task) {                    // 某个任务的实时进度
        S.runTaskId = params.task;
        return '<div class="page-head"><div><button class="btn btn-ghost btn-sm" data-action="go" data-route="run" style="margin:0 0 6px -8px">' + icon('chevronLeft') + t('nav.run') + '</button><h1>' + t('run3.live') + '</h1></div></div><div id="run-board">' + U.skeleton(5) + '</div>';
      }
      S.runTaskId = null;
      const [icps, list, mon, sch, src] = await Promise.all([api('GET', '/api/icps'), api('GET', '/api/tasks?limit=100' + (S.runFilter === 'all' ? '' : '&kind=research')), api('GET', '/api/tg/chats?monitored=1').catch(() => []), api('GET', '/api/schedules').catch(() => []), api('GET', '/api/settings/sources').catch(() => null)]);
      S.srcKeys = src ? { youtube: src.keys.youtube_api_key.set, free: src.active_search === 'free' } : null;
      S.tgMonN = mon.filter((c) => !c.left_at).length;
      S.schedules = sch;
      S.runLive = list.some((x) => x.live);
      return '<div class="page-head"><div><h1>' + t('nav.run') + '</h1><p>' + t('run3.sub') + '</p></div></div>' +
        '<div class="grid grid-2" style="align-items:start">' + newTaskCard(icps, S.tgMonN, params.tg) + '<div class="grid" style="align-self:start;min-width:0">' + schedulesCard(sch) + tasksCard(list) + '</div></div>';
    },
    async mount(root, params) {
      const startBtn = U.$('#t-start', root);
      if (startBtn) {
        const sync = () => { const w = U.$('#urls-wrap', root); const web = U.$('[data-src=web]', root); if (w && web) w.classList.toggle('hidden', !web.checked); };
        U.$$('[data-src]', root).forEach((c) => c.addEventListener('change', sync)); sync();
        const when = bindWhen(root, (mode) => {
          startBtn.innerHTML = icon(mode === 'now' ? 'play' : 'calendar') + t(mode === 'now' ? 'tasks.start' : 'sch.save');
          U.$('#t-target-hint', root).textContent = t(mode === 'daily' ? 'sch.targetDailyHint' : 'tasks.target_hint');
        });
        U.every(5000, () => { if (S.runLive || U.$('#run-tasks .tag-info')) refreshTasks().catch(() => {}); });   // 进行中的任务：进度每 5 秒刷新
        startBtn.onclick = async (e) => {
          const sources = U.$$('[data-src]', root).filter((c) => c.checked).map((c) => c.dataset.src);
          const urls = U.$('#t-urls', root).value.split(/\s+/).map((x) => x.trim()).filter(Boolean);
          const target = +U.$('#t-target', root).value;
          if (sources.includes('tg') && !S.tgMonN && !(await U.confirmDialog({ title: t('tasks3.noGroupsTitle'), body: t('tasks3.noGroupsBody'), ok: t('tasks3.startAnyway') }))) return;
          try { localStorage.setItem('st.sources', JSON.stringify(sources)); localStorage.setItem('st.urls', U.$('#t-urls', root).value); localStorage.setItem('st.target', target); } catch (err) { /* ignore */ }
          try {
            const w = when();
            const r = await busy(e.currentTarget, () => api('POST', '/api/tasks', { icp_id: +U.$('#t-icp', root).value, sources, urls, target, when: w }));
            if (r.schedule) { toast(t('sch.saved', { time: r.schedule.next_run_at ? U.fmtTime(r.schedule.next_run_at) : '—' })); U.refresh(); }
            else U.go('run', { task: r.id });
          } catch (err) {
            if (err.key === 'profile_incomplete') {
              if (await U.confirmDialog({ title: t('qs.incomplete_title'), body: U.errText(err), html: false, ok: t('qs.go_complete') })) U.go('onboarding', { step: '4' });
            } else toastErr(err);
          }
        };
      }
      if (S.runTaskId) await liveBoard(U.$('#run-board', root), S.runTaskId);
    },
  };
  /* 没找够 / 没搜到时：按来源用大白话说原因和去哪里改（PRD §4 / U17：不叫客户自己补关键词） */
  function srcNotes(task) {
    const notes = (task.stats && task.stats.source_notes) || {};
    const lines = Object.entries(notes).map(([src, why]) => {
      const k = 'srcnote.' + src + '.' + why;
      return '<li>' + esc(U.has(k) ? t(k) : t('srcnote.generic', { source: t('source.' + src), reason: U.has('errors.' + why) ? t('errors.' + why) : why })) + '</li>';
    });
    return (task.result === 'sources_exhausted' ? ' ' + t('board.shortfall2') : '') + (lines.length ? '<ul style="margin:6px 0 0;padding-left:18px">' + lines.join('') + '</ul>' : '');
  }
  async function liveBoard(el, taskId) {
    let task = await api('GET', '/api/tasks/' + taskId);
    const agents = Object.assign({}, task.agents || {});
    let stats = task.stats || {}, live = task.live, filter = 'all';
    const feed = [];
    const t0 = task.created_at;
    const draw = () => {
      const q = stats.qualified || 0;
      el.innerHTML = '<div class="card" style="margin-bottom:16px"><div class="run-head">' + avatar('manager', 'lg', { solid: true }) +
        '<div class="grow"><div class="xs muted">' + t('run.task') + ' · <span class="mono">' + esc(taskId) + '</span></div><div style="font-size:var(--fs-lg);font-weight:600">' + (task.kind === 'research' ? t('wb2.goalLine', { n: task.target }) : t('kind.' + task.kind)) + '</div>' +
        '<div class="row wrap" style="margin-top:6px">' + (task.icp_name ? '<span class="tag tag-primary">' + icon('target') + esc(task.icp_name) + '</span>' : '') + (task.params.sources || []).map(srcTag).join('') + statusBadge(Object.assign({}, task, { live })) + '</div></div>' +
        '<div class="meter"><span>' + t('run.elapsed') + '</span><b id="run-elapsed">—</b></div><div class="meter"><span>' + t('run.cost') + '</span><b id="run-cost">' + U.moneyApprox(task.cost, task.cost_src) + '</b></div>' +
        (task.kind === 'research' ? '<div class="meter" style="min-width:150px"><span>' + t('dash.qualified') + '</span><b>' + q + ' / ' + task.target + '</b><div class="progress sm" style="margin-top:4px"><span style="width:' + Math.min(100, q / Math.max(1, task.target) * 100) + '%"></span></div></div>' : '') +
        (live ? '<button class="btn btn-danger-ghost" id="run-stop">' + icon('stop') + t('run.stop') + '</button>' : '<button class="btn btn-secondary" data-action="go" data-route="leads" data-q="task_id=' + esc(taskId) + '">' + t('board.view_leads') + '</button>') + '</div>' +
        (!live && task.result ? '<div class="alert ' + (task.result === 'target_reached' ? 'alert-success' : 'alert-warning') + '" style="margin:0 20px 16px">' + icon(task.result === 'target_reached' ? 'checkCircle' : 'info') + '<span><b>' + t('result.' + (task.result || 'done')) + '</b> ' + t('board.final', { n: q, target: task.target, backup: stats.backup || 0, cost: money(task.cost) }) + srcNotes(task) + (task.error ? ' ' + t('errors.' + task.error) : '') + '</span></div>' : '') + '</div>' +
        '<div class="agent-strip" style="margin-bottom:16px">' + AG().map((k) => {
          const st = agents[k]; const a = U.agentState(k);
          const state = !a.enabled || a.status === 'off' ? 'off' : st ? (st.state === 'done' ? 'idle' : st.state || 'working') : 'idle';
          return '<div class="card agent-lane" data-agent="' + k + '"><div class="row">' + avatar(k, 'sm', { dimOff: true }) + '<b class="small">' + esc(t('agent.' + k)) + '</b><span class="grow"></span>' + U.statusPill({ key: k, enabled: state !== 'off', status: state }) + '</div>' +
            '<div class="now" id="lane-' + k + '">' + (st ? U.evText(st) : esc(state === 'off' ? t('st.off') : t('board.waiting'))) + '</div>' +
            '<div class="xs muted" style="margin-top:6px">' + laneStat(k) + '</div></div>';
        }).join('') + '</div>' +
        '<div class="grid grid-main-side"><div class="card"><div class="card-header"><h3>' + icon('activity') + t('run.feed') + '</h3><span class="sub">' + t('run.feedSub') + '</span><div class="actions"><div class="seg">' +
        '<button class="' + (filter === 'all' ? 'active' : '') + '" data-f="all">' + t('run.allAgents') + '</button>' +
        ['manager', 'researcher', 'monitor', 'analyst'].map((k) => '<button class="' + (filter === k ? 'active' : '') + '" data-f="' + k + '" title="' + esc(t('agent.' + k)) + '"><span data-agent="' + k + '" style="color:var(--agent);display:inline-flex">' + icon('a_' + k) + '</span></button>').join('') + '</div></div></div>' +
        '<div class="feed" id="feed" style="max-height:560px;overflow:auto"></div></div>' +
        '<div class="grid" style="align-self:start"><div class="card"><div class="card-header"><h3>' + icon('filter') + t('run.funnel') + '</h3></div><div class="card-body"><div class="funnel">' +
        [['found', stats.pool || 0, 'researcher'], ['screened', stats.processed || 0, 'analyst'], ['graded', (stats.qualified || 0) + (stats.backup || 0), 'analyst'], ['qualified', q, 'manager']].map((r) => '<div class="funnel-row" data-agent="' + r[2] + '"><span class="text-2">' + t('run.f.' + r[0]) + '</span><div class="bar"><span style="width:' + (r[1] / Math.max(1, stats.pool || 1) * 100) + '%"></span></div><b class="num" style="text-align:right">' + r[1] + '</b></div>').join('') +
        '</div><div class="kv xs" style="margin-top:12px">' + [['filtered', (stats.filtered || 0) + (stats.discarded || 0)], ['duplicate', stats.duplicate], ['pages', stats.pages], ['queries', stats.queries]].map(([k, v]) => '<span class="k">' + t('kpi.' + k) + '</span><span class="num">' + U.fmtNum(v) + '</span>').join('') + '</div></div></div></div></div>';
      U.$$('[data-f]', el).forEach((b) => { b.onclick = () => { filter = b.dataset.f; draw(); }; });
      const stop = U.$('#run-stop', el);
      if (stop) stop.onclick = async () => { if (await U.confirmDialog({ title: t('run.stopTitle'), body: t('board.cancel_confirm'), ok: t('run.stop'), danger: true, onOk: () => api('POST', '/api/tasks/' + taskId + '/cancel') })) toast(t('run.stopped'), 'warn'); };
      drawFeed();
    };
    const laneStat = (k) => k === 'researcher' ? t('kpi.queries') + ' ' + (stats.queries || 0) + ' · ' + t('kpi.pages') + ' ' + (stats.pages || 0) + ' · ' + t('kpi.pool') + ' ' + (stats.pool || 0)
      : k === 'analyst' ? t('kpi.processed') + ' ' + (stats.processed || 0) + ' · ' + t('kpi.qualified') + ' ' + (stats.qualified || 0)
        : k === 'monitor' && stats.messages ? t('wb2.msgs', { n: stats.messages, c: stats.chats || 0 }) + (stats.lang_skipped ? ' · ' + t('wb2.langSkipped', { n: stats.lang_skipped }) : '') : '&nbsp;';
    const drawFeed = () => {
      const box = U.$('#feed', el); if (!box) return;
      const items = feed.filter((e) => filter === 'all' || e.agent === filter).slice(-200).reverse();
      box.innerHTML = items.length ? items.map(feedItem).join('') : empty({ icon: 'activity', title: t('run.feedEmpty') });
    };
    draw();
    const tick = () => { const s = Math.max(0, Math.floor((task.finished_at || Date.now() / 1000) - t0)); const e2 = U.$('#run-elapsed', el); if (e2) e2.textContent = [s / 3600, s / 60 % 60, s % 60].map((v) => String(Math.floor(v)).padStart(2, '0')).join(':'); };
    tick(); U.every(1000, () => { if (live) tick(); });
    const es = new EventSource('/api/tasks/' + encodeURIComponent(taskId) + '/events?after=0');
    U.onLeave(() => es.close());
    let pending = false;
    const schedule = () => { if (pending) return; pending = true; requestAnimationFrame(() => { pending = false; if (el.isConnected) { draw(); tick(); } }); };
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      feed.push(ev);
      if (ev.agent !== 'system') agents[ev.agent] = Object.assign({}, ev, { state: ev.key === 'ev.agent_paused_budget' ? 'paused' : (['ev.researcher_done', 'ev.monitor_round_summary'].includes(ev.key) ? 'done' : 'working') });
      else if (ev.key === 'ev.agent_stuck_restart') { const a = (ev.params.agent || '').replace('@agent.', ''); if (a) agents[a] = Object.assign({}, ev, { state: 'stuck' }); }
      schedule();
    };
    es.addEventListener('stats', (m) => { const d = JSON.parse(m.data); stats = d.stats; schedule(); });
    const finish = async () => { live = false; try { task = await api('GET', '/api/tasks/' + taskId); stats = task.stats; } catch (e) { /* ignore */ } Object.keys(agents).forEach((k) => { if (agents[k].state === 'working') agents[k].state = 'done'; }); if (el.isConnected) draw(); U.refreshBadges(); };
    es.addEventListener('end', () => { es.close(); finish(); });
    es.onerror = () => { if (es.readyState === 2) finish(); };
  }

  /* =================================================================
     ② 审核台：一键通过所有 A 级 · 卡片（等级/公司/国家·来源/原文/理由/联系）· 通过/修改后通过/这条不好/稍后
     ================================================================= */
  S.reviewDone = S.reviewDone || {};
  P.review = {
    async render(params) {
      const f = { country: params.country || '', source: params.source || '' };
      const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v)).toString();
      const d = S.reviewData = await api('GET', '/api/review' + (qs ? '?' + qs : ''));
      const done = Object.values(S.reviewDone).filter((x) => !d.items.some((i) => i.id === x.lead.id));
      const opt = (v, cur, label) => '<option value="' + esc(v) + '" ' + (v === cur ? 'selected' : '') + '>' + esc(label) + '</option>';
      return '<div class="page-head"><div><h1>' + t('rv.title') + '</h1><p>' + t('rv.pending') + ' ' + d.counts.pending + ' · ' + t('rv.later') + ' ' + d.counts.later + ' · ' + t('rv.doneToday') + ' ' + d.counts.done_today + ' · ' + t('rv2.sub') + '</p></div><div class="row">' +
        '<button class="btn btn-success" data-action="approveAllA" ' + (d.counts.grade_a ? '' : 'disabled') + '>' + icon('check') + t('rv.approveAllA', { n: d.counts.grade_a }) + '</button></div></div>' +
        ((d.facets.countries.length > 1 || d.facets.sources.length > 1 || f.country || f.source) ? '<div class="row wrap" style="margin-bottom:12px;gap:8px">' +
          '<select class="select input-sm" style="width:160px" data-change="rvFilter" data-k="country">' + opt('', f.country, t('leads.country') + ' ▾') + d.facets.countries.map((c) => opt(c, f.country, U.countryName(c))).join('') + '</select>' +
          '<select class="select input-sm" style="width:160px" data-change="rvFilter" data-k="source">' + opt('', f.source, t('leads.source') + ' ▾') + d.facets.sources.map((s) => opt(s, f.source, t('source.' + s))).join('') + '</select></div>' : '') +
        (d.items.length === 0 ? '<div class="card">' + empty({ icon: 'checkCircle', title: t('rv.allDone'), body: t('rv2.allDoneSub'), actions: '<button class="btn btn-secondary" data-action="go" data-route="leads">' + t('nav.leads') + '</button>' }) + '</div>' : '') +
        '<div class="grid">' + done.map(doneCard).join('') + d.items.map(reviewCard).join('') + '</div>';
    },
  };
  function doneCard(x) {
    const l = x.lead;
    return '<div class="card review-card done" style="grid-template-columns:1fr auto"><div class="rc-main row">' + grade(l.grade) + '<b>' + esc(l.company_name) + '</b><span class="tag ' + (x.act === 'approve' || x.act === 'edit_approve' ? 'tag-success' : x.act === 'reject' ? 'tag-danger' : '') + '">' + t('rv.done_' + (x.act === 'edit_approve' ? 'edit' : x.act)) + '</span></div>' +
      '<div class="rc-main"><button class="btn btn-ghost btn-sm" data-action="reviewUndo" data-id="' + l.id + '">' + icon('undo') + t('rv.undo') + '</button></div></div>';
  }
  function quoteOf(l) {
    const s = (l.sources || []).find((x) => x.quote) || {};
    return s.quote || (l.profile && (l.profile.buying_signals || [])[0]) || (l.profile && l.profile.summary) || '';
  }
  function reviewCard(l) {
    const p = l.profile || {};
    const q = quoteOf(l);
    return '<div class="card review-card"><div class="rc-main"><div class="row" style="gap:10px">' + grade(l.grade, true) + '<div class="grow"><h3>' + esc(l.company_name) + '</h3><div class="xs muted" style="margin-top:2px">' + esc(U.countryName(l.country) || '—') + ' · ' + esc(t('source.' + l.first_source)) +
      (l.posted_at ? ' · ' + U.ago(l.posted_at) : '') + (l.intent ? ' · ' + t('intent.' + l.intent) : '') + (l.website ? ' · <span class="mono">' + esc(l.website.replace(/^https?:\/\//, '')) + '</span>' : '') + '</div></div>' +
      '<button class="btn btn-ghost btn-sm" data-action="openLead" data-id="' + l.id + '">' + t('rv.fullProfile') + icon('chevronRight') + '</button></div>' +
      (q ? '<div class="quote" style="margin-top:12px">“' + U.txt(q.slice(0, 400)) + '”</div>' : '') +
      (p.grade_reason ? '<div class="small" style="margin-top:10px"><span class="muted">' + t('rv.why') + '：</span>' + esc(p.grade_reason) + '</div>' : '') +
      '<div class="small row wrap" style="margin-top:8px;gap:8px"><span class="muted">' + t('rv.contact') + '：</span>' + contactLine(l, true) + '</div></div>' +
      '<div class="rc-side"><button class="btn btn-success" data-action="reviewAct" data-id="' + l.id + '" data-act="approve">' + icon('check') + t('rv.approve') + '</button>' +
      '<button class="btn btn-secondary" data-action="reviewEdit" data-id="' + l.id + '">' + icon('edit') + t('rv.editApprove') + '</button>' +
      '<button class="btn btn-danger-ghost" data-action="reviewReject" data-id="' + l.id + '">' + icon('thumbsDown') + t('nf.btn') + '</button>' +
      '<button class="btn btn-ghost" data-action="reviewAct" data-id="' + l.id + '" data-act="later">' + icon('clock') + t('rv.later') + '</button></div></div>';
  }
  const findLead = (id) => (S.reviewData.items || []).find((x) => x.id === +id);
  A.rvFilter = (el) => { const p = Object.assign({}, S.params, { [el.dataset.k]: el.value }); Object.keys(p).forEach((k) => { if (!p[k]) delete p[k]; }); U.go('review', p); };
  A.reviewAct = async (el) => {
    const id = +el.dataset.id, act = el.dataset.act, l = findLead(id) || { id, company_name: '', grade: '' };
    await busy(el, () => api('POST', '/api/review/' + id, { action: act }));
    S.reviewDone[id] = { act, lead: l };
    U.closeDrawer(); toast(t('rv.done_' + act), act === 'later' ? 'warn' : 'success'); U.refreshBadges(); U.refresh();
  };
  A.reviewUndo = async (el) => { await busy(el, () => api('POST', '/api/review/' + el.dataset.id + '/undo')); delete S.reviewDone[el.dataset.id]; toast(t('nf.undone')); U.refreshBadges(); U.refresh(); };
  A.reviewReject = (el) => notFit(findLead(el.dataset.id), t('rv.rejectTitle'));
  A.reviewEdit = (el) => {
    const l = findLead(el.dataset.id), c = contactsOf(l), ct = (l.contacts || []).find((x) => x.name) || {};
    const w = U.openModal({ title: t('rv.editTitle'), sub: esc(l.company_name),
      body: '<div class="form-grid"><div class="field"><label class="label">' + t('rv.editGrade') + '</label><select class="select" id="ed-grade">' + ['A', 'B', 'C'].map((g) => '<option ' + (g === l.grade ? 'selected' : '') + '>' + g + '</option>').join('') + '</select></div>' +
        '<div class="field"><label class="label">' + t('leads.company') + '</label><input class="input" id="ed-name" value="' + esc(l.company_name) + '"></div>' +
        '<div class="field"><label class="label">' + t('leads.contact') + '</label><input class="input" id="ed-contact" value="' + esc(ct.name || '') + '"></div>' +
        '<div class="field"><label class="label">Telegram</label><input class="input mono" id="ed-tg" value="' + esc(c.tg) + '"></div>' +
        '<div class="field span-2"><label class="label">' + t('lead.c_email') + '</label><input class="input mono" id="ed-email" value="' + esc(c.email) + '"></div>' +
        '<div class="field"><label class="label">' + t('rv.assign') + '</label><input class="input" id="ed-assign" placeholder="' + esc(t('rv2.autoAssign')) + '"></div>' +
        '<div class="field"><label class="label">' + t('rv.planLabel') + '</label><select class="select" id="ed-plan"><option value="">' + t('rv.planAuto') + '</option><option value="tg">' + t('rv.planTg') + '</option><option value="email">' + t('rv.planEmail') + '</option><option value="both">' + t('rv.planBoth') + '</option></select></div>' +
        '<div class="field span-2"><label class="label">' + t('rv2.noteToSales') + '</label><textarea class="textarea" id="ed-note" placeholder="' + esc(t('rv2.noteToSalesPh')) + '"></textarea></div></div>',
      footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-success" id="ed-ok">' + icon('check') + t('rv.approve') + '</button>' });
    U.$('#ed-ok', w).onclick = async (e) => {
      const v = (s) => U.$(s, w).value.trim();
      const edits = { grade: v('#ed-grade'), company_name: v('#ed-name') };
      if (v('#ed-tg') !== c.tg || v('#ed-email') !== c.email || v('#ed-contact') !== (ct.name || '')) Object.assign(edits, { telegram: v('#ed-tg'), email: v('#ed-email'), contact_name: v('#ed-contact') });
      try { await busy(e.currentTarget, () => api('POST', '/api/review/' + l.id, { action: 'edit_approve', edits, assigned_to: v('#ed-assign'), plan: v('#ed-plan'), note: v('#ed-note') })); } catch (err) { toastErr(err); return; }
      U.closeModal(); S.reviewDone[l.id] = { act: 'edit_approve', lead: l }; toast(t('rv.done_edit')); U.refreshBadges(); U.refresh();
    };
  };
  A.approveAllA = async () => {
    const n = S.reviewData.counts.grade_a;
    const f = { country: S.params.country || '', source: S.params.source || '' };
    if (await U.confirmDialog({ title: t('rv.bulkTitle', { n }), body: t('rv2.bulkBody'), ok: t('rv.approve'), okCls: 'btn-success',
      onOk: async () => { const r = await api('POST', '/api/review/batch', { grade: 'A', filters: f }); toast(t('rv.bulkDone', { n: r.approved })); } })) { U.refreshBadges(); U.refresh(); }
  };

  /* =================================================================
     ③ 线索库：导出 Excel · 全部/A/B/备用池 + 国家/来源/状态 · 表格 级/公司/国家/TG·WA/状态 · 点行看详情
     ================================================================= */
  P.leads = {
    async render(params) {
      const f = { grade: params.grade || '', qualified: params.qualified || '', country: params.country || '', source: params.source || '', status: params.status || '', q: params.q || '', task_id: params.task_id || '', page: +(params.page || 1) };
      const qs = (extra) => new URLSearchParams(Object.entries(Object.assign({}, f, extra || {})).filter(([, v]) => v !== '' && v != null)).toString();
      const [data, fac, pool] = await Promise.all([api('GET', '/api/leads?' + qs({ size: 50 })), api('GET', '/api/leads/facets'), api('GET', '/api/pool/status')]);
      S.leadSel = new Set(); S.leadF = f;
      const c = fac.counts;
      const opt = (v, cur, label) => '<option value="' + esc(v) + '" ' + (v === cur ? 'selected' : '') + '>' + esc(label) + '</option>';
      const chip = (k, v, label, on) => '<span class="chip' + (on ? ' active' : '') + '" data-action="lf" data-k="' + k + '" data-v="' + v + '">' + label + '</span>';
      const curChip = f.qualified === '0' ? 'backup' : f.grade || 'all';
      return '<div class="page-head"><div><h1>' + t('leads.title') + '</h1><p>' + t('leads2.sub', { total: c.total, qualified: c.qualified }) + '</p></div><div class="row">' +
        '<a class="btn btn-secondary" href="/api/leads/export.xlsx?' + qs() + '">' + icon('download') + t('c.export') + '</a>' +
        '<a class="btn btn-ghost btn-sm" href="/api/leads/export.csv?' + qs() + '">' + t('leads.export_csv') + '</a>' +
        '<button class="btn btn-danger-ghost btn-sm hidden" id="l-del" data-action="leadsDelete">' + icon('trash') + '<span></span></button></div></div>' +
        poolCard(pool) +
        (f.task_id ? '<div class="alert alert-info">' + icon('search') + '<span>' + t('leads.task_filter', { id: f.task_id }) + '</span><div class="alert-actions"><button class="btn btn-secondary btn-sm" data-action="go" data-route="leads">' + t('leads.clear_filter') + '</button></div></div>' : '') +
        '<div class="row wrap" style="margin-bottom:12px;gap:8px">' + chip('grade', '', t('c.all'), curChip === 'all') + chip('grade', 'A', grade('A') + ' <span class="xs muted">' + c.a + '</span>', curChip === 'A') + chip('grade', 'B', grade('B') + ' <span class="xs muted">' + c.b + '</span>', curChip === 'B') + chip('qualified', '0', esc(t('leads.backup')), curChip === 'backup') +
        '<span class="grow"></span><div class="input-group" style="width:220px">' + icon('search') + '<input class="input input-sm" id="l-q" placeholder="' + esc(t('leads.searchPh')) + '" value="' + esc(f.q) + '"></div>' +
        '<select class="select input-sm" style="width:140px" data-change="lfSel" data-k="country">' + opt('', f.country, t('leads.country') + ' ▾') + fac.countries.map((x) => opt(x, f.country, U.countryName(x))).join('') + '</select>' +
        '<select class="select input-sm" style="width:140px" data-change="lfSel" data-k="source">' + opt('', f.source, t('leads.source') + ' ▾') + fac.sources.map((s) => opt(s, f.source, t('source.' + s))).join('') + '</select>' +
        '<select class="select input-sm" style="width:140px" data-change="lfSel" data-k="status">' + opt('', f.status, t('leads.status') + ' ▾') + fac.statuses.map((s) => opt(s, f.status, t('lstatus.' + s))).join('') + '</select></div>' +
        '<div class="card">' + (data.items.length ? '<div class="table-wrap"><table class="table"><thead><tr><th class="w-check"><input type="checkbox" data-change="leadSelAll"></th><th style="width:48px">' + t('leads.grade') + '</th><th>' + t('leads.company') + '</th><th>' + t('leads.country') + '</th><th>' + t('leads.contactCol') + '</th><th>' + t('leads.status') + '</th><th>' + t('leads.found') + '</th></tr></thead><tbody>' +
          data.items.map((l) => '<tr class="clickable" data-action="openLead" data-id="' + l.id + '"><td data-stop><input type="checkbox" data-change="leadSel" data-id="' + l.id + '"></td><td>' + grade(l.grade) + '</td><td><div class="cell-main">' + esc(l.company_name) + '</div><div class="cell-sub ellipsis" style="max-width:420px">' + esc(t('source.' + l.first_source)) + (l.website ? ' · ' + esc(l.website.replace(/^https?:\/\//, '')) : '') + (l.qualified ? '' : ' · ' + esc(t('nq.' + l.not_qualified_reason))) + '</div></td>' +
            '<td class="nowrap">' + U.countryCell(l.country) + '</td><td class="small">' + contactLine(l) + '</td><td class="nowrap">' + statusTag(l) + '</td><td class="xs muted nowrap">' + U.fmtTime(l.created_at) + '</td></tr>').join('') + '</tbody></table></div>' +
          '<div class="card-footer"><span class="small muted grow">' + t('leads.page_info', { from: (f.page - 1) * 50 + 1, to: (f.page - 1) * 50 + data.items.length, total: data.total }) + '</span>' +
          '<button class="btn btn-secondary btn-sm" data-action="lf" data-k="page" data-v="' + (f.page - 1) + '" ' + (f.page <= 1 ? 'disabled' : '') + '>' + icon('chevronLeft') + '</button><button class="btn btn-secondary btn-sm" data-action="lf" data-k="page" data-v="' + (f.page + 1) + '" ' + (f.page * 50 >= data.total ? 'disabled' : '') + '>' + icon('chevronRight') + '</button></div>'
          : (c.total ? empty({ icon: 'search', title: t('leads.empty'), body: t('leads.emptySub'), actions: '<button class="btn btn-secondary" data-action="go" data-route="leads">' + t('c.reset') + '</button>' })
            : empty({ icon: 'database', title: t('leads.emptyFirst'), body: t('leads2.emptyFirstSub'), actions: '<button class="btn btn-primary" data-action="newTask">' + icon('message') + t('wb2.tellManager') + '</button>' }))) + '</div>';
    },
    mount(root, params) {
      const q = U.$('#l-q', root);
      let tm; if (q) q.addEventListener('input', () => { clearTimeout(tm); tm = setTimeout(() => lfGo({ q: q.value.trim() }), 450); });
      U.$$('[data-stop]', root).forEach((td) => td.addEventListener('click', (e) => e.stopPropagation()));
      if (params.open || params.lead) openLead(+(params.open || params.lead));
    },
  };
  /* 线索池状况（PRD 5.2.2）：各分区数量 · 今日新增/合格/备用/丢弃 · 本周升级 · 各来源合格率 */
  function poolCard(p) {
    const z = p.zones, td = p.today;
    const cell = (label, v, cls) => '<div><div class="xs muted">' + label + '</div><b class="num' + (cls ? ' ' + cls : '') + '">' + U.fmtNum(v) + '</b></div>';
    return '<div class="card" style="margin-bottom:12px"><div class="card-body row wrap" style="gap:22px;align-items:flex-end">' +
      '<b class="small">' + icon('database') + ' ' + t('pool.title') + '</b>' +
      ['qualified', 'backup', 'discarded', 'expired', 'won', 'pending'].map((k) => cell(t('pool.zone.' + k), z[k] || 0)).join('') +
      '<span class="wb-sep"></span>' + cell(t('pool.today_new'), td.new) + cell(t('pool.today_q'), td.qualified) + cell(t('pool.today_b'), td.backup) + cell(t('pool.today_d'), td.discarded) + cell(t('pool.upgrades'), p.upgrades_week) +
      '<span class="grow"></span><button class="btn btn-ghost btn-sm" data-action="poolRun" title="' + esc(t('pool.recheck_note', { d: p.settings.recheck_days })) + '">' + icon('refresh') + t('pool.run') + '</button></div>' +
      (p.per_source.length ? '<div class="card-body tight xs muted" style="border-top:1px solid var(--color-border)">' + t('pool.rate') + '：' + p.per_source.map((x) => esc(t('source.' + x.source)) + ' ' + Math.round(x.rate * 100) + '% (' + x.qualified + '/' + x.n + ')').join(' · ') + '</div>' : '') + '</div>';
  }
  A.poolRun = async (el) => { try { const r = await busy(el, () => api('POST', '/api/pool/run')); toast(t('pool.running')); U.go('run', { task: r.id }); } catch (e) { toastErr(e); } };

  function lfGo(patch) {
    const f = Object.assign({}, S.leadF, { page: 1 }, patch);
    if ('grade' in patch) f.qualified = '';
    if ('qualified' in patch) f.grade = '';
    const p = {}; Object.entries(f).forEach(([k, v]) => { if (v !== '' && v != null && !(k === 'page' && +v === 1)) p[k] = v; });
    U.go('leads', Object.keys(p).length ? p : null);
  }
  A.lf = (el) => lfGo({ [el.dataset.k]: el.dataset.v });
  A.lfSel = (el) => lfGo({ [el.dataset.k]: el.value });
  A.leadSel = (el) => { el.checked ? S.leadSel.add(+el.dataset.id) : S.leadSel.delete(+el.dataset.id); syncDel(); };
  A.leadSelAll = (el) => { U.$$('[data-change=leadSel]').forEach((c) => { c.checked = el.checked; el.checked ? S.leadSel.add(+c.dataset.id) : S.leadSel.delete(+c.dataset.id); }); syncDel(); };
  function syncDel() { const b = U.$('#l-del'); if (!b) return; b.classList.toggle('hidden', !S.leadSel.size); b.querySelector('span').textContent = t('leads.delete_n', { n: S.leadSel.size }); }
  A.leadsDelete = async () => {
    const n = S.leadSel.size;
    if (await U.confirmDialog({ title: t('leads.delete_sel'), body: t('leads.delete_confirm', { n }), ok: t('c.delete'), danger: true, onOk: () => api('POST', '/api/leads/delete', { ids: [...S.leadSel] }) })) { toast(t('leads.deleted', { n })); U.refresh(); }
  };

  /* 线索详情抽屉：客户画像 · 判断依据 · 来源链接+原文 · 👍 / 这条不好 / 已成交 */
  async function openLead(id) {
    let d;
    try { d = await api('GET', '/api/leads/' + id); } catch (e) { toastErr(e); return; }
    const p = d.profile || {}, hm = p.hard_match || {};
    const list = (arr) => (arr && arr.length ? '<ul class="pb-list small">' + arr.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '');
    const pf = (k, v) => v && (!Array.isArray(v) || v.length) ? '<div style="margin-bottom:10px"><div class="xs muted">' + t(k) + '</div><div class="small">' + (Array.isArray(v) ? list(v) : esc(v)) + '</div></div>' : '';
    const yes = (v) => '<span class="tag ' + (v ? 'tag-success' : 'tag-danger') + '">' + icon(v ? 'check' : 'x') + '</span>';
    const ct = (d.contacts || []).find((x) => x.name) || {};
    const c = contactsOf(d);
    U.openDrawer('<div class="drawer-header" data-current-lead="' + d.id + '">' + grade(d.grade, true) + '<div class="grow"><h3>' + esc(d.company_name) + '</h3>' +
      '<div class="row wrap small text-2" style="margin-top:4px">' + U.countryCell(d.country) + (d.customer_type ? '<span>·</span><span>' + esc(U.ctypeName(d.customer_type)) + '</span>' : '') + '<span>·</span><span>' + t('leads.score') + ' ' + d.score + '</span>' +
      (d.posted_at ? '<span>·</span><span>' + U.ago(d.posted_at) + '</span>' : '') + (d.intent ? '<span class="tag">' + t('intent.' + d.intent) + '</span>' : '') + statusTag(d) + '</div></div>' +
      '<button class="btn btn-ghost btn-icon btn-sm" data-action="closeDrawer">' + icon('x') + '</button></div>' +
      '<div class="drawer-body">' +
      (d.qualified ? '<div class="alert alert-success">' + icon('checkCircle') + '<span><b>' + t('lead.qualified') + '</b> ' + t('lead.qualified_sub') + '</span></div>' : '<div class="alert alert-warning">' + icon('alert') + '<span><b>' + t('lead.backup') + '</b> ' + t('nq.' + d.not_qualified_reason) + '</span></div>') +
      '<div class="section-title" style="margin-top:16px">' + icon('user') + t('rv.contact') + '</div>' + (ct.name ? '<div class="small">' + esc(ct.name) + (ct.title ? ' · ' + esc(ct.title) : '') + '</div>' : '') + '<div style="margin-top:6px">' + contactLine(d) + '</div>' +
      (c.email ? '<div class="xs muted" style="margin-top:4px">' + t('leads.d.emailSecondary') + '：<span class="mono">' + esc(c.email) + '</span></div>' : '') +
      ((d.contacts || []).length > 1 ? '<details style="margin-top:6px"><summary class="xs muted" style="cursor:pointer">' + t('lead.contacts') + ' (' + d.contacts.length + ')</summary><div class="table-wrap" style="margin-top:6px"><table class="table"><tbody>' +
        d.contacts.map((x) => '<tr><td class="small">' + esc(x.name || '-') + (x.title ? '<div class="cell-sub">' + esc(x.title) + '</div>' : '') + '</td><td class="small mono">' + esc(x.email || x.telegram || x.whatsapp || '-') + '</td><td>' + (x.verified ? '<span class="tag tag-success">' + t('lead.verified') + '</span>' : '<span class="tag tag-warning">' + t('lead.unverified') + '</span>') + '</td></tr>').join('') + '</tbody></table></div></details>' : '') +
      '<div class="section-title" style="margin-top:20px">' + icon('building') + t('leads.d.profile') + '</div>' +
      pf('leads.d.intro', p.summary) + pf('leads.d.size', p.size) + pf('lead.markets', p.markets) + pf('leads.d.news', p.recent_news) + pf('leads.d.pain', p.pain_points) + pf('lead.signals', p.buying_signals) + pf('leads.d.fit', p.fit_points) + pf('leads.d.risk', p.risks) +
      (d.website ? '<div class="xs muted">' + t('lead.website') + '：<a href="' + esc(d.website) + '" target="_blank" rel="noopener noreferrer">' + esc(d.website) + '</a></div>' : '') +
      '<div class="section-title" style="margin-top:20px">' + icon('sparkles') + t('leads.d.basis') + '</div><div class="alert ' + (d.grade === 'A' ? 'alert-success' : 'alert-info') + '">' + icon('zap') + '<span>' + esc(p.grade_reason || '-') + '<br><span class="xs">' + t('lead.hard_country') + ' ' + yes(hm.country_ok) + ' ' + t('lead.hard_type') + ' ' + yes(hm.customer_type_ok) + ' ' + t('lead.hard_excluded') + ' ' + yes(!hm.excluded) + '</span></span></div>' +
      '<div class="section-title" style="margin-top:20px">' + icon('link') + t('leads.d.sources') + '</div>' +
      ((d.sources || []).length ? d.sources.map((s) => '<div class="quote" style="margin-bottom:10px"><div class="row xs muted" style="justify-content:space-between"><span class="row ellipsis" style="gap:6px">' + icon(/t\.me/.test(s.url || '') ? 'telegram' : /youtube/.test(s.url || '') ? 'youtube' : 'globe') + esc(s.title || s.url) + '</span>' +
        (s.url && /^https?:/.test(s.url) ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' + t('leads.d.openSource') + ' ' + icon('external') + '</a>' : '') + '</div>' + (s.quote ? '<div style="margin-top:4px">“' + U.txt(s.quote) + '”</div>' : '') + '<div class="xs muted mono ellipsis" style="margin-top:4px">' + esc(s.url || '') + '</div></div>').join('')
        : '<div class="alert alert-warning">' + icon('alert') + '<span>' + t('leads.d.noEvidence') + '</span></div>') +
      ((d.raw || []).length ? '<div class="xs muted">' + t('lead.found_times', { n: d.raw.length }) + '</div>' : '') +
      (d.feedback ? '<div class="alert ' + (d.feedback > 0 ? 'alert-success' : 'alert-danger') + '" style="margin-top:14px">' + icon(d.feedback > 0 ? 'thumbsUp' : 'thumbsDown') + '<span>' + (d.deal ? t('leads.d.wonToast') : d.feedback > 0 ? t('leads.d.thanksUp') : t('fbr.' + (d.feedback_reason || 'other')) + (d.feedback_note ? '：' + esc(d.feedback_note) : '')) + '</span></div>' : '') +
      '<div class="xs muted" style="margin-top:14px">' + t('lead.template') + '：' + esc(d.icp_name || '-') + ' · ' + t('lead.created') + ' ' + U.fmtTime(d.created_at) + ' · ID ' + d.id + '</div></div>' +
      '<div class="drawer-footer"><button class="btn ' + (d.feedback === 1 ? 'btn-success' : 'btn-secondary') + '" data-action="thumbUp" data-id="' + d.id + '" data-on="' + (d.feedback === 1 ? 1 : 0) + '" title="' + esc(t('leads.d.yes')) + '">' + icon('thumbsUp') + '</button>' +
      '<button class="btn btn-secondary" data-action="notFitLead" data-id="' + d.id + '" ' + (d.status === 'rejected' ? 'disabled' : '') + '>' + icon('thumbsDown') + t('nf.btn') + '</button>' +
      '<button class="btn ' + (d.deal ? 'btn-success' : 'btn-secondary') + '" data-action="markWon" data-id="' + d.id + '" data-on="' + (d.deal ? 1 : 0) + '">' + icon('flag') + t('leads.d.won') + '</button>' +
      '<button class="btn ' + (d.do_not_contact ? 'btn-danger' : 'btn-ghost') + '" data-action="leadDnc" data-id="' + d.id + '" data-on="' + (d.do_not_contact ? 1 : 0) + '" title="' + esc(t('lead.dnc_body')) + '">' + icon('hand') + t('lead.dnc') + '</button>' +
      '<button class="btn btn-ghost btn-icon" data-action="leadDelete" data-id="' + d.id + '" title="' + esc(t('c.delete')) + '">' + icon('trash') + '</button><span class="grow"></span>' +
      (d.status === 'pending_review' ? '<button class="btn btn-primary" data-action="drawerApprove" data-id="' + d.id + '">' + icon('check') + t('rv.approve') + '</button>' : '') + '</div>');
    S.drawerLead = d;
  }
  window.openLead = openLead;
  A.openLead = (el) => openLead(+el.dataset.id);
  A.thumbUp = async (el) => { const on = el.dataset.on === '1'; await api('POST', '/api/leads/' + el.dataset.id + '/feedback', { polarity: on ? 0 : 1 }); toast(on ? t('fb.cleared') : t('leads.d.thanksUp')); openLead(+el.dataset.id); };
  A.notFitLead = (el) => notFit(S.drawerLead);
  A.markWon = async (el) => { const on = el.dataset.on === '1'; await api('POST', '/api/leads/' + el.dataset.id + '/deal', { deal: !on }); toast(on ? t('lead.deal_cleared') : t('leads.d.wonToast')); openLead(+el.dataset.id); if (S.route === 'leads') U.refresh(); };
  A.leadDnc = async (el) => {
    const on = el.dataset.on === '1';
    if (!on && !await U.confirmDialog({ title: t('lead.dnc_title'), body: t('lead.dnc_body'), ok: t('lead.dnc'), danger: true })) return;
    await api('POST', '/api/leads/' + el.dataset.id + '/dnc', { on: !on });
    toast(on ? t('lead.dnc_off') : t('lead.dnc_on')); openLead(+el.dataset.id);
  };
  A.drawerApprove = async (el) => { await busy(el, () => api('POST', '/api/review/' + el.dataset.id, { action: 'approve' })); U.closeDrawer(); toast(t('rv.done_approve')); U.refreshBadges(); U.refresh(); };
  A.leadDelete = async (el) => { if (await U.confirmDialog({ title: t('c.delete'), body: t('leads.delete_confirm', { n: 1 }), ok: t('c.delete'), danger: true, onOk: () => api('POST', '/api/leads/delete', { ids: [+el.dataset.id] }) })) { U.closeDrawer(); toast(t('common.deleted')); U.refresh(); } };

  /* 手动添加样本（好例子 / 不好的例子）：模板管理和 AI 记忆共用，不用跳页面 */
  function sampleDialog(icpId, done) {
    let pol = 1;
    const w = U.openModal({ title: t('learn.add'), body: '<div class="seg" id="sp-pol"><button class="active" data-p="1">' + icon('thumbsUp') + t('learn.good') + '</button><button data-p="-1">' + icon('thumbsDown') + t('learn.bad') + '</button></div>' +
      '<div class="field" style="margin-top:12px"><label class="label">' + t('learn.text') + ' <span class="req">*</span></label><textarea class="textarea" id="sp-text" style="min-height:120px" placeholder="' + esc(t('learn.text_ph')) + '"></textarea></div>' +
      '<div class="field hidden" id="sp-r"><label class="label">' + t('fb.reason') + '</label><select class="select" id="sp-reason">' + S.meta.feedback_reasons.map((r) => '<option value="' + r + '">' + t('fbr.' + r) + '</option>').join('') + '</select></div>' +
      '<div class="field"><label class="label">' + t('fb.note') + '</label><input class="input" id="sp-note"></div>',
    footer: '<button class="btn btn-secondary" data-action="closeModal">' + t('c.cancel') + '</button><button class="btn btn-primary" id="sp-ok">' + t('c.save') + '</button>' });
    U.$$('#sp-pol button', w).forEach((b) => { b.onclick = () => { pol = +b.dataset.p; U.$$('#sp-pol button', w).forEach((x) => x.classList.toggle('active', x === b)); U.$('#sp-r', w).classList.toggle('hidden', pol !== -1); }; });
    U.$('#sp-ok', w).onclick = async (e) => { try { await busy(e.currentTarget, () => api('POST', '/api/icps/' + icpId + '/samples', { polarity: pol, text: U.$('#sp-text', w).value, reason: pol < 0 ? U.$('#sp-reason', w).value : '', note: U.$('#sp-note', w).value })); U.closeModal(); toast(t('learn.added')); if (done) done(); } catch (err) { toastErr(err); } };
  }
  window.sampleDialog = sampleDialog;
  /* =================================================================
     模板管理（导航，#/icp；唯一入口）：左 模板列表 · 中 AI 生成 + 字段 + 样本 · 右 聊天修改
     ================================================================= */
  /* 求购说法 / 打分标准：AI 根据你的资料生成（PRD 2.4.2，不写死在程序里）；在右边的对话里说就能改 */
  /* 打分标准（U31）：只显示中文说明，不显示程序名和权重 */
  const criteriaHtml = (i) => (i.grading_criteria || []).length ? '<ul class="small" style="margin:0;padding-left:18px">' + (i.grading_criteria || []).map((c) => '<li>' + esc(c.description || '') + '</li>').join('') + '</ul><div class="hint">' + t('icp3.aiHint') + '</div>' : '<div class="small muted">' + t('icp3.notYet') + '</div>';
  const EMPTY_ICP = () => ({ name: '', product: '', countries: [], customer_types: [], industries: [], company_size: '', buy_words: [], product_words: [], exclude_words: [], exclude_companies: [], reference_customers: [], contact_titles: [], relax_policy: { allow_neighbor_countries: false, allow_change_customer_type: false, allow_more_industries: false, notes: '' }, qualified_grades: ['A', 'B'] });
  P.icp = {
    title: () => t('nav.icp'),
    async render(params) {
      const list = await api('GET', '/api/icps');
      const id = params.new ? null : +(params.id || (list[0] || {}).id || 0) || null;
      const st = S.icpEd = { id, icp: EMPTY_ICP(), reasons: [], summary: '', chat: [], origin: 'manual', mode: localStorage.getItem('st.genmode') || 'website', list };
      if (id) { const x = await api('GET', '/api/icps/' + id); Object.assign(st, { icp: Object.assign(EMPTY_ICP(), x.data), reasons: x.reasons || [], chat: x.chat || [], origin: x.origin, name: x.name }); }
      /* U35：单栏 —— 页头下拉选模板（新建 / 复制 / 删除也在页头），表单占满中间，右边是聊天修改 */
      const cur = list.find((x) => x.id === id);
      return '<div class="page-head"><div><h1>' + t('icp.title') + '</h1><p>' + t('icp2.sub') + '</p></div><div class="row wrap" style="gap:8px">' +
        '<select class="select" id="i-pick" style="width:auto;min-width:220px">' + list.map((x) => '<option value="' + x.id + '" ' + (x.id === id ? 'selected' : '') + '>' + esc(x.name) + '</option>').join('') + '<option value="new" ' + (!id ? 'selected' : '') + '>＋ ' + esc(t('icp.new')) + '</option></select>' +
        (id ? '<button class="btn btn-secondary btn-sm" data-action="icpCopy">' + icon('copy') + t('icp.copy') + '</button><button class="btn btn-ghost btn-icon btn-sm" data-action="icpDelete" title="' + esc(t('c.delete')) + '">' + icon('trash') + '</button>' : '') +
        '<button class="btn btn-primary" id="i-save">' + icon('check') + t('icp.saveTpl') + '</button></div></div>' +
        (cur ? '<p class="xs muted" style="margin:-8px 0 12px">' + t('icp.stats', { leads: cur.lead_count, samples: cur.sample_count }) + ' · ' + U.fmtTime(cur.updated_at) + '</p>' : '') +
        '<div class="grid grid-main-side" style="align-items:start"><div class="grid" style="min-width:0">' + (!id ? genCard(st) : '') +
        '<div class="card"><div class="card-header"><h3>' + icon('target') + t('icp.fields') + '</h3><span class="sub">' + esc(st.icp.name || t('icp.new')) + '</span></div><div class="card-body" id="icp-form"></div></div>' +
        '<div class="card" id="icp-reasons"></div><div class="card" id="icp-samples"></div></div>' +
        '<div class="card chat" style="height:calc(100vh - 200px);min-height:520px;position:sticky;top:0"><div class="card-header"><h3>' + avatar('manager', 'sm') + t('icp.chat') + '</h3></div><div class="chat-scroll" id="icp-chat"></div>' +
        '<div class="chat-input"><textarea class="textarea" id="icp-in" placeholder="' + esc(t('icp.chat_ph')) + '"></textarea><button class="btn btn-primary btn-icon" id="icp-send">' + icon('send') + '</button></div></div></div>';
    },
    mount(root) { icpMount(root); },
  };
  function genCard(st) {
    return '<div class="card"><div class="card-header"><h3>' + icon('sparkles') + t('icp.genTitle') + '</h3><span class="sub">' + t('icp.ai_gen_sub') + '</span></div><div class="card-body"><div class="gen-source" id="gen-mode">' +
      [['website', 'globe', 'icp.mode_website'], ['text', 'edit', 'icp.mode_text'], ['pdf', 'file', 'icp.mode_pdf']].map((m) => '<button class="' + (st.mode === m[0] ? 'active' : '') + '" data-m="' + m[0] + '">' + icon(m[1], 'lg') + '<b>' + t(m[2]) + '</b></button>').join('') + '</div>' +
      '<div style="margin-top:14px" id="gen-in"></div><div class="row" style="margin-top:10px"><button class="btn btn-ai" id="gen-go">' + icon('sparkles') + t('icp.generate') + '</button><span class="xs muted">' + t('icp.gen_note') + '</span></div></div></div>';
  }
  function icpMount(root) {
    const st = S.icpEd, W = {};
    st.dirty = false;
    S.leaveGuard = () => st.dirty;                     // U36：改了没保存就离开 → 先提醒
    const markDirty = () => { st.dirty = true; };
    root.addEventListener('input', (e) => { if (e.target.closest('#icp-form')) markDirty(); });
    root.addEventListener('change', (e) => { if (e.target.closest('#icp-form')) markDirty(); });
    root.addEventListener('click', (e) => { if (e.target.closest('#icp-form .tag')) markDirty(); });
    const pick = U.$('#i-pick', root);
    if (pick) pick.onchange = () => { U.go('icp', pick.value === 'new' ? { new: 1 } : { id: pick.value }); };
    const genIn = () => {
      const box = U.$('#gen-in', root); if (!box) return;
      box.innerHTML = st.mode === 'website' ? '<div class="input-group">' + icon('globe') + '<input class="input" id="gen-url" placeholder="https://www.your-company.com"></div>'
        : st.mode === 'text' ? '<textarea class="textarea" id="gen-text" placeholder="' + esc(t('icp.text_ph')) + '"></textarea>'
          : '<label class="dropzone" style="display:block">' + icon('upload', 'xl') + '<div style="margin-top:8px" id="gen-fname">' + t('icp.drop') + '</div><input type="file" id="gen-file" accept="application/pdf,.pdf" style="display:none"></label>';
      const f = U.$('#gen-file', root); if (f) f.onchange = () => { U.$('#gen-fname', root).textContent = f.files[0] ? f.files[0].name : t('icp.drop'); };
    };
    U.$$('#gen-mode button', root).forEach((b) => { b.onclick = () => { st.mode = b.dataset.m; try { localStorage.setItem('st.genmode', st.mode); } catch (e) { /* ignore */ } U.$$('#gen-mode button', root).forEach((x) => x.classList.toggle('active', x === b)); genIn(); }; });
    genIn();
    const go = U.$('#gen-go', root);
    if (go) go.onclick = async (e) => {
      const fd = new FormData(); fd.append('mode', st.mode);
      if (st.mode === 'website') fd.append('url', U.$('#gen-url', root).value.trim());
      if (st.mode === 'text') fd.append('text', U.$('#gen-text', root).value.trim());
      if (st.mode === 'pdf') { const f = U.$('#gen-file', root).files[0]; if (f) fd.append('file', f); }
      try {
        const r = await busy(e.currentTarget, () => api('POST', '/api/icps/generate', fd));
        st.icp = Object.assign(EMPTY_ICP(), r.icp); st.reasons = r.reasons; st.summary = r.summary; st.origin = st.mode;
        form(); reasons(); markDirty(); toast(t('icp.generated'));
      } catch (err) { toastErr(err); }
    };
    const form = () => {
      const i = st.icp, f = U.$('#icp-form', root);
      const row = (k, label, req, html) => '<div class="icp-field"><div class="k">' + label + (req ? ' <span style="color:var(--color-danger)">*</span>' : '') + '</div><div id="' + k + '">' + (html || '') + '</div></div>';
      /* 线索词放在最上面（PRD §3）：求购词 / 产品词 / 排除词，中 / 英 / 马来文 */
      f.innerHTML = '<div class="icp-words" style="margin-bottom:14px;padding:12px;border:1px solid var(--color-border);border-radius:var(--radius-md);background:var(--color-surface-2)">' +
        '<div style="margin-bottom:6px"><b class="row" style="gap:6px">' + icon('search') + t('icp4.words') + '</b><div class="xs muted" style="margin-top:2px">' + t('icp4.wordsHint') + '</div></div>' +
        row('f-buy', t('icp4.buy'), true) + row('f-prod', t('icp4.prod'), true) + row('f-exw', t('icp4.exw')) + '</div>' +
        row('f-name', t('icp.name'), true, '<input class="input" id="f-name-i" value="' + esc(i.name) + '">') +
        row('f-product', t('icp.product'), true, '<textarea class="textarea" id="f-product-i">' + esc(i.product) + '</textarea>') +
        row('f-countries', t('icp.countries')) + row('f-types', t('icp.customer_types'), true) + row('f-ind', t('icp.industries')) +
        row('f-size', t('icp.company_size'), false, '<input class="input" id="f-size-i" value="' + esc(i.company_size) + '" placeholder="' + esc(t('icp.size_ph')) + '">') +
        row('f-exc', t('icp.exclude_companies')) + row('f-ref', t('icp.reference_customers')) + row('f-titles', t('icp.contact_titles')) +
        row('f-relax', t('icp.relax'), false, ['allow_neighbor_countries:icp.relax_neighbor', 'allow_change_customer_type:icp.relax_type', 'allow_more_industries:icp.relax_industry'].map((x) => { const [k, l] = x.split(':'); return '<label class="checkbox small" style="display:flex"><input type="checkbox" data-relax="' + k + '" ' + (i.relax_policy[k] ? 'checked' : '') + '>' + t(l) + '</label>'; }).join('') +
          '<input class="input input-sm" id="f-rn" style="margin-top:6px" value="' + esc(i.relax_policy.notes || '') + '" placeholder="' + esc(t('icp.relax_notes')) + '"><div class="hint">' + t('icp.relax_hint') + '</div>') +
        row('f-criteria', t('icp3.criteria'), false, criteriaHtml(i)) +
        row('f-grades', t('icp.grades'), false, '<div class="row">' + ['A', 'B', 'C'].map((g) => '<label class="checkbox"><input type="checkbox" data-g="' + g + '" ' + (i.qualified_grades.includes(g) ? 'checked' : '') + ' ' + (g === 'A' ? 'disabled' : '') + '>' + grade(g) + '</label>').join('') + '</div><div class="hint">' + t('icp.grades_hint') + '</div>');
      W.countries = countryField(i.countries);
      U.$('#f-countries', root).appendChild(W.countries);
      U.$('#f-countries', root).insertAdjacentHTML('beforeend', '<div id="f-adapt" style="margin-top:8px">' + adaptation(i.countries) + '</div>');
      W.types = tagInput(i.customer_types, { placeholder: t('icp.types_ph'), format: U.ctypeName });
      U.$('#f-types', root).appendChild(W.types);
      [['buy', 'buy_words', 'f-buy', 'icp4.buyPh'], ['prod', 'product_words', 'f-prod', 'icp4.prodPh'], ['exw', 'exclude_words', 'f-exw', 'icp4.exwPh'],
        ['ind', 'industries', 'f-ind', 'icp.tag_ph'], ['exc', 'exclude_companies', 'f-exc', 'icp.exc_ph'], ['ref', 'reference_customers', 'f-ref', 'icp.ref_ph'], ['titles', 'contact_titles', 'f-titles', 'icp.tag_ph']]
        .forEach(([w, k, el, ph]) => { W[w] = tagInput(i[k] || [], { placeholder: t(ph) }); U.$('#' + el, root).appendChild(W[w]); });
      if (!(i.buy_words || []).length && !(i.product_words || []).length) U.$('#f-buy', root).insertAdjacentHTML('beforeend', '<div class="hint">' + t('icp4.emptyHint') + '</div>');
    };
    // 保留表单里没有的字段（订单大小、采购场景、求购说法、打分标准……），只覆盖表单里改的
    const collect = () => Object.assign({}, st.icp, {
      name: U.$('#f-name-i', root).value.trim(), product: U.$('#f-product-i', root).value.trim(), countries: W.countries.get(), customer_types: W.types.get(),
      industries: W.ind.get(), company_size: U.$('#f-size-i', root).value.trim(),
      buy_words: W.buy.get(), product_words: W.prod.get(), exclude_words: W.exw.get(),
      exclude_companies: W.exc.get(), reference_customers: W.ref.get(), contact_titles: W.titles.get(),
      relax_policy: Object.assign({ notes: U.$('#f-rn', root).value.trim() }, Object.fromEntries(U.$$('[data-relax]', root).map((c) => [c.dataset.relax, c.checked]))),
      qualified_grades: ['A'].concat(['B', 'C'].filter((g) => U.$('[data-g=' + g + ']', root).checked)),
    });
    const reasons = () => {
      const c = U.$('#icp-reasons', root);
      c.innerHTML = '<div class="card-header"><h3>' + icon('sparkles') + t('icp.reasons') + '</h3></div><div class="card-body small">' + (st.summary ? '<p>' + esc(st.summary) + '</p>' : '') +
        (st.reasons.length ? st.reasons.map((r) => '<div class="why">' + icon('sparkles') + '<span><b>' + esc(U.has('icp.' + r.field) ? t('icp.' + r.field) : r.field) + '</b>：' + esc(r.reason) + '</span></div>').join('') : '<span class="muted">' + t('icp.reasons_empty') + '</span>') + '</div>';
    };
    const chat = () => {
      const box = U.$('#icp-chat', root);
      box.innerHTML = '<div class="msg" data-agent="manager">' + avatar('manager', 'sm') + '<div><div class="who">' + t('agent.manager') + '</div><div class="bubble">' + t('icp3.chatIntro') + '</div></div></div>' +
        st.chat.map((m) => m.role === 'user' ? '<div class="msg me"><div><div class="bubble">' + U.para(m.content) + '</div></div></div>' : '<div class="msg" data-agent="manager">' + avatar('manager', 'sm') + '<div><div class="bubble">' + U.para(m.content) + '</div></div></div>').join('');
      box.scrollTop = box.scrollHeight;
    };
    const send = async () => {
      const inp = U.$('#icp-in', root), msg = inp.value.trim(); if (!msg) return;
      const cur = collect(); st.chat.push({ role: 'user', content: msg }); inp.value = ''; chat();
      try {
        const r = await busy(U.$('#icp-send', root), () => api('POST', '/api/icps/chat', { icp: cur, message: msg, history: st.chat.slice(0, -1) }));
        st.icp = Object.assign(EMPTY_ICP(), r.icp); markDirty();
        st.chat.push({ role: 'assistant', content: r.reply + (r.changes && r.changes.length ? '\n' + r.changes.map((x) => '• ' + x).join('\n') : '') });
        chat(); form(); toast(t('icp.changed', { n: (r.changes || []).length || 1 }));
      } catch (err) { st.chat.pop(); chat(); inp.value = msg; toastErr(err); }
    };
    U.$('#icp-send', root).onclick = send;
    U.$('#icp-in', root).addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
    const samples = async () => {
      const c = U.$('#icp-samples', root);
      if (!st.id) { c.innerHTML = '<div class="card-header"><h3>' + icon('brain') + t('learn.title') + '</h3></div><div class="card-body small muted">' + t('learn.save_first') + '</div>'; return; }
      let list = []; try { list = await api('GET', '/api/icps/' + st.id + '/samples'); } catch (e) { /* ignore */ }
      const pos = list.filter((x) => x.polarity > 0).length;
      c.innerHTML = '<div class="card-header"><h3>' + icon('brain') + t('learn.title') + '</h3><span class="tag tag-success">' + icon('thumbsUp') + pos + '</span><span class="tag tag-danger">' + icon('thumbsDown') + (list.length - pos) + '</span><div class="actions"><button class="btn btn-secondary btn-sm" id="s-add">' + icon('plus') + t('learn.add') + '</button></div></div>' +
        '<div class="card-body"><p class="xs muted" style="margin-bottom:8px">' + t('learn.sub') + '</p>' + (list.length ? list.slice(0, 30).map((s) => '<div class="plan-item" style="padding-left:0;padding-right:0"><span>' + icon(s.polarity > 0 ? 'thumbsUp' : 'thumbsDown') + '</span><div class="body"><div class="small" style="white-space:pre-wrap">' + esc(s.text.slice(0, 220)) + (s.text.length > 220 ? '…' : '') + '</div><div class="xs muted">' + t('learn.origin_' + s.origin) + (s.reason ? ' · ' + t('fbr.' + s.reason) : '') + (s.note ? ' · ' + esc(s.note) : '') + ' · ' + U.fmtTime(s.created_at) + '</div></div><button class="btn btn-ghost btn-icon btn-sm" data-sdel="' + s.id + '">' + icon('trash') + '</button></div>').join('') : '<div class="small muted">' + t('learn.empty') + '</div>') + '</div>';
      U.$$('[data-sdel]', c).forEach((b) => { b.onclick = async () => { try { await api('DELETE', '/api/samples/' + b.dataset.sdel); samples(); } catch (e) { toastErr(e); } }; });
      U.$('#s-add', c).onclick = () => sampleDialog(st.id, samples);
    };
    form(); reasons(); chat(); samples();
    U.$('#i-save', root).onclick = async (e) => {
      const data = collect();
      try {
        const saved = await busy(e.currentTarget, () => (st.id ? api('PUT', '/api/icps/' + st.id, { data, reasons: st.reasons, chat: st.chat }) : api('POST', '/api/icps', { data, reasons: st.reasons, chat: st.chat, origin: st.origin })));
        toast(t('icp.saved'));
        st.dirty = false;
        U.go('icp', { id: saved.id });
      } catch (err) { toastErr(err); }
    };
  }
  A.icpCopy = async () => { const r = await api('POST', '/api/icps/' + S.icpEd.id + '/copy'); toast(t('icp.copied')); U.go('icp', { id: r.id }); };
  A.icpDelete = async () => { if (await U.confirmDialog({ title: t('icp.delete_title'), body: t('icp.delete_confirm', { name: S.icpEd.icp.name }), ok: t('c.delete'), danger: true, onOk: () => api('DELETE', '/api/icps/' + S.icpEd.id) })) { toast(t('common.deleted')); U.go('icp'); } };

  window.leadHelpers = { contactLine, statusTag, srcTag, contactsOf };
})();

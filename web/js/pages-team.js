/* 页面（二）：AI 记忆（备忘录 / 学到的经验 / 行业作战手册 / 版本）、会议室（第 3 步占位）、提醒中心（铃铛抽屉）、单点对话（→ 工作台 @某 AI） */
(function () {
  'use strict';
  const S = window.STATE, U = window.UI, A = window.ACTIONS, P = window.PAGES;
  const { esc, avatar, empty, api, toast, toastErr, busy } = U;
  const AG = () => U.agentKeys();   // v1：不含销售员 / 跟进员（后端 FEATURES["outreach"]）

  /* =================================================================
     ⑥ AI 记忆
     ================================================================= */
  P.learning = {
    async render(params) {
      const tab = S.tabs.learning || 'memos';
      const ov = S.learnOv = await api('GET', '/api/learning/overview');
      const icpId = S.learnIcp = +(params.icp || (ov.templates[0] || {}).id || 0);
      let body = '';
      if (tab === 'memos') body = await memosTab(params);
      else if (tab === 'exp') body = await expTab(ov, icpId);
      else if (tab === 'playbook') body = await playbookTab();
      else if (tab === 'prompts') body = await promptsTab(params);
      else body = await versionsTab(ov, icpId);
      return '<div class="page-head"><div><h1>' + t('nav.learning') + '</h1><p>' + t('mem.sub') + '</p></div></div>' +
        U.tabs('learning', [['memos', t('mem.memos'), 'edit', ov.memos_pending || ''], ['exp', t('mem.exp'), 'brain', ov.pending.length || ''], ['playbook', t('company.title'), 'building'], ['prompts', t('prm.tab'), 'file'], ['versions', t('ln.versions'), 'undo']], 'memos') + body;
    },
    mount(root) {
      const ov = S.learnOv;
      const form = U.$('#company-editor', root);
      if (form) {
        const dirty = () => Object.keys(U.companyPatch(form, S.companyOriginal)).length > 0;
        const sync = () => { U.$('#company-save', form).disabled = !dirty(); U.$('#company-cancel', form).disabled = !dirty(); };
        S.leaveGuard = () => form.isConnected && dirty();
        form.addEventListener('input', sync); sync();
        U.$('#company-cancel', form).onclick = () => { S.leaveGuard = null; U.refresh(); };
        U.$('#company-save', form).onclick = async (e) => {
          if (!U.companyValid(form)) return;
          try { await busy(e.currentTarget, () => api('PUT', '/api/company', U.companyPatch(form, S.companyOriginal))); S.leaveGuard = null; toast(t('c.saved')); U.refresh(); }
          catch (err) { toastErr(err); }
        };
      }
      U.$$('[data-tag-mount]', root).forEach((el) => { const w = window.W.tagInput(JSON.parse(el.dataset.values || '[]'), { placeholder: t(el.dataset.ph) }); el.appendChild(w); el._w = w; });
      const ch = U.$('#acc-chart', root);
      if (ch) {
        // v1 没有人工审核：质量看“这条不好”的比例和成交比例（按周，推送给销售的线索）
        const pts = ov.quality || [];
        if (!pts.length) ch.innerHTML = '<div class="small muted">' + t('learn2.noQuality') + '</div>';
        else window.CHARTS.lineChart(ch, { labels: pts.map((c) => c.period.replace(/^\d{4}-/, '')), min: 0, max: 100, ticks: [0, 50, 100], unit: '%', height: 200,
          series: [{ agent: 'sales', name: t('learn2.badRate'), values: pts.map((c) => Math.round(c.bad_rate * 100)) },
            { agent: 'followup', name: t('learn2.dealRate'), values: pts.map((c) => Math.round(c.deal_rate * 100)) }] });
      }
      if (ov.embed.download.state === 'downloading') U.every(3000, () => { if (S.route === 'learning') U.refresh(); });
    },
  };
  A.learnIcp = (el) => U.go('learning/' + (S.tabs.learning || 'exp'), { icp: el.value });

  /* ---- 备忘录：每条可确认 / 改 / 删；下方是“每个 AI 记得什么”（记忆分层） */
  async function memosTab(params) {
    const agent = params.agent || 'manager';
    const [list, layers] = await Promise.all([api('GET', '/api/memos'), api('GET', '/api/memory/' + agent)]);
    const rows = list.slice().sort((x, y) => (y.status === 'pending') - (x.status === 'pending'));
    return '<div class="grid grid-main-side"><div style="min-width:0"><div class="card">' + (rows.length ? rows.map(memoRow).join('') : empty({ icon: 'edit', title: t('mem.empty'), body: t('mem2.emptySub') })) +
      '<div class="card-footer"><select class="select input-sm" id="m-agent" style="width:150px"><option value="all">' + t('mem2.allAgents') + '</option>' + AG().map((k) => '<option value="' + k + '">' + esc(t('agent.' + k)) + '</option>').join('') + '</select>' +
      '<input class="input input-sm grow" id="m-new" placeholder="' + esc(t('memo.add_ph')) + '"><button class="btn btn-secondary btn-sm" data-action="memoAdd">' + icon('plus') + t('memo.add') + '</button></div></div>' +
      '<p class="xs muted" style="margin-top:10px">' + icon('shield') + ' ' + t('mem2.layersSub') + '</p></div>' +
      '<div class="grid" style="align-self:start"><div class="card"><div class="card-header"><h3>' + icon('brain') + t('mem2.layers') + '</h3></div><div class="card-body">' +
      '<div class="row wrap" style="margin-bottom:12px">' + AG().map((k) => '<span class="chip chip-sm' + (k === agent ? ' active' : '') + '" data-agent="' + k + '" data-action="go" data-route="learning/memos" data-q="agent=' + k + '">' + avatar(k, 'sm') + esc(t('agent.' + k)) + '</span>').join('') + '</div>' +
      '<div class="section-title">' + icon('file') + t('mem.role') + '</div><p class="small text-2">' + esc(U.has('role.' + agent) ? t('role.' + agent) : t('agent_desc.' + agent)) + '</p><button class="btn btn-ghost btn-sm" data-action="go" data-route="learning/prompts" data-q="agent=' + agent + '">' + icon('edit') + t('prm.viewEdit') + '</button>' +
      '<div class="section-title" style="margin-top:14px">' + icon('lock') + t('mem.numbers') + ' ' + U.lockTag() + '</div><div class="kv small"><span class="k">' + t('budget.daily') + '</span><span>$' + layers.numbers.budget.daily + '</span><span class="k">' + t('budget.monthly') + '</span><span>$' + layers.numbers.budget.monthly + '</span><span class="k">' + t('push.cutoff') + '</span><span>' + esc(layers.numbers.notify_cutoff) + '</span></div>' +
      /* U37：程序的工作记录（英文、给 AI 看的）默认收起 */
      '<details style="margin-top:14px"><summary class="small text-2" style="cursor:pointer">' + icon('activity') + ' ' + t('mem.workTech') + '</summary><p class="xs muted">' + t('mem.work_sub') + '</p><pre class="small" style="white-space:pre-wrap;margin:6px 0 0;font-family:var(--font-mono)">' + esc(layers.work) + '</pre></details></div></div></div></div>';
  }
  function memoRow(m) {
    const who = m.agent === 'all' ? t('mem2.allAgents') : t('agent.' + m.agent);
    return '<div class="plan-item mem-row" data-agent="' + (m.agent === 'all' ? 'manager' : m.agent) + '"><span class="mem-ic">📌</span><div class="body"><div class="title">' + esc(who) + '：' + esc(m.text) + '</div><div class="desc xs">' + t('memo.origin.' + m.origin) + ' · ' + U.fmtTime(m.created_at) + '</div></div>' +
      (m.status === 'pending' ? '<span class="tag tag-warning">' + t('mem.pending') + '</span><button class="btn btn-primary btn-sm" data-action="memoOk" data-id="' + m.id + '">' + t('mem.confirm') + '</button><button class="btn btn-ghost btn-sm" data-action="memoNo" data-id="' + m.id + '">' + t('ln.dismiss') + '</button>'
        : m.status === 'confirmed' ? '<span class="tag tag-success">' + t('mem.active') + '</span><button class="btn btn-ghost btn-icon btn-sm" data-action="memoEdit" data-id="' + m.id + '" data-text="' + esc(m.text) + '" title="' + esc(t('c.edit')) + '">' + icon('edit') + '</button><button class="btn btn-ghost btn-icon btn-sm" data-action="memoDel" data-id="' + m.id + '" title="' + esc(t('c.delete')) + '">' + icon('trash') + '</button>'
          : '<span class="tag">' + t('memo.st.' + m.status) + '</span><button class="btn btn-ghost btn-icon btn-sm" data-action="memoDel" data-id="' + m.id + '">' + icon('trash') + '</button>') + '</div>';
  }
  A.memoOk = async (el) => { await api('POST', '/api/memos/' + el.dataset.id + '/decide', { confirm: true }); toast(t('mem.activeNow')); U.refreshBadges(); U.refresh(); };
  A.memoNo = async (el) => { await api('POST', '/api/memos/' + el.dataset.id + '/decide', { confirm: false }); toast(t('mem.dropped')); U.refreshBadges(); U.refresh(); };
  A.memoDel = async (el) => { await api('DELETE', '/api/memos/' + el.dataset.id); toast(t('common.deleted')); U.refresh(); };
  A.memoAdd = async () => { const v = U.$('#m-new').value.trim(); if (!v) { U.$('#m-new').classList.add('is-error'); return; } await api('POST', '/api/memos', { agent: U.$('#m-agent').value, text: v }); toast(t('memo.added')); U.refresh(); };
  A.memoEdit = async (el) => {
    const ok = await U.confirmDialog({ title: t('memo.edit'), html: true, body: '<textarea class="textarea" id="me-t" style="min-height:90px">' + esc(el.dataset.text) + '</textarea>', ok: t('c.save'),
      onOk: (w) => api('PUT', '/api/memos/' + el.dataset.id, { text: U.$('#me-t', w).value }) });
    if (ok) U.refresh();
  };

  /* ---- 提示词（PRD 2.4.2）：属于客户，可以查看和修改；默认的只写“怎么干活”，业务内容都来自客户的资料 */
  const PRM_GROUPS = () => ['all'].concat(AG(), ['decider']);
  async function promptsTab(params) {
    const d = S.promptData = await api('GET', '/api/prompts');
    const g = params.agent || 'all';
    const items = d.items.filter((x) => g === 'all' ? true : x.agent === g);
    const chip = (k) => '<span class="chip chip-sm' + (k === g ? ' active' : '') + '" data-action="go" data-route="learning/prompts" data-q="agent=' + k + '">' + (AG().includes(k) ? avatar(k, 'sm') : '') + esc(k === 'all' ? t('prm.all') : t('agent.' + k)) + '</span>';
    return '<div class="alert alert-info" style="margin-bottom:16px">' + icon('info') + '<span>' + t('prm.intro') + '</span></div>' +
      '<div class="row wrap" style="margin-bottom:12px">' + PRM_GROUPS().map(chip).join('') + '<span class="grow"></span><span class="xs muted">' + t('prm.version', { v: d.version }) + '</span></div>' +
      '<div class="card">' + items.map((x) => '<div class="plan-item" style="align-items:center"><span class="text-2">' + (AG().includes(x.agent) ? avatar(x.agent, 'sm') : icon(x.agent === 'decider' ? 'cpu' : 'file')) + '</span><div class="body"><div class="title">' + esc(U.has('prm.' + x.name) ? t('prm.' + x.name) : x.name) +
        (x.edited ? '<span class="tag tag-warning">' + t('prm.edited') + '</span>' : '') + '</div><div class="desc xs mono">' + esc(x.name) + (x.vars.length ? ' · ' + x.vars.map((v) => '{{' + v + '}}').join(' ') : '') + '</div></div>' +
        '<button class="btn btn-secondary btn-sm" data-action="promptEdit" data-name="' + esc(x.name) + '">' + icon('edit') + t('prm.viewEdit') + '</button>' +
        (x.edited ? '<button class="btn btn-ghost btn-sm" data-action="promptReset" data-name="' + esc(x.name) + '">' + icon('undo') + t('prm.reset') + '</button>' : '') + '</div>').join('') + '</div>';
  }
  A.promptEdit = async (el) => {
    const x = (S.promptData.items || []).find((i) => i.name === el.dataset.name);
    if (!x) return;
    const hint = (x.choice ? t('prm.choiceHint') + ' ' : '') + (x.vars.length ? t('prm.varsHint', { v: x.vars.map((v) => '{{' + v + '}}').join(' ') }) : '');
    const ok = await U.confirmDialog({ title: U.has('prm.' + x.name) ? t('prm.' + x.name) : x.name, html: true, cls: 'lg', ok: t('c.save'),
      body: '<p class="xs muted mono" style="margin-bottom:6px">' + esc(x.name) + '</p><textarea class="textarea mono" id="pe-t" style="min-height:320px;font-size:12px">' + esc(x.text) + '</textarea>' + (hint ? '<span class="hint">' + esc(hint) + '</span>' : ''),
      onOk: (w) => api('PUT', '/api/prompts/' + encodeURIComponent(x.name), { text: U.$('#pe-t', w).value }) });
    if (ok) { toast(t('c.saved')); U.refresh(); }
  };
  A.promptReset = async (el) => {
    const ok = await U.confirmDialog({ title: t('prm.reset'), body: t('prm.resetBody'), ok: t('prm.reset'), onOk: () => api('DELETE', '/api/prompts/' + encodeURIComponent(el.dataset.name)) });
    if (ok) { toast(t('prm.resetDone')); U.refresh(); }
  };

  /* ---- 学到的经验：待确认的规则 · 关键词 / 排除名单 / 消息类型（可改，存为新版本）· 通过率曲线 · 判断模型自检 · 本机语义模型 */
  async function expTab(ov, icpId) {
    const d = icpId ? await api('GET', '/api/learning/' + icpId) : null;
    const types = d ? d.knowledge.filter((k) => k.kind === 'message_type' && ['auto_applied', 'confirmed'].includes(k.status)) : [];
    const dec = ov.decision, tests = (dec.tests || []).filter((x) => x.action !== 'insufficient_samples').slice().reverse();
    const last = tests[tests.length - 1];
    const pend = ov.pending.map((k) => '<div class="plan-item mem-row" data-agent="monitor"><span class="mem-ic">💡</span><div class="body"><div class="title">' + esc(k.icp_name) + ' · ' + t('kn.kind.' + k.kind) + '：' + esc((k.content && k.content.name) || JSON.stringify(k.content)) + '</div><div class="desc xs">' + esc((k.content && k.content.description) || '') + ' · ' + t('learn.big_change') + '</div></div>' +
      '<span class="tag tag-warning">' + t('mem.pending') + '</span><button class="btn btn-primary btn-sm" data-action="knDecide" data-id="' + k.id + '" data-ok="1">' + t('mem.confirm') + '</button><button class="btn btn-ghost btn-sm" data-action="knDecide" data-id="' + k.id + '" data-ok="0">' + t('ln.dismiss') + '</button></div>').join('');
    const tplSel = ov.templates.length > 1 ? '<select class="select input-sm" style="width:auto" data-change="learnIcp">' + ov.templates.map((x) => '<option value="' + x.id + '" ' + (x.id === icpId ? 'selected' : '') + '>' + esc(x.name) + '</option>').join('') + '</select>' : (ov.templates[0] ? '<span class="tag tag-primary">' + icon('target') + esc(ov.templates[0].name) + '</span>' : '');
    return '<div class="grid grid-main-side"><div class="grid" style="align-self:start;min-width:0">' +
      (pend ? '<div class="card">' + pend + '</div>' : '') +
      (!icpId ? '<div class="card">' + empty({ icon: 'target', title: t('icp.empty'), actions: '<button class="btn btn-primary" data-action="go" data-route="onboarding">' + t('nav.onboarding') + '</button>' }) + '</div>' :
        /* U32：线索词、排除词、好坏例子只在「模板管理」里改；这里只看 AI 自己加了什么（只读 + 撤销） */
        '<div class="card"><div class="card-header"><h3>' + icon('search') + t('learn2.aiWords') + '</h3>' + tplSel + (d.effective.keywords.length ? '<div class="actions"><button class="btn btn-ghost btn-sm" data-action="learnUndo" data-kind="keywords">' + icon('undo') + t('learn2.undo') + '</button></div>' : '') + '</div>' +
        '<div class="card-body"><p class="xs muted" style="margin-bottom:8px">' + t('learn2.aiWordsSub') + '</p>' + (d.effective.keywords.length ? '<div class="row wrap" style="gap:6px">' + d.effective.keywords.map((k) => '<span class="tag">' + esc(k) + '</span>').join('') + '</div>' : '<div class="small muted">' + t('learn.none_yet') + '</div>') + '</div></div>' +
        '<div class="card"><div class="card-header"><h3>' + icon('xCircle') + t('learn2.aiExcl') + '</h3>' + ((d.effective.exclude_keywords.length || d.effective.exclude_companies.length) ? '<div class="actions"><button class="btn btn-ghost btn-sm" data-action="learnUndo" data-kind="exclusions">' + icon('undo') + t('learn2.undo') + '</button></div>' : '') + '</div><div class="card-body"><p class="xs muted" style="margin-bottom:8px">' + t('learn2.aiExclSub') + '</p>' +
        ((d.effective.exclude_keywords.length || d.effective.exclude_companies.length) ? '<div class="row wrap" style="gap:6px">' + d.effective.exclude_keywords.concat(d.effective.exclude_companies).map((k) => '<span class="tag">' + esc(k) + '</span>').join('') + '</div>' : '<div class="small muted">' + t('learn.none_yet') + '</div>') + '</div></div>' +
        '<div class="card"><div class="card-header"><h3>' + icon('message') + t('learn.types') + '</h3><span class="sub">' + t('learn.types_sub') + '</span></div>' +
        (types.length ? types.map((k) => '<div class="plan-item mem-row" data-agent="monitor"><span class="mem-ic">💡</span><div class="body"><div class="title" style="font-weight:400"><b>' + esc(k.content.name || '') + '</b> — ' + esc(k.content.description || '') + '</div><div class="desc xs">v' + k.version + ' · ' + t('kn.st.' + k.status) + '</div></div><span class="tag tag-success">' + t('mem.active') + '</span></div>').join('') : '<div class="card-body small muted">' + t('learn.none_yet') + '</div>') + '</div>' +
        '<div class="card"><div class="card-header"><h3>' + icon('thumbsUp') + t('learn.tab_samples') + '</h3><span class="tag tag-success">' + icon('thumbsUp') + d.samples.filter((s) => s.polarity > 0).length + '</span><span class="tag tag-danger">' + icon('thumbsDown') + d.samples.filter((s) => s.polarity < 0).length + '</span>' +
        '<span class="sub">' + t('learn2.samplesWhere') + '</span></div>' +
        (d.samples.length ? '<div class="table-wrap" style="max-height:320px"><table class="table"><tbody>' + d.samples.slice(0, 50).map((s) => '<tr><td>' + icon(s.polarity > 0 ? 'thumbsUp' : 'thumbsDown') + '</td><td class="small" style="max-width:380px">' + esc(s.text.slice(0, 160)) + (s.text.length > 160 ? '…' : '') + '</td><td>' + U.agentChip(s.agent) + '</td><td class="xs muted">' + t('learn.origin_' + s.origin) + (s.reason ? ' · ' + t('fbr.' + s.reason) : '') + '</td></tr>').join('') + '</tbody></table></div>'
          : '<div class="card-body">' + empty({ icon: 'thumbsUp', title: t('learn.empty'), body: t('learn.empty_sub') }) + '</div>') + '</div>') + '</div>' +
      '<div class="grid" style="align-self:start"><div class="card"><div class="card-header"><h3>' + icon('chart') + t('learn2.quality') + '</h3></div><div class="card-body"><p class="xs muted">' + t('learn2.qualitySub') + '</p>' +
      '<div class="row" style="gap:18px;margin-top:8px"><div><div class="xs muted">' + t('learn2.badRate') + '</div><b class="num">' + Math.round(ov.rates.bad_rate * 100) + '%</b></div><div><div class="xs muted">' + t('learn2.dealRate') + '</div><b class="num">' + Math.round(ov.rates.deal_rate * 100) + '%</b></div><div><div class="xs muted">' + t('learn2.pushed7') + '</div><b class="num">' + ov.rates.pushed + '</b></div></div>' +
      '<div id="acc-chart" style="margin-top:8px"></div></div></div>' +
      '<div class="card"><div class="card-header"><h3>' + icon('shieldCheck') + t('learn2.spot') + '</h3></div><div class="card-body small"><p class="xs muted">' + t('learn2.spotSub') + '</p>' +
      '<div class="kv" style="margin-top:8px"><span class="k">' + t('learn2.spotRate') + '</span><span>' + Math.round(ov.spot.rate * 100) + '%</span><span class="k">' + t('learn2.spotChecked') + '</span><span>' + ov.spot.checked + '</span><span class="k">' + t('learn2.spotFailed') + '</span><span>' + ov.spot.failed + '</span></div></div></div>' +
      '<div class="card"><div class="card-header"><h3>' + icon('zap') + t('learn.dm_tests') + '</h3></div><div class="card-body">' +
      (dec.fallback_active ? '<div class="alert alert-warning" style="margin-bottom:10px">' + icon('alert') + '<span>' + t('learn.dm_fallback', { r: dec.fallback_reason || '' }) + '</span></div>' : '') +
      (last ? '<div class="row" style="justify-content:space-between;align-items:flex-end"><div><div class="xs muted">' + t('dm.lastAcc') + '</div><div class="kpi-value" style="font-size:var(--fs-2xl)">' + Math.round(last.accuracy * 100) + '%</div><div class="xs muted">' + last.correct + '/' + last.n + ' · ' + U.fmtTime(last.ts) + '</div></div>' + (tests.length > 1 ? window.CHARTS.sparkline(tests.map((x) => Math.round(x.accuracy * 100)), 140, 44) : '') + '</div>'
        : '<p class="small muted">' + t('learn.dm_tests_sub') + '</p>') +
      '<div class="xs muted" style="margin-top:8px">' + t('agent.decider') + '：' + (dec.configured ? esc(dec.model) + (dec.api_name ? ' · ' + esc(String(dec.api_name).startsWith('@') ? t(String(dec.api_name).slice(1)) : dec.api_name) : '') : t('wb.s.unconfigured') + ' · ' + t('decider.setWhere')) + '</div></div></div>' +
      '<div class="card"><div class="card-header"><h3>' + icon('cpu') + t('learn.embed') + '</h3></div><div class="card-body small"><p class="xs muted">' + t('learn.embed_sub') + '</p><div style="margin-top:6px">' + esc(ov.embed.model) + ' · ' + esc(ov.embed.license) + ' · ' + ov.embed.size_mb + ' MB</div><div style="margin-top:8px">' +
      (ov.embed.installed ? '<span class="tag tag-success">' + icon('check') + t('learn.embed_ok') + '</span>' : ov.embed.download.state === 'downloading' ? '<div class="progress sm"><span style="width:' + (100 * ov.embed.download.done / Math.max(1, ov.embed.download.total)) + '%"></span></div>' : '<button class="btn btn-secondary btn-sm" data-action="embedDl">' + icon('download') + t('learn.embed_download') + '</button>') +
      (ov.embed.download.state === 'error' ? '<div class="hint error">' + esc(ov.embed.download.error) + '</div>' : '') + '</div></div></div></div></div>';
  }
  /* 撤销 AI 最近一次自动加的词 / 排除项（U32） */
  A.learnUndo = async (el) => { await busy(el, () => api('POST', '/api/learning/' + S.learnIcp + '/undo', { kind: el.dataset.kind })); toast(t('learn2.undone')); U.refresh(); };
  A.knDecide = async (el) => { await api('POST', '/api/learning/knowledge/' + el.dataset.id + '/decide', { accept: el.dataset.ok === '1' }); toast(el.dataset.ok === '1' ? t('ln.applied') : t('ln.dismissed')); U.refreshBadges(); U.refresh(); };
  A.embedDl = async () => { await api('POST', '/api/embed/download'); toast(t('learn.embed_started')); setTimeout(U.refresh, 1200); };

  /* ---- 行业作战手册：第 3 步由销售主管生成；第 2 步显示快速开始整理的我方画像 */
  async function playbookTab() {
    const co = S.companyOriginal = await api('GET', '/api/company');
    return '<div class="card" id="company-editor"><div class="card-header"><h3>' + icon('building') + t('company.title') + '</h3></div><div class="card-body">' + U.companyForm(co) + '</div><div class="card-footer" style="justify-content:flex-end"><button class="btn btn-secondary" id="company-cancel">' + t('company.discard') + '</button><button class="btn btn-primary" id="company-save" disabled>' + t('c.save') + '</button></div></div>';
  }

  /* ---- 版本：学到的经验（每个模板）+ 画像版本（快速开始 / 重新开始设置），都可以回退 */
  async function versionsTab(ov, icpId) {
    const [d, qs] = await Promise.all([icpId ? api('GET', '/api/learning/' + icpId) : null, api('GET', '/api/quickstart')]);
    const tplSel = ov.templates.length > 1 ? '<div class="actions"><select class="select input-sm" style="width:auto" data-change="learnIcp">' + ov.templates.map((x) => '<option value="' + x.id + '" ' + (x.id === icpId ? 'selected' : '') + '>' + esc(x.name) + '</option>').join('') + '</select></div>' : '';
    const kn = d ? d.knowledge : [];
    const curV = kn.filter((k) => ['auto_applied', 'confirmed'].includes(k.status)).reduce((m, k) => Math.max(m, k.version), 0);
    return '<div class="grid grid-2" style="align-items:start"><div class="card"><div class="card-header"><h3>' + icon('undo') + t('ln.versions') + '</h3>' + tplSel + '</div><div class="card-body">' +
      (kn.length ? '<div class="timeline">' + kn.map((k) => '<div class="tl-item' + (k.version === curV ? ' current' : '') + '"><div class="row"><b>v' + k.version + '</b>' + (k.version === curV ? '<span class="tag tag-primary">' + t('ln.current') + '</span>' : '') + '<span class="tag ' + (k.status === 'rolled_back' || k.status === 'rejected' ? 'tag-danger' : k.status === 'pending_confirmation' ? 'tag-warning' : 'tag-success') + '">' + t('kn.st.' + k.status) + '</span><span class="xs muted">' + U.fmtTime(k.created_at) + '</span><span class="grow"></span>' +
        (k.status !== 'rolled_back' && k.version !== curV ? '<button class="btn btn-secondary btn-sm" data-action="knRollback" data-v="' + k.version + '">' + icon('undo') + t('ln.rollback') + '</button>' : '') + '</div><div class="small text-2">' + t('kn.kind.' + k.kind) + ' · ' + t('kn.origin.' + k.origin) + (k.note ? ' · ' + esc(k.note) : '') + '</div></div>').join('') + '</div>'
        : '<div class="small muted">' + t('learn.none_yet') + '</div>') + '</div></div>' +
      '<div class="card"><div class="card-header"><h3>' + icon('building') + t('pv.title') + '</h3><div class="actions"><button class="btn btn-secondary btn-sm" data-action="go" data-route="onboarding" data-q="redo=1">' + icon('refresh') + t('sb.restart_btn') + '</button></div></div><div class="card-body">' +
      ((qs.versions || []).length ? '<div class="timeline">' + qs.versions.map((v) => '<div class="tl-item' + (v.status === 'active' ? ' current' : '') + '"><div class="row"><b>v' + v.version + '</b><span class="tag ' + (v.status === 'active' ? 'tag-primary' : '') + '">' + t('pv.' + v.status) + '</span><span class="xs muted">' + U.fmtTime(v.created_at) + '</span><span class="grow"></span>' +
        (v.status !== 'active' ? '<button class="btn btn-secondary btn-sm" data-action="pvRollback" data-v="' + v.version + '">' + icon('undo') + t('ln.rollback') + '</button>' : '') + '</div></div>').join('') + '</div>'
        : '<div class="small muted">' + t('learn.none_yet') + '</div>') + '<p class="xs muted" style="margin-top:10px">' + t('pv2.kept') + '</p></div></div></div>';
  }
  A.knRollback = async (el) => {
    if (await U.confirmDialog({ title: t('learn.rollback_title', { v: el.dataset.v }), body: t('learn.rollback_body'), ok: t('ln.rollback'),
      onOk: async () => { const r = await api('POST', '/api/learning/' + S.learnIcp + '/rollback', { version: +el.dataset.v }); toast(t('learn.rolled', { n: r.rolled_back })); } })) U.refresh();
  };
  A.pvRollback = async (el) => {
    if (await U.confirmDialog({ title: t('learn.rollback_title', { v: el.dataset.v }), body: t('pv2.kept'), ok: t('ln.rollback'), onOk: () => api('POST', '/api/quickstart/rollback', { version: +el.dataset.v }) })) { toast(t('c.saved')); U.refresh(); }
  };

  /* =================================================================
     ④ 会议室：第 3 步上线（只能由客户召集；开会时全员暂停，会后自动恢复）
     ================================================================= */
  P.meeting = {
    async render() {
      return '<div class="page-head"><div><h1>' + t('mt.title') + '</h1><p>' + t('mt.sub') + '</p></div><div class="row">' +
        '<button class="btn btn-secondary" disabled>' + icon('calendar') + t('mt.scheduleBtn') + '</button><button class="btn btn-primary" disabled>' + icon('meeting') + t('mt.convene') + '</button></div></div>' +
        '<div class="card">' + empty({ icon: 'meeting', title: t('mt2.soon'), body: t('mt2.soonSub'), actions: '<button class="btn btn-secondary" data-action="go" data-route="dashboard">' + t('nav.dashboard') + '</button>' }) + '</div>';
    },
  };

  /* =================================================================
     单点对话（隐藏路由）：改为工作台里的 @某 AI
     ================================================================= */
  P.chat = {
    async render() {
      const k = S.tabs.chat; S.chatTarget = AG().includes(k) ? k : 'manager';
      setTimeout(() => U.go('dashboard'), 0);
      return '';
    },
  };

  /* =================================================================
     提醒中心（铃铛）—— PRD 7.2
     ================================================================= */
  const LV = { urgent: ['danger', 'alert', 'tag-danger'], general: ['warn', 'clock', 'tag-warning'], info: ['', 'info', 'tag-info'] };
  A.openAlerts = async () => {
    const f = S.alertFilter || 'open';
    let d;
    let notices = [];
    try { d = await api('GET', '/api/alerts' + (f === 'all' ? '' : '?status=' + f)); notices = f === 'open' ? (await api('GET', '/api/alerts?status=notice')).items.slice(0, 8) : []; } catch (e) { toastErr(e); return; }
    // 每类提醒可以有自己的标签（例如高意向买家：“原话：”，U58）
    const parts = (a) => ['impact', 'handled', 'action'].filter((k) => U.has('alert.' + a.category + '.' + k)).map((k) => '<div class="small text-2" style="margin-top:4px"><b>' + (U.has('alert.' + a.category + '.label_' + k) ? t('alert.' + a.category + '.label_' + k) : t('alert.label_' + k)) + '</b>' + t('alert.' + a.category + '.' + k, a.params) + '</div>').join('');
    const card = (a) => '<div class="card" style="margin-bottom:10px;padding:14px 16px' + (a.status === 'resolved' ? ';opacity:.7' : '') + '"><div class="row" style="align-items:flex-start"><div class="reminder" style="padding:0;border:0"><div class="ic ' + LV[a.level][0] + '">' + icon(LV[a.level][1]) + '</div></div>' +
      '<div class="grow"><div class="row wrap"><b>' + t('alert.' + a.category + '.title', a.params) + '</b>' + (a.count > 1 ? '<span class="tag">×' + a.count + '</span>' : '') + (a.status === 'notice' ? '' : '<span class="tag ' + (a.status === 'resolved' ? 'tag-success' : LV[a.level][2]) + '">' + (a.status === 'resolved' ? t('al.resolved') : t('al.open')) + '</span>') + '</div>' + parts(a) +
      '<div class="row" style="margin-top:8px"><span class="xs muted">' + U.fmtTime(a.updated_at) + ' · ' + (a.notified_at ? icon('telegram') + ' ' + t('al.sent_tg') : a.notify_failed ? t('al.desktop') : '') + '</span><span class="grow"></span>' +
      (a.status === 'open' ? '<button class="btn btn-ghost btn-sm" data-action="alResolve" data-id="' + a.id + '">' + t('al.markResolved') + '</button>' : '') +
      (a.link ? '<button class="btn btn-secondary btn-sm" data-action="alGo" data-link="' + esc(a.link) + '">' + t('al.go') + icon('arrowRight') + '</button>' : '') + '</div></div></div></div>';
    U.openDrawer('<div class="drawer-header"><div class="risk-icon" style="background:var(--color-primary-soft);color:var(--color-primary)">' + icon('bell', 'lg') + '</div><div class="grow"><h3>' + t('al.title') + '</h3><div class="small text-2">' + t('al2.sub') + '</div></div><button class="btn btn-ghost btn-icon btn-sm" data-action="closeDrawer">' + icon('x') + '</button></div>' +
      '<div class="drawer-body"><div class="row" style="margin-bottom:14px"><div class="seg">' + ['open', 'resolved', 'all'].map((k) => '<button class="' + (f === k ? 'active' : '') + '" data-action="alFilter" data-k="' + k + '">' + (k === 'all' ? t('c.all') : k === 'open' ? t('al.open') : t('al.resolved')) + '</button>').join('') + '</div><span class="grow"></span>' +
      (f === 'open' && d.items.length > 1 ? '<button class="btn btn-ghost btn-sm" data-action="alResolveAll">' + icon('check') + t('al2.resolveAll') + '</button>' : '') +
      '<button class="btn btn-ghost btn-sm" data-action="alSettings">' + icon('settings') + t('al.settings') + '</button></div>' +
      (d.items.length ? ['urgent', 'general', 'info'].map((lv) => { const g = d.items.filter((a) => a.level === lv); return g.length ? '<div class="section-title" style="margin:10px 0 8px;color:' + (lv === 'urgent' ? 'var(--color-danger)' : lv === 'general' ? 'var(--color-warning)' : 'var(--color-info)') + '">' + icon(LV[lv][1]) + t('al.level.' + lv) + (lv === 'urgent' ? ' · ' + t('al.urgent') : lv === 'general' ? ' · ' + t('al.normal') : '') + '</div>' + g.map(card).join('') : ''; }).join('')
        : empty({ icon: 'checkCircle', title: t('al.empty'), body: t('al.emptySub') })) +
      (notices.length ? '<div class="section-title" style="margin:16px 0 8px">' + icon('info') + t('al2.notices') + '</div>' + notices.map(card).join('') : '') + '</div>');
    try { await api('POST', '/api/alerts/read', {}); U.refreshBadges(); } catch (e) { /* ignore */ }
  };
  A.alFilter = (el) => { S.alertFilter = el.dataset.k; A.openAlerts(); };
  A.alResolveAll = async (el) => { const r = await busy(el, () => api('POST', '/api/alerts/resolve-all')); toast(t('al2.resolvedAll', { n: r.resolved })); A.openAlerts(); U.refreshBadges(); };
  A.alResolve = async (el) => { await api('POST', '/api/alerts/' + el.dataset.id + '/resolve'); toast(t('al.resolvedToast')); A.openAlerts(); };
  A.alGo = (el) => { U.closeDrawer(); location.hash = el.dataset.link; };
  A.alSettings = () => { U.closeDrawer(); U.go('settings'); };
})();

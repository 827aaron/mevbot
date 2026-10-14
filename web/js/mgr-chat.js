/* =====================================================================
   和销售主管的对话（全局唯一的一段对话；按 design/prototype/mgr-chat.js 移植，接真实接口）
   - 工作台：占页面主要高度；其他页面：右侧可收起的“主管”面板（#mgr-panel），内容相同
   - 输入 @调研员 / @Scout 切换为和某个 AI 单独对话（只用它自己的工作数据）
   - 后端消息格式 {id, from, time, text|key+params, card?, memo?, actions[]}；
     每个行内动作的 act 对应这里的一个处理函数：go（跳转）/ chatAct（交给后端执行：撤销、确认备忘录…）/ openLead
   ===================================================================== */
(function () {
  'use strict';
  const S = window.STATE, U = window.UI, A = window.ACTIONS;
  const { esc } = U;
  const AGENT_KEYS = { filter: (f) => U.agentKeys().filter(f), find: (f) => U.agentKeys().find(f) };   // v1 不含销售员 / 跟进员
  S.chats = {};               // agent -> {msgs, last, loaded, sending}
  S.chatTarget = 'manager';

  function box(key) { return (S.chats[key] = S.chats[key] || { msgs: [], last: 0, loaded: false, sending: false, clearing: false, epoch: 0 }); }
  function merge(key, list) {
    const b = box(key);
    (list || []).forEach((m) => {
      if (!m) return;
      const i = b.msgs.findIndex((x) => x.id === m.id);
      if (i >= 0) b.msgs[i] = m; else b.msgs.push(m);
      b.last = Math.max(b.last, m.id || 0);
    });
    b.msgs.sort((x, y) => (x.id || 1e15) - (y.id || 1e15));
  }
  async function load(key, force) {
    const b = box(key);
    if (b.clearing || (b.loaded && !force)) return;
    const epoch = b.epoch;
    const r = await U.api('GET', '/api/chat/' + key);
    if (epoch !== b.epoch) return;
    b.msgs = []; b.last = 0; merge(key, r.messages); b.loaded = true;
  }
  async function poll() {
    const key = S.chatTarget || 'manager', b = box(key);
    if (!document.querySelector('[data-mgr-chat]') || b.sending || b.clearing || !b.loaded) return;
    const epoch = b.epoch;
    try {
      const r = await U.api('GET', '/api/chat/' + key + '?after=' + b.last);
      if (epoch !== b.epoch) return;
      if (r.messages.length) { merge(key, r.messages); renderChats(); seen(key); }
    } catch (e) { /* 网络抖动：下次再取 */ }
  }
  function seen(key) { U.api('POST', '/api/chat/' + key + '/seen').then(() => { if (key === 'manager') { S.badges.chat = 0; U.renderTop(); } }).catch(() => {}); }

  /* ---------- 渲染 ---------- */
  function msgText(m) { return m.key ? t(m.key, m.params || {}) : esc(m.text); }
  function messageTools(m, key) {
    const text = m.key ? t(m.key, m.params || {}).replace(/<[^>]+>/g, '') : String(m.text || '');
    return '<div class="msg-tools"><button class="btn btn-ghost btn-sm" data-action="chatCopy" data-copy-text="' + esc(text) + '">' + icon('copy') + t('c.copy') + '</button>' +
      (m.id > 0 && !m.pending ? '<button class="btn btn-ghost btn-sm" data-action="chatDelete" data-k="' + esc(key) + '" data-id="' + m.id + '"' + (box(key).clearing ? ' disabled' : '') + '>' + icon('trash') + t('chat.deleteMessage') + '</button>' : '') + '</div>';
  }
  function actionBtn(a) {
    const label = t(a.k, a.p || {});
    if (a.act === 'chatAct' && a.done) return '<span class="tag tag-outline">' + icon('check') + esc(a.k === 'nf.undo' ? t('chat.undone') : label) + '</span>';
    return '<button class="link-btn" data-action="' + a.act + '" data-route="' + esc(a.route || '') + '" data-id="' + esc(a.id == null ? '' : a.id) + '">【' + label + '】</button>';
  }
  function memoCard(m) {
    const x = m.memo;
    const done = x.status && x.status !== 'pending';
    return '<div class="memo-card' + (done ? ' is-done' : '') + '">' + icon('edit') + '<div class="grow"><div class="xs muted">' + t('chat.memoTitle') + '</div><b>' + esc(x.text) + '</b>' +
      (!done ? '<div class="row" style="margin-top:6px"><button class="btn btn-primary btn-sm" data-action="chatAct" data-id="' + x.ok + '">' + icon('check') + t('mem.confirm') + '</button>' +
        (x.no ? '<button class="btn btn-ghost btn-sm" data-action="chatAct" data-id="' + x.no + '">' + t('mem.noNeed') + '</button>' : '') + '</div>'
        : '<div class="xs" style="margin-top:4px;color:' + (x.status === 'confirmed' ? 'var(--color-success)' : 'var(--color-text-3)') + '">' + icon(x.status === 'confirmed' ? 'checkCircle' : 'x') + ' ' + t(x.status === 'confirmed' ? 'mem.activeNow' : 'mem.dropped') + '</div>') + '</div></div>';
  }
  function msgHtml(m, key) {
    if (m.thinking) return '<div class="msg" data-agent="' + key + '">' + U.avatar(key, 'sm') + '<div><div class="bubble"><span class="thinking" data-agent="' + key + '"><i></i><i></i><i></i></span> <span class="xs muted">' + t('chat.thinking') + '</span></div></div></div>';
    if (m.from === 'me') return '<div class="msg me"><div><div class="who">' + t('agent.client') + ' · ' + U.hhmm(m.time) + '</div><div class="bubble">' + U.para(m.text) + '</div>' + messageTools(m, key) + '</div></div>';
    const card = m.card === 'memo' && m.memo ? memoCard(m) : '';
    const acts = m.actions || [];
    return '<div class="msg" data-agent="' + key + '">' + U.avatar(key, 'sm') + '<div><div class="who">' + esc(t('agent.' + key)) + ' · ' + U.hhmm(m.time) + '</div>' +
      '<div class="bubble">' + (m.key ? msgText(m) : U.para(m.text)) + (acts.length && !card ? ' ' + acts.map(actionBtn).join('') : '') + card +
      (acts.length && card ? '<div class="row wrap" style="margin-top:6px;gap:2px">' + acts.map(actionBtn).join('') + '</div>' : '') + '</div>' + messageTools(m, key) + '</div></div>';
  }
  const SUGGEST = ['mc2.s1', 'mc2.s2', 'mc2.s3', 'mc2.s4'];
  function chatInner() {
    const tg = S.chatTarget || 'manager', solo = tg !== 'manager', b = box(tg);
    const ag = U.agentState(tg);
    const off = !ag.enabled || ag.status === 'off';
    let list = b.msgs.slice();
    if (!list.length && b.loaded) list = [{ from: 'agent', id: 0, text: solo ? t('mc.agentIntro', { name: t('agent.' + tg) }) : t('mc2.intro'), time: Date.now() / 1000 }];
    if (b.sending) list.push({ thinking: true });
    return (solo ? '<div class="solo-bar" data-agent="' + tg + '">' + U.avatar(tg, 'sm') + '<b class="small">' + esc(t('mc.talkingTo', { name: t('agent.' + tg) })) + '</b><span class="grow"></span><button class="btn btn-ghost btn-sm" data-action="mgrTarget" data-k="manager">' + icon('chevronLeft') + t('mc.back') + '</button></div>' : '') +
      '<div class="row wrap" style="padding:6px 12px;justify-content:flex-end"><button class="btn btn-ghost btn-sm" data-action="mgrClear" data-k="' + tg + '"' + (b.clearing ? ' disabled' : '') + '>' + icon('trash') + t('chat.clear') + '</button><button class="btn btn-ghost btn-sm" data-action="mgrForget" data-k="' + tg + '"' + (b.clearing ? ' disabled' : '') + '>' + icon('brain') + t('chat.forget') + '</button></div>' +
      '<div class="chat-scroll mgr-scroll">' + (b.loaded ? list.map((m) => msgHtml(m, tg)).join('') : '<div class="row small muted" style="padding:20px"><span class="spinner"></span>' + t('c.loading') + '</div>') + '</div>' +
      '<div class="suggestions"><span class="xs muted" style="align-self:center">' + t('mc.mention') + '</span>' + AGENT_KEYS.filter((k) => k !== 'manager').map((k) => '<span class="chip chip-sm' + (tg === k ? ' active' : '') + '" data-action="mgrTarget" data-k="' + k + '" data-agent="' + k + '">' + U.avatar(k, 'sm') + esc(t('agent.' + k)) + '</span>').join('') +
      (solo ? '' : '<span class="sugg-sep"></span>' + SUGGEST.map((k) => '<span class="chip chip-sm sugg-phrase" data-action="mgrSuggest" data-k="' + k + '">' + esc(t(k)) + '</span>').join('')) + '</div>' +
      (off ? '<div class="chat-input"><div class="alert alert-warning grow">' + icon('info') + '<span>' + t('chat.offNotice', { name: t('agent.' + tg) }) + '</span></div></div>'
        : '<div class="chat-input"><textarea class="textarea mgr-input" placeholder="' + esc(solo ? t('chat.placeholder', { name: t('agent.' + tg) }) : t('mc2.placeholder')) + '">' + esc(S.mgrDraft || '') + '</textarea>' +
          (solo ? '' : '<button class="btn btn-secondary" data-action="opsCommand" title="' + esc(t('ops.commandHint')) + '">' + t('ops.command') + '</button>') +
          '<button class="btn btn-primary" data-action="mgrSend">' + icon('send') + t('chat.send') + '</button></div>');
  }
  /* 页面里需要放主管对话的地方写：<div class="mgr-chat-wrap" data-mgr-chat>…</div> */
  function chatBlock() { return '<div class="mgr-chat-wrap" data-mgr-chat>' + chatInner() + '</div>'; }
  function bind(root) {
    (root || document).querySelectorAll('[data-mgr-chat]').forEach((w) => {
      const sc = w.querySelector('.mgr-scroll'); if (sc) sc.scrollTop = sc.scrollHeight;
      const ta = w.querySelector('.mgr-input');
      if (ta && !ta._bound) {
        ta._bound = true;
        ta.addEventListener('input', () => { S.mgrDraft = ta.value; });
        ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(ta.value); } });
      }
    });
  }
  function renderChats() {
    document.querySelectorAll('[data-mgr-chat]').forEach((w) => { w.innerHTML = chatInner(); });
    bind(document);
  }
  async function ensure() {
    const key = S.chatTarget || 'manager';
    if (!box(key).loaded) { try { await load(key); } catch (e) { U.toastErr(e); return; } renderChats(); seen(key); }
  }

  /* ---------- 右侧“主管”面板（工作台以外的页面） ---------- */
  function panelVisible() {
    const page = window.PAGES[S.route];
    return S.mgrOpen && S.route !== 'dashboard' && !(page && page.fullscreen);
  }
  function renderPanel() {
    const el = document.getElementById('mgr-panel');
    if (!el) return;
    const vis = panelVisible();
    el.classList.toggle('hidden', !vis);
    document.body.classList.toggle('has-mgr-panel', vis && window.innerWidth >= 1360);
    if (!vis) { el.innerHTML = ''; return; }
    el.innerHTML = '<div class="mgr-panel-head" data-agent="manager">' + U.avatar('manager', '', { presence: true }) + '<div class="grow"><b>' + t('agent.manager') + '</b> <span class="agent-nick">Atlas</span><div class="xs muted">' + t('mc.sameChat') + '</div></div>' +
      '<button class="btn btn-ghost btn-icon btn-sm" data-action="toggleMgr" title="' + esc(t('mc.collapse')) + '">' + icon('panelRight') + '</button></div>' + chatBlock();
    bind(el);
    ensure();
  }

  /* ---------- 发送 ---------- */
  function parseMention(q) {
    const m = q.match(/^[@＠]\s*(\S+)\s*([\s\S]*)$/); if (!m) return null;
    const name = m[1].toLowerCase().replace(/\s/g, '');
    const hit = AGENT_KEYS.find((k) => [I18N.dict.zh['agent.' + k], I18N.dict.en['agent.' + k], U.agentNick(k), k].some((n) => n && n.toLowerCase().replace(/\s/g, '') === name));
    return hit ? { key: hit, rest: m[2].trim() } : null;
  }
  function pageCtx() {
    const ctx = { route: S.route };
    const lid = (S.params && (S.params.lead || S.params.open)) || (document.querySelector('#drawer [data-current-lead]') || {}).dataset?.currentLead;
    if (lid) ctx.lead_id = +lid;
    return ctx;
  }
  async function send(text) {
    let q = String(text || '').trim(); if (!q) return;
    const mention = parseMention(q);
    if (mention) { S.chatTarget = mention.key; q = mention.rest; await ensure(); if (!q) { S.mgrDraft = ''; renderChats(); focusInput(); return; } }
    const key = S.chatTarget || 'manager', b = box(key);
    if (b.sending || b.clearing) return;
    const epoch = b.epoch;
    S.mgrDraft = '';
    b.msgs.push({ id: 0, from: 'me', text: q, time: Date.now() / 1000, pending: true }); b.sending = true; renderChats();
    try {
      const r = await U.api('POST', '/api/chat/' + key, { message: q, page: pageCtx() });
      if (epoch !== b.epoch) return;
      b.msgs = b.msgs.filter((x) => !x.pending); merge(key, [r.user_message, r.message]);
    } catch (e) { if (epoch !== b.epoch) return; b.msgs = b.msgs.filter((x) => !x.pending); S.mgrDraft = q; U.toastErr(e); }
    b.sending = false; renderChats(); seen(key); U.refreshBadges();
  }

  async function clearDialog(key, forget) {
    const all = key === 'all';
    const keys = all ? [...new Set(U.agentKeys().concat(Object.keys(S.chats)))] : [key];
    if (keys.some((k) => box(k).clearing)) return false;
    const accepted = await U.confirmDialog({
      title: t(all ? 'chat.forgetAll' : forget ? 'chat.forget' : 'chat.clear'),
      body: t(all ? 'chat.forgetAllBody' : forget ? 'chat.forgetBody' : 'chat.clearBody', { name: t('agent.' + key) }),
      ok: t('c.delete'), danger: true,
      onOk: async () => {
        keys.forEach((k) => { const b = box(k); b.clearing = true; b.epoch++; });
        renderChats();
        let cleared = false;
        try {
          await U.api('DELETE', (forget ? '/api/memory/' : '/api/chat/') + key);
          keys.forEach((k) => { const b = box(k); b.msgs = []; b.last = 0; b.loaded = true; });
          if (keys.includes(S.chatTarget || 'manager')) S.mgrDraft = '';
          if (keys.includes('manager')) {
            S.badges.chat = 0;
            A.closeManagerDock();
          }
          cleared = true;
        } finally {
          keys.forEach((k) => { const b = box(k); b.clearing = false; b.sending = false; });
          // Recover persisted history if deletion failed; never resurrect an
          // obsolete pending request in the client.
          if (!cleared) await Promise.all(keys.map((k) => load(k, true).catch(() => { box(k).loaded = false; })));
          renderChats(); U.refreshBadges(); U.renderTop();
        }
      },
    });
    if (accepted) {
      U.toast(t('chat.cleared'));
      if (S.route === 'learning') U.refresh();
    }
    return accepted;
  }

  /* Feedback stays visible without forcing the manager panel over the user's work. */
  async function managerUpdated() {
    try { await load('manager', true); } catch (e) { return; }
    if (S.chatTarget !== 'manager') S.chatTarget = 'manager';
    renderPanel(); renderChats();
    if (S.route === 'dashboard' || S.mgrOpen) seen('manager');
    else {
      const previous = document.querySelector('.mgr-dock'); if (previous) previous.remove();
      const msg = box('manager').msgs.filter((m) => m.from !== 'me').slice(-1)[0];
      if (msg) {
        const dock = document.createElement('div'); dock.className = 'mgr-dock'; dock.dataset.agent = 'manager';
        dock.innerHTML = U.avatar('manager', 'sm') + '<div class="grow"><div class="who">' + t('agent.manager') + '</div><div class="txt">' + msgText(msg) + '</div>' +
          (msg.actions || []).map(actionBtn).join('') + '<button class="link-btn" data-action="toggleMgr">' + t('company.openManager') + '</button></div><button class="btn btn-ghost btn-icon btn-sm" data-action="closeManagerDock" title="' + esc(t('c.close')) + '">' + icon('x') + '</button>';
        document.body.appendChild(dock); setTimeout(() => dock.remove(), 9000);
      }
      U.refreshBadges();
    }
    U.renderTop();
  }
  A.closeManagerDock = () => { const dock = document.querySelector('.mgr-dock'); if (dock) dock.remove(); };
  let narrowPanel = window.innerWidth < 1360;
  window.addEventListener('resize', () => {
    const narrow = window.innerWidth < 1360;
    if (narrow === narrowPanel) return;
    narrowPanel = narrow;
    if (narrow) S.mgrOpen = false;
    renderPanel(); U.renderTop();
  });

  /* ---------- 行内动作（每个 act 一个处理函数） ---------- */
  A.mgrSend = (el) => { const w = el.closest('[data-mgr-chat]'); send(w.querySelector('.mgr-input').value); };
  A.mgrClear = (el) => clearDialog(el.dataset.k, false);
  A.mgrForget = (el) => clearDialog(el.dataset.k, true);
  A.chatCopy = async (el) => {
    const text = el.dataset.copyText || '';
    try {
      try {
        if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('clipboard unavailable');
        await navigator.clipboard.writeText(text);
      } catch (err) {
        // WKWebView may not expose the Clipboard API. Native text selection
        // and the browser's copy command remain available.
        const active = document.activeElement, selection = window.getSelection();
        const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
        const ta = document.createElement('textarea'); ta.value = text; ta.readOnly = true;
        ta.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.appendChild(ta);
        try { ta.select(); if (!document.execCommand('copy')) throw new Error('copy failed'); }
        finally {
          ta.remove();
          if (active && active.isConnected && active.focus) active.focus();
          if (selection) { selection.removeAllRanges(); ranges.forEach((range) => selection.addRange(range)); }
        }
      }
      U.toast(t('c.copied'));
    } catch (err) { U.toast(t('chat.copyFailed'), 'error'); }
  };
  A.chatDelete = async (el) => {
    const key = el.dataset.k, id = +el.dataset.id, b = box(key);
    if (b.clearing || !id) return;
    const accepted = await U.confirmDialog({ title: t('chat.deleteMessage'), body: t('chat.deleteMessageBody'), ok: t('c.delete'), danger: true,
      onOk: async () => {
        b.clearing = true; b.epoch++; renderChats();
        let deleted = false;
        try {
          await U.api('DELETE', '/api/chat/' + key + '/messages/' + id);
          b.msgs = b.msgs.filter((m) => m.id !== id && !m.pending);
          deleted = true;
        } finally {
          b.clearing = false; b.sending = false;
          if (!deleted) await load(key, true).catch(() => { b.loaded = false; });
          renderChats(); U.refreshBadges();
        }
      },
    });
    if (accepted) U.toast(t('common.deleted'));
  };
  A.mgrSuggest = (el) => send(t(el.dataset.k));
  A.mgrTarget = async (el) => { S.chatTarget = el.dataset.k; S.mgrDraft = ''; renderChats(); await ensure(); renderChats(); setTimeout(focusInput, 20); };
  A.chatAct = async (el) => {
    const key = S.chatTarget || 'manager', b = box(key), epoch = b.epoch;
    if (b.clearing) return;
    const [mid, idx] = String(el.dataset.id).split(':').map(Number);
    const r = await U.busy(el, () => U.api('POST', '/api/chat/action/' + mid + '/' + idx));
    if (epoch !== b.epoch) return;
    merge(key, [r.message]);
    const res = r.result || {};
    if (res.goto) { U.go(res.goto, res.lead_id ? { lead: res.lead_id } : null); return; }
    await poll(); renderChats();
    if (res.lead_id) { if (S.reviewDone) delete S.reviewDone[res.lead_id]; U.toast(t('nf.undone')); if (['review', 'leads', 'dashboard'].includes(S.route)) U.refresh(); }
    U.refreshBadges();
  };
  A.openLead = (el) => window.openLead && window.openLead(+el.dataset.id);
  A.toggleMgr = () => { S.mgrOpen = !S.mgrOpen; U.savePrefs(); renderPanel(); U.renderTop(); };
  A.newTask = () => { if (S.route !== 'dashboard') U.go('dashboard'); setTimeout(focusInput, 60); };
  function focusInput() { const ta = document.querySelector('[data-mgr-chat] .mgr-input'); if (ta) ta.focus(); }
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (S.route !== 'dashboard' && !S.mgrOpen) A.toggleMgr();
      focusInput();
    }
  });
  setInterval(poll, 4000);

  window.MGR = { chatBlock, chatInner, bind, renderChats, renderPanel, send, ensure, managerUpdated, load, clearDialog };
})();

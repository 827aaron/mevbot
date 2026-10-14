/* 启动：偏好 → 文案 → 概况 → 语言/主题 → 路由；之后每 15 秒刷新角标（审核 / 记忆待确认 / 提醒 / 主管未读） */
(async function () {
  'use strict';
  const S = window.STATE, U = window.UI;
  U.loadPrefs();
  try {
    await U.loadDicts();
    S.meta = await U.api('GET', '/api/meta');
    for (const lang of ['zh', 'en']) {
      for (const key of ['name', 'tagline']) {
        const value = S.meta.app && S.meta.app[key];
        if (value && typeof value[lang] === 'string') I18N.dict[lang]['app.' + key] = value[lang];
      }
    }
  } catch (e) {
    document.body.innerHTML = '<div style="padding:40px;font:16px sans-serif">无法连接到本地服务，请重新双击“启动”。<br>Cannot reach the local service. Please start the app again.</div>';
    return;
  }
  if (S.meta.edition && S.meta.edition.id === 'my_igaming') {
    const agencyLabels = {
      'tasks.target': ['代理线索目标', 'Agency lead target'], 'tasks.target_hint': ['按公开代理业务特征查找待核实线索', 'Find leads to verify from public agency-business signals'], 'nq.no_contact': ['联系方式待补充', 'Contacts to be completed'], 'leads.backup': ['待核实线索', 'Leads to verify'],
      'dash.qualified': ['代理线索', 'Agency leads'], 'kpi.qualified': ['代理线索', 'Agency leads'], 'run.f.qualified': ['代理线索', 'Agency leads'],
      'lead.qualified': ['待核实代理线索', 'Agency lead to verify'], 'lead.backup': ['待核实代理线索', 'Agency lead to verify'], 'lead.qualified_sub': ['身份、地区和联系方式仍待核实', 'Identity, region, and contacts remain to be verified'],
      'lstatus.qualified': ['待核实代理', 'Agency lead to verify'], 'lstatus.backup': ['待核实代理', 'Agency lead to verify'],
      'leads.company': ['代理名称', 'Agent name'], 'leads.country': ['地区标记', 'Region label'], 'leads2.sub': ['共 {total} 条代理线索 · 业务与联系方式待核实', '{total} agency leads · business and contacts to verify'],
      'pool.zone.qualified': ['代理线索', 'Agency leads'], 'pool.today_q': ['今日代理线索', 'Agency leads today'], 'pool.rate': ['线索入库比例', 'Lead-entry ratio'],
      'ch.qualified': ['代理线索', 'Agency leads'],
      'scout.title': ['主管每日扩 TG 监控池', 'Manager daily TG monitoring-pool expansion'], 'scout.sub': ['每日按公开代理业务特征发现候选群、扩充监控池并持续观察代理线索。推广注册链接与邀请码仅保留为来源证据，仍待核实。', 'Discover candidate groups daily using public agency-business signals, expand the monitoring pool, and observe agency leads over time. Promotional registration links and invitation codes are retained as source evidence to verify.'],
      'scout.enabled': ['启用每日扩池', 'Enable daily pool expansion'], 'scout.autoQueue': ['候选群自动进入加群队列', 'Automatically queue candidate groups to join'], 'scout.queueTitle': ['确认候选群自动加群排队', 'Confirm automatic join queuing for candidates'], 'scout.queueHint': ['代理业务特征命中后可入队；低分和广告特征不作硬性排除。实际加入遵守账号限制，排队不代表已加入或开启监控。', 'Groups with agency-business signals can be queued. Low scores and advertising signals do not cause automatic exclusion. Joining follows account limits; queuing does not mean joined or monitored.'],
      'grp.adFlag': ['推广特征', 'Promotional signal'],
      'ops.progress': ['代理线索 {done} / 目标 {target}', 'Agency leads {done} / goal {target}'],
    };
    Object.entries(agencyLabels).forEach(([key, values]) => ['zh', 'en'].forEach((lang, i) => { I18N.dict[lang][key] = values[i]; }));
  }
  let lang = S.meta.ui_lang;
  try { if (!lang) lang = localStorage.getItem('ja-lang'); } catch (e) { /* ignore */ }
  I18N.setLang(lang === 'en' ? 'en' : 'zh');
  U.applyTheme();
  applyI18n(document);
  /* 离开前提醒未保存（U36）：页面设置 S.leaveGuard = () => 有没有没保存的改动 */
  let lastHash = location.hash, bypass = false;
  window.addEventListener('hashchange', async () => {
    if (!bypass && S.leaveGuard && S.leaveGuard()) {
      const target = location.hash;
      history.replaceState(null, '', lastHash);
      if (!(await U.confirmDialog({ title: t('c.unsavedTitle'), body: t('c.unsavedBody'), ok: t('c.leaveAnyway') }))) return;
      bypass = true; location.hash = target; return;
    }
    bypass = false; S.leaveGuard = null; lastHash = location.hash; U.render();
  });
  await U.refreshBadges();
  U.render();
  setInterval(U.refreshBadges, 15000);
  // 浏览器组件下载中：每 3 秒更新一次进度（第 5a 步）
  setInterval(async () => { const S = window.STATE; if (S.browser && S.browser.state === 'downloading') { try { S.browser = await U.api('GET', '/api/browser'); U.renderNav(); } catch (e) { /* ignore */ } } }, 3000);
  try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) { /* ignore */ }
})();

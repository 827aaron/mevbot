/* 启动：偏好 → 文案 → 概况 → 语言/主题 → 路由；之后每 15 秒刷新角标（审核 / 记忆待确认 / 提醒 / 主管未读） */
(async function () {
  'use strict';
  const S = window.STATE, U = window.UI;
  U.loadPrefs();
  try {
    await U.loadDicts();
    S.meta = await U.api('GET', '/api/meta');
  } catch (e) {
    document.body.innerHTML = '<div style="padding:40px;font:16px sans-serif">无法连接到本地服务，请重新双击“启动”。<br>Cannot reach the local service. Please start the app again.</div>';
    return;
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

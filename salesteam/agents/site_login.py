"""官网登录后只读（PRD 3.0）：会员区、经销商专区、会员价格、下载中心等。

- 客户点「登录官网」→ 弹出真实浏览器窗口（Playwright；优先自带 Chromium，没有则用电脑上的 Chrome / Edge），
  使用独立的浏览器资料目录（data/browser/site-<网站>），**客户自己输入账号密码**、处理验证码
- 软件不读取、不保存密码；客户点“我已登录完成”后只保存登录状态（cookie，加密存本地），关闭窗口
- 随后只读：只发 GET 请求读取同一网站的页面；跳过退出登录、后台管理、账户设置、下单、删除等页面；不点击、不提交任何表单
- 提示客户使用只读账号；读完可一键清除登录状态（删除 cookie 与浏览器资料目录）
- 高风险开关：需要客户先确认风险（默认关闭，开启记录在本机）

第 4 步会把同一套“人工辅助登录”用于 FB / Ins / 领英 / 海关数据网站。
"""
import os
import queue
import shutil
import threading
import time
import urllib.parse

from .. import db, paths, settings, tasks
from ..errors import UserError

_sessions: dict[int, dict] = {}


def _profile_dir(host: str):
    return paths.DATA_DIR / "browser" / f"site-{host.replace(':', '_')}"


def _launch(p, user_dir: str, headless: bool):
    """优先自带 Chromium；没有就用电脑上安装的 Chrome / Edge。"""
    errors = []
    for channel in (None, "chrome", "msedge"):
        try:
            kw = {"channel": channel} if channel else {}
            return p.chromium.launch_persistent_context(user_dir, headless=headless, **kw)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel or 'chromium'}: {str(e)[:80]}")
    raise UserError("browser_missing", detail=" | ".join(errors)[:300])


def open_window(ob_id: int, url: str) -> dict:
    url = (url or "").strip()
    if not url:
        ob = db.query_one("SELECT login FROM onboarding WHERE id=?", (ob_id,))
        det = (db.loads(ob["login"], {}) if ob else {}).get("detected") or []
        url = det[0]["url"] if det else ""
    if not url.startswith(("http://", "https://")):
        raise UserError("relay_bad_url")
    host = urllib.parse.urlparse(url).netloc.lower()
    from .. import browsers
    if not browsers.ready():          # 安装包：浏览器组件还在下载（第一次打开时后台下载）
        st = browsers.ensure_background()
        raise UserError("browser_not_ready", progress=st.get("progress", 0))
    if ob_id in _sessions and _sessions[ob_id]["status"] == "open":
        return status(ob_id)
    headless = bool(os.environ.get("SALESTEAM_SITE_LOGIN_HEADLESS"))   # 仅自动化测试用
    sess = {"status": "starting", "url": url, "host": host, "stop": threading.Event(), "cmds": queue.Queue(),
            "error": "", "cookies": 0}
    _sessions[ob_id] = sess

    def run():
        try:
            from playwright.sync_api import sync_playwright
            d = _profile_dir(host)
            d.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                ctx = _launch(p, str(d), headless)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                sess["status"] = "open"
                while not sess["stop"].is_set():
                    try:
                        fn = sess["cmds"].get(timeout=0.5)   # 仅测试：在浏览器线程里执行（模拟客户操作）
                        fn(page)
                    except queue.Empty:
                        pass
                    if not ctx.pages:                         # 客户把窗口关了
                        break
                    try:
                        cur = ctx.pages[-1].url
                        if cur.startswith("http") and urllib.parse.urlparse(cur).netloc.lower() == host:
                            sess["after_url"] = cur               # 登录后停留的页面（会员区入口）
                    except Exception:  # noqa: BLE001
                        pass
                hn = (urllib.parse.urlparse(url).hostname or "").lower()
                cookies = [c for c in ctx.cookies() if hn.endswith(c.get("domain", "").lstrip(".")) or
                           c.get("domain", "").lstrip(".").endswith(hn.removeprefix("www."))]
                from .. import settings as st
                st.set_secret(f"site_login:{host}", db.dumps([{"name": c["name"], "value": c["value"],
                                                                "domain": c.get("domain", "")} for c in cookies]))
                sess["cookies"] = len(cookies)
                ctx.close()
            sess["status"] = "done"
        except UserError as e:
            sess.update(status="error", error=e.key)
        except Exception as e:  # noqa: BLE001
            sess.update(status="error", error=str(e)[:200])
    t = threading.Thread(target=run, daemon=True, name=f"site-login-{ob_id}")
    sess["thread"] = t
    t.start()
    for _ in range(100):
        if sess["status"] != "starting":
            break
        time.sleep(0.2)
    return status(ob_id)


def status(ob_id: int) -> dict:
    s = _sessions.get(ob_id)
    if not s:
        return {"status": "none"}
    return {k: s[k] for k in ("status", "url", "host", "error", "cookies")}


def run_in_browser(ob_id: int, fn):
    """仅测试使用：在登录窗口里执行操作（模拟客户自己输入）。"""
    _sessions[ob_id]["cmds"].put(fn)


def finish(ob_id: int) -> dict:
    """客户点“我已登录完成”：保存登录状态 → 关闭窗口 → 开始只读读取会员区。"""
    s = _sessions.get(ob_id)
    if not s:
        raise UserError("site_login_not_open")
    s["stop"].set()
    s["thread"].join(timeout=30)
    if s["status"] != "done":
        raise UserError("site_login_failed", detail=s.get("error", ""))
    if not s["cookies"]:
        raise UserError("site_login_no_session")
    t = tasks.create_task({"onboarding_id": ob_id, "member_url": s.get("after_url") or s["url"], "cookie_host": s["host"]},
                          kind="onboarding")
    return {"task_id": t["id"], "cookies": s["cookies"]}


def cookies_for(host: str) -> list[dict]:
    return db.loads(settings.get_secret(f"site_login:{host}") or "[]", [])


def clear(ob_id: int) -> dict:
    s = _sessions.pop(ob_id, None)
    hosts = {s["host"]} if s else set()
    ob = db.query_one("SELECT login FROM onboarding WHERE id=?", (ob_id,))
    if ob:
        h = db.loads(ob["login"], {}).get("host")
        if h:
            hosts.add(h)
    for h in hosts:
        settings.set_secret(f"site_login:{h}", "")
        shutil.rmtree(_profile_dir(h), ignore_errors=True)
    return {"cleared": sorted(hosts)}

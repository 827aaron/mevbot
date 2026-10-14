"""Separate manual platform logins and bounded, read-only rendered pages.

Passwords and challenges are handled by the owner in the platform's browser.
Only encrypted storage state is retained; monitoring never posts or joins.
"""
import threading
from urllib.parse import urlsplit, urlunsplit

from . import background, browsers, db, settings
from .errors import UserError

PLATFORMS = {
    "x": {"hosts": ("x.com", "twitter.com"), "login": "https://x.com/i/flow/login"},
    "reddit": {"hosts": ("reddit.com",), "login": "https://www.reddit.com/login/"},
    "facebook": {"hosts": ("facebook.com",), "login": "https://www.facebook.com/login/"},
    "instagram": {"hosts": ("instagram.com",), "login": "https://www.instagram.com/accounts/login/"},
    "youtube": {"hosts": ("youtube.com", "youtu.be"), "login": "https://accounts.google.com/ServiceLogin?service=youtube"},
}
_sessions = {}
_lock = threading.RLock()
_browser_slot = threading.BoundedSemaphore(1)
_session_versions = {}


def platform_for(url):
    host = (urlsplit(url).hostname or "").lower()
    return next((name for name, cfg in PLATFORMS.items()
                 if any(host == h or host.endswith("." + h) for h in cfg["hosts"])), None)


def canonical_url(url):
    p = urlsplit(url)
    name = platform_for(url)
    if name == "x":
        return urlunsplit((p.scheme, "x.com", p.path.rstrip("/") or "/", "", ""))
    if name == "reddit":
        return urlunsplit((p.scheme, "reddit.com", p.path.rstrip("/") or "/", "", ""))
    return url


def _platform(name):
    if name not in PLATFORMS:
        raise UserError("invalid_value", field="platform")
    return PLATFORMS[name]


def _key(name):
    _platform(name)
    return "social_session:" + name


def status(name):
    _platform(name)
    with _lock:
        s = _sessions.get(name, {})
        return {"id": name, "configured": bool(settings.get_secret(_key(name))),
                "status": s.get("status", "none"), "error": s.get("error", "")}


def status_all():
    return [status(p) for p in PLATFORMS]


def storage_state(name):
    """Session material is never exposed through the status API."""
    return db.loads(settings.get_secret(_key(name)) or "{}", {})


def _launch(p, headless):
    errors = []
    for channel in (None, "chrome", "msedge"):
        try:
            return p.chromium.launch(headless=headless, **({"channel": channel} if channel else {}))
        except Exception:
            errors.append(channel or "chromium")
    raise UserError("browser_missing", detail=", ".join(errors))


def _check_browser():
    if not browsers.ready():
        st = browsers.ensure_background()
        raise UserError("browser_not_ready", progress=st.get("progress", 0))


@background.tracked("social:login")
def open_login(name):
    cfg = _platform(name)
    _check_browser()
    with _lock:
        if _sessions.get(name, {}).get("status") in ("starting", "open"):
            return status(name)
        s = {"status": "starting", "error": "", "stop": threading.Event()}
        _sessions[name] = s

        def run():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = _launch(p, False)
                    try:
                        ctx = browser.new_context(storage_state=storage_state(name) or None)
                        page = ctx.new_page()
                        page.goto(cfg["login"], wait_until="domcontentloaded", timeout=45000)
                        s["status"] = "open"
                        while not s["stop"].wait(0.25):
                            if not ctx.pages:
                                break
                        if s.get("save"):
                            # The login window can traverse the provider's auth
                            # hosts. Filter before persisting: never share them
                            # with another platform or external referral page.
                            state = ctx.storage_state()
                            allowed = cfg["hosts"] + (("google.com",) if name == "youtube" else ())
                            cookies = [c for c in state["cookies"] if any(
                                (c.get("domain") or "").lstrip(".").lower() == h or
                                (c.get("domain") or "").lstrip(".").lower().endswith("." + h) for h in allowed)]
                            origins = [o for o in state.get("origins", []) if platform_for(o.get("origin", "")) == name]
                            if not cookies:
                                raise UserError("site_login_no_session")
                            settings.set_secret(_key(name), db.dumps({"cookies": cookies, "origins": origins}))
                            s["status"] = "done"
                        else:
                            s["status"] = "none"
                    finally:
                        browser.close()
            except UserError as exc:
                s.update(status="error", error=exc.key)
            except Exception:
                s.update(status="error", error="site_login_failed")
        s["thread"] = background.spawn(run, name="social-login-" + name)
    return status(name)


@background.tracked("social:finish_login")
def finish_login(name):
    _platform(name)
    with _lock:
        s = _sessions.get(name)
        if not s or s["status"] != "open":
            raise UserError("site_login_not_open")
        s["save"] = True
        s["stop"].set()
    s["thread"].join(timeout=15)
    if s["status"] != "done":
        raise UserError(s.get("error") or "site_login_failed")
    return status(name)


@background.tracked("social:clear_login")
def clear_session(name):
    _platform(name)
    with _lock:
        s = _sessions.get(name)
        if s and s["status"] in ("starting", "open"):
            s["save"] = False
            s["stop"].set()
            worker = s.get("thread")
        else:
            worker = None
    if worker:
        worker.join(timeout=15)
        if worker.is_alive():
            raise UserError("site_login_failed")
    with _lock:
        _session_versions[name] = _session_versions.get(name, 0) + 1
        settings.set_secret(_key(name), "")
        _sessions.pop(name, None)
    return status(name)


def _login_wall(page, name):
    path = urlsplit(page.url).path.lower()
    if name in ("x", "reddit") and any(segment in path for segment in ("/login", "/register", "/i/flow/", "/account/access")):
        return True
    if (name == "facebook" and ("/login" in path or "checkpoint" in path)) or (
            name == "instagram" and ("/accounts/login" in path or "/challenge" in path)) or (
            name == "youtube" and (urlsplit(page.url).hostname or "").endswith("google.com")):
        return True
    if page.locator('input[type="password"]').count() > 0:
        return True
    if name == "youtube":
        try:
            text = page.locator("body").inner_text(timeout=5000)[:15000].lower()
            return any(s in text for s in ("sign in to confirm your age", "sign in to confirm you’re not a bot",
                                           "sign in to confirm you're not a bot", "登录以确认您的年龄", "登入以確認"))
        except Exception:
            pass
    return False


def extract_units(page, name, limit=30):
    """Each item retains its own author and text; never borrow a group bio."""
    if name in ("x", "reddit"):
        return _public_units(page, name, limit)
    selector = {"facebook": '[role="article"]', "instagram": 'article',
                "youtube": 'ytd-comment-thread-renderer'}[name]
    elements = page.locator(selector)
    count = min(limit, elements.count())
    units = []
    for i in range(count):
        el = elements.nth(i)
        text = el.inner_text(timeout=5000).strip()
        if not text:
            continue
        links = el.locator('a[href]').evaluate_all("els => els.map(e => ({url:e.href,text:e.innerText})).slice(0,40)")
        link = next((l["url"] for l in links if ("lc=" in l["url"] if name == "youtube" else
                     any(x in l["url"] for x in ("/posts/", "/permalink/", "/p/", "/reel/")))), page.url)
        units.append({"url": link, "text": text[:6000], "links": links})
    return units


def _public_units(page, name, limit=30):
    rows = page.evaluate(r"""({name,limit}) => {
        const selector = name === 'x' ? 'article[data-testid="tweet"]' :
          'shreddit-post, shreddit-comment, .thing.link, .thing.comment, [data-testid="post-container"]';
        return Array.from(document.querySelectorAll(selector)).slice(0,limit).map(el => {
          const isX = name === 'x';
          const own = selectors => Array.from(el.querySelectorAll(selectors)).find(n => n.closest(selector) === el) || null;
          const authorNode = own(isX ? '[data-testid="User-Name"] a[href]' : 'a[rel="author"], a.author, [data-testid="comment_author_link"]');
          let author = el.getAttribute('author') || (authorNode && (authorNode.innerText || authorNode.getAttribute('href'))) || '';
          let body = own(isX ? '[data-testid="tweetText"]' : '[slot="text-body"], [slot="comment"], [data-click-id="body"], .usertext-body, .md');
          if (!body) return null;
          // Quoted tweets and nested comments belong to their own authors.
          if (isX && body.closest('[data-testid="quoteTweet"], [role="link"]')) return null;
          const clone = body.cloneNode(true);
          clone.querySelectorAll('shreddit-comment, .thing.comment, [data-testid="quoteTweet"], blockquote').forEach(n => n.remove());
          const time = own(isX ? 'time' : 'a.bylink, a[data-testid="comment_timestamp"]');
          let url = el.getAttribute('permalink') || (time && (isX ? time.closest('a')?.href : time.href)) || '';
          if (!url && !isX) url = own('a[href*="/comments/"]')?.href || '';
          if (!url) return null;
          url = new URL(url,location.href).href;
          const id = el.getAttribute('post-id') || el.getAttribute('comment-id') || el.getAttribute('thingid') || el.id || url;
          const links = Array.from(body.querySelectorAll('a[href]')).filter(a => a.closest(selector) === el && !a.closest('blockquote, [data-testid="quoteTweet"]')).slice(0,40).map(a => ({url:a.href,text:a.innerText || ''}));
          return {id,author,url,text:clone.innerText || clone.textContent || '',links};
        }).filter(Boolean);
    }""", {"name": name, "limit": max(1, min(100, limit))})
    units, seen = [], set()
    for row in rows or []:
        text = str(row.get("text") or "").strip()
        author = str(row.get("author") or "").strip()
        url = canonical_url(str(row.get("url") or ""))
        identity = str(row.get("id") or url)
        if identity.startswith(("http://", "https://")):
            identity = canonical_url(identity)
        if not text or not author or platform_for(url) != name or identity in seen:
            continue
        seen.add(identity)
        units.append({"id": identity, "author": author[:200], "url": url, "text": text[:12000],
                      "links": row.get("links") or [], "platform": name})
    return units


def read(url, access="public", should_stop=lambda: False, max_items=30):
    """Owner-authorized browser GET/navigation and scrolling only; no forms/clicks."""
    name = platform_for(url)
    if not name or access not in ("public", "login"):
        raise UserError("invalid_value", field="social_url")
    state = storage_state(name) if access == "login" else {}
    version = _session_versions.get(name, 0)
    if access == "login" and not state.get("cookies"):
        raise UserError("social_login_required", platform=name)
    _check_browser()
    if not _browser_slot.acquire(timeout=1):
        raise UserError("browser_busy")
    auth_redirect = [False]
    try:
        if should_stop():
            raise UserError("ev.cancelled")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = _launch(p, True)
            try:
                ctx = browser.new_context(storage_state=state or None)
                # No authenticated document may navigate off this platform.
                # Third-party subresources use the browser's cookie isolation.
                def route(req_route):
                    req = req_route.request
                    if req.is_navigation_request() and platform_for(req.url) != name:
                        if name == "youtube" and (urlsplit(req.url).hostname or "") == "accounts.google.com":
                            auth_redirect[0] = True
                        req_route.abort()
                    else:
                        req_route.continue_()
                ctx.route("**/*", route)
                page = ctx.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if response and response.status >= 400:
                    raise UserError("social_access_denied", platform=name, http_status=response.status)
                for _ in range(3):
                    if should_stop():
                        raise UserError("ev.cancelled")
                    if access == "login" and version != _session_versions.get(name, 0):
                        raise UserError("social_login_required", platform=name)
                    if _login_wall(page, name):
                        raise UserError("social_login_required", platform=name)
                    # This loads visible comments/posts without clicking join,
                    # subscribe, reply, like, or a registration link.
                    page.mouse.wheel(0, 1200)
                    page.wait_for_timeout(1000)
                units = extract_units(page, name, max(1, min(100, max_items)))
                if access == "login" and version != _session_versions.get(name, 0):
                    raise UserError("social_login_required", platform=name)
                text = "\n\n".join("Source: " + u["url"] + "\n" + u["text"] for u in units)
                if not units:
                    # No rendered post/comment is an explicit read problem,
                    # never proof of zero agents or a successful monitor run.
                    raise UserError("social_content_unavailable", platform=name)
                return {"url": page.url, "title": page.title(), "markdown": text[:90000],
                        "links": [(l["url"], l["text"]) for u in units for l in u["links"]],
                        "emails": [], "units": units, "platform": name, "access": access}
            finally:
                browser.close()
    except UserError:
        raise
    except Exception:
        raise UserError("social_login_required" if auth_redirect[0] else "social_read_failed", platform=name)
    finally:
        _browser_slot.release()

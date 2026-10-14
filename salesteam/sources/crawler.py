"""网页爬虫（Scrapling）：抓页面 → 转 Markdown 交给 AI 读取，不依赖固定页面结构。

安全默认（PRD 4.1 / 第 9 节）：
- 默认遵守 robots.txt（Protego 解析，Scrapling 自带依赖）
- 同一网站请求最小间隔（默认 3 秒）、每任务/每天页数上限（保守值）
- 隐身模式（PRD 4.1 / 9 / 16）：测试阶段默认开启（正式版默认值测试后再定），在 设置 → 数据来源 开关（不弹风险确认）。
  关闭：遇到任何验证 → 停止这个网站并提示“网站有验证，已停止”。
  开启（按需使用）：平时用普通方式读取；识别到 Cloudflare 这类浏览器检查页时，自动用 Scrapling StealthyFetcher
  重试这一页一次；还是被拦、或遇到图片验证码（如百度 wappass）→ 停止这个网站并记录，继续其他工作，绝不循环、绝不识别验证码
- 需要加载后才显示内容的网站（JS 渲染）：普通读取几乎没有文字时，用 DynamicFetcher（真实浏览器）读一次
- 真实浏览器（隐身 / 动态）同一时间最多 1 个，用完就关（老板电脑内存紧张）
"""
import re
import threading
import time
import urllib.parse

from protego import Protego

from .. import settings
from ..errors import UserError
from . import mockweb


def forum_units(resp, url):
    """Keep forum posts and their authors separate, excluding quoted replies."""
    if not hasattr(resp, "css"):
        return []
    nodes = resp.css('article.message, .post[id], [itemtype*="DiscussionForumPosting"], [data-post-id]')
    units, seen = [], set()
    for node in nodes[:100]:
        attrs = node.attrib
        ident = attrs.get("data-post-id") or attrs.get("id")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        body = node.css('.message-body, .post-body, .post-content, [itemprop="text"]')
        body = body[0] if body else node
        # Quoted statements belong to the quoted author, not this post's author.
        raw = str(body.html_content)
        raw = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", "", raw, flags=re.I | re.S)
        from scrapling.parser import Selector
        clean = Selector(raw)
        text = str(clean.get_all_text(separator="\n", strip=True)).strip()
        if not text:
            continue
        author = node.css('.username, .author, [itemprop="author"]')
        author = str(author[0].get_all_text(separator=" ", strip=True)).strip() if author else ""
        links = [{"url": urllib.parse.urljoin(url, a.attrib.get("href", "")),
                  "text": str(a.get_all_text(separator=" ", strip=True))}
                 for a in clean.css('a[href]') if a.attrib.get("href", "").startswith(("/", "http"))]
        permalink = url.split("#")[0] + "#" + ident
        units.append({"id": ident, "url": permalink, "author": author,
                      "text": ("Author: " + author + "\n" if author else "") + text[:6000], "links": links})
    return units

# 不自己写 User-Agent（0.2.2 文档审查）：自定义的旧版 Chrome UA 和 curl_cffi 模拟的新版浏览器指纹对不上，反而更容易触发验证
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BAD_EMAIL_END = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
MAX_MD_CHARS = 40000
# 明确的人机验证页面标记（只出现在验证页上）：出现就停
CHALLENGE_MARKERS = ("cf-chl", "<title>just a moment...", "attention required! | cloudflare",
                     "complete the following challenge", "bots use duckduckgo", "百度安全验证", "/static/captcha/")
# 弱标记：正常网站也可能有（介绍“短信安全验证”的产品页、防伪验证页、表单里嵌的 reCAPTCHA / hCaptcha / Turnstile…），
# 不能因为一个表单就把客户的整个网站停掉：
# - 文字标记：只看页面上显示的文字，而且页面几乎没有正文（验证页都很短）才算
# - 脚本标记（嵌在网页代码里的验证组件）：只在 403 / 503 时才算
TEXT_MARKERS = ("captcha", "are you a robot", "verify you are human", "access denied", "安全验证", "人机验证", "滑动验证",
                "请完成下方验证", "拖动滑块", "请输入验证码", "访问验证", "异常访问", "unusual traffic",
                "checking your browser", "enable javascript and cookies")
WEAK_MARKERS = TEXT_MARKERS + ("geetest", "hcaptcha", "challenges.cloudflare.com")
CHALLENGE_TITLE = re.compile(r"captcha|安全验证|人机验证|访问验证|security check|are you a robot|verify you are human|"
                             r"just a moment|attention required|access denied|安全检查", re.I)
THIN_PAGE_CHARS = 800          # 去掉标签和脚本后不到这么多字 = “几乎没有正文”
# 跳转后的网址像人机验证 / 拦截页（例如 wappass.baidu.com/static/captcha/…、google.com/sorry/…、/cdn-cgi/challenge…）
CHALLENGE_URL = re.compile(r"captcha|wappass\.|/challenge|cdn-cgi/|/sorry/|geetest|anti[-_]?bot|security[-_]?check|"
                           r"safe[-_]?verify|antispider|/verify(?:[/?._-]|$)|/validate(?:[/?._-]|$)", re.I)


# 图片 / 滑块类验证码：永远直接停（软件不识别验证码，隐身模式也不试）
IMAGE_CAPTCHA = re.compile(r"wappass\.|/static/captcha|captcha\.|geetest|hcaptcha|recaptcha|百度安全验证|滑动验证|拖动滑块|"
                           r"请输入验证码|请完成下方验证|图片验证|/sorry/|unusual traffic", re.I)
# Cloudflare 这类“浏览器检查”页：隐身模式开启时可以用真实浏览器重试一次
BROWSER_CHECK = ("cf-chl", "just a moment", "attention required! | cloudflare", "challenges.cloudflare.com",
                 "cdn-cgi/challenge-platform", "checking your browser", "ddos-guard", "sucuri website firewall",
                 "enable javascript and cookies to continue")

_browser_slot = threading.BoundedSemaphore(1)     # 真实浏览器同一时间只开 1 个（隐身 + 动态共用）


def challenge_kind(reason: str, final_url: str, head: str) -> str:
    """captcha = 图片 / 滑块验证码（直接停）；browser_check = Cloudflare 类检查（可隐身重试一次）；
    rate_limited / blocked = 限流 / 连续 403（直接停）。"""
    if IMAGE_CAPTCHA.search(final_url or "") or IMAGE_CAPTCHA.search(head[:20000]):
        return "captcha"
    if reason == "rate_limited":
        return "rate_limited"
    if reason == "forbidden_burst":
        return "blocked"
    if any(m in head for m in BROWSER_CHECK) or "cdn-cgi/" in (final_url or "") or reason == "captcha_status":
        return "browser_check"
    return "captcha"


def looks_js_rendered(md: str, raw: str) -> bool:
    """普通读取几乎没有文字、页面却是一个 JS 应用的壳（React / Vue / Next / Angular …）。"""
    if len((md or "").strip()) >= 300:
        return False
    r = (raw or "")[:200000].lower()
    if "<script" not in r:
        return False
    return bool(re.search(r"id=[\"'](root|app|__next|__nuxt|svelte|main-app)[\"']|ng-version|data-reactroot|"
                          r"enable javascript|requires javascript|you need to enable", r)) or r.count("<script") >= 5


def site_key(host: str) -> str:
    """同一个网站（含子域名）：取主机名的最后两段（www.baidu.com、wappass.baidu.com → baidu.com）。
    对 .com.cn / .co.uk 这类取最后三段。"""
    h = (host or "").lower().rsplit("@", 1)[-1]
    h = h[1:].split("]")[0] if h.startswith("[") else h.split(":")[0]
    if re.fullmatch(r"[\d.]+", h) or ":" in h:          # IP 地址：整个地址就是一个网站
        return h
    parts = [x for x in h.split(".") if x]
    if len(parts) >= 3 and parts[-2] in ("com", "co", "net", "org", "gov", "edu", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def challenge_reason(requested: str, final_url: str, status: int, head: str) -> str:
    """判断是不是人机验证 / 拦截页：返回原因（空 = 不是）。不绕过，只停下来。"""
    fu = (final_url or "").lower()
    if fu and fu != (requested or "").lower() and CHALLENGE_URL.search(urllib.parse.urlparse(fu).netloc +
                                                                      urllib.parse.urlparse(fu).path):
        return "captcha_redirect"
    if any(m in head for m in CHALLENGE_MARKERS):
        return "captcha_page"
    if status == 429:
        return "rate_limited"
    if status in (403, 503) and any(m in head for m in WEAK_MARKERS):
        return "captcha_status"
    text = _visible_text(head)
    if len(text.replace(" ", "")) < THIN_PAGE_CHARS:
        t = re.search(r"<title[^>]*>(.*?)</title>", head, re.S)
        if t and CHALLENGE_TITLE.search(t.group(1)):
            return "captcha_title"
        if any(m in text for m in TEXT_MARKERS):
            return "captcha_page"
    return ""


def _visible_text(head: str) -> str:
    """页面上显示的文字（去掉脚本、样式、标签，空白合并成一个空格），用来判断“几乎没有正文”和文字标记。"""
    text = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", head)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

_day_lock = threading.Lock()


class Skip(Exception):
    """这一页不抓（robots 不允许 / 超出上限 / 抓取失败），带 i18n key。"""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


def clean_emails(text: str) -> list[str]:
    out = []
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if e.endswith(_BAD_EMAIL_END) or "example.com" in e or e.startswith(("noreply", "no-reply")):
            continue
        if e not in out:
            out.append(e)
    return out


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


class Crawler:
    def __init__(self, mock=False, should_stop=None, max_pages=None, delay=None, respect_robots=None,
                 count_daily=True, cookies=None, keep_raw=False, same_site_only=False):
        self.cfg = dict(settings.get("crawler"))
        if delay is not None:
            self.cfg["delay_seconds"] = float(delay)
        if respect_robots is not None:
            self.cfg["respect_robots"] = bool(respect_robots)
        self.count_daily = count_daily
        self.cookies = cookies          # 登录后只读：客户自己登录得到的 cookie（只发 GET 请求）
        self.keep_raw = keep_raw
        self.same_site_only = same_site_only    # 读官网（快速开始）：跳到别的网站的页面不读；找客户的爬取不限制
        self.mock = mock
        self.should_stop = should_stop or (lambda: False)
        self.max_pages = max_pages or int(self.cfg["max_pages_per_task"])
        self.pages = 0
        self._robots: dict[str, Protego | None] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.blocked: dict[str, str] = {}      # 遇到人机验证的网站（site_key → 原因）：本次不再请求
        self._http403: dict[str, int] = {}
        self._stealth_sites: set[str] = set()  # 隐身重试成功过的网站：后面的页面直接用隐身方式（不再先撞一次验证）
        self._js_sites: set[str] = set()       # JS 渲染的网站：后面的页面直接用真实浏览器读
        self._deferred: dict[str, str] = {}    # 需要隐身重试、但浏览器暂时用不了的网站（不算被拦、不暂停网站）：这一轮先跳过
        self.stats = {"stealth_retries": 0, "stealth_ok": 0, "dynamic": 0}

    # -------------------------------------------------------- 限速 / 配额
    def _count_daily(self):
        with _day_lock:
            today = time.strftime("%Y-%m-%d")
            st = settings.get("crawl_counter", {}) or {}
            if st.get("day") != today:
                st = {"day": today, "n": 0}
            if st["n"] >= int(self.cfg["daily_page_limit"]):
                raise Skip("ev.crawl_daily_limit", limit=self.cfg["daily_page_limit"])
            st["n"] += 1
            settings.put("crawl_counter", st)

    def _wait_turn(self, host: str, delay: float):
        while True:
            with self._lock:
                last = self._last.get(host, 0)
                wait = last + delay - time.time()
                if wait <= 0:
                    self._last[host] = time.time()
                    return
            if self.should_stop():
                raise Skip("ev.cancelled")
            time.sleep(min(wait, 0.5))

    # -------------------------------------------------------- robots.txt
    def _robots_ok(self, url: str) -> tuple[bool, float]:
        if not self.cfg.get("respect_robots", True):
            return True, 0.0
        u = urllib.parse.urlparse(url)
        base = f"{u.scheme}://{u.netloc}"
        if base not in self._robots:
            parser = None
            try:
                from scrapling.fetchers import Fetcher
                r = Fetcher.get(base + "/robots.txt", timeout=10, stealthy_headers=False,
                                retries=1)
                if r.status == 200:
                    parser = Protego.parse(r.body.decode(r.encoding or "utf-8", errors="replace"))
            except Exception:  # noqa: BLE001 —— 拿不到 robots.txt 视为允许
                parser = None
            self._robots[base] = parser
        p = self._robots[base]
        if p is None:
            return True, 0.0
        delay = p.crawl_delay("*")
        return p.can_fetch(url, "*"), float(delay or 0)

    # -------------------------------------------------------- 抓取
    def reserve_rendered(self, url: str, authenticated=False):
        """Reserve a browser read under the same stop, page and pacing limits.

        Public crawling follows robots. An owner's explicitly configured
        logged-in source is an authorized session read, not public indexing.
        """
        if self.should_stop():
            raise Skip("ev.cancelled")
        if self.pages >= self.max_pages:
            raise Skip("ev.crawl_task_limit", limit=self.max_pages)
        if not self.mock:
            host = urllib.parse.urlparse(url).netloc.lower()
            if not authenticated:
                ok, delay = self._robots_ok(url)
                if not ok:
                    raise Skip("ev.robots_blocked", url=url)
            else:
                delay = 0
            if self.count_daily:
                self._count_daily()
            self._wait_turn(host, max(float(self.cfg["delay_seconds"]), delay))
        self.pages += 1

    def fetch(self, url: str) -> dict:
        url = normalize_url(url)
        if not url:
            raise Skip("ev.crawl_failed", url=url, reason="empty")
        if self.should_stop():
            raise Skip("ev.cancelled")
        if self.pages >= self.max_pages:
            raise Skip("ev.crawl_task_limit", limit=self.max_pages)

        if self.mock or mockweb.is_mock_url(url):
            self.pages += 1
            p = mockweb.page(url)
            p["emails"] = clean_emails(p["markdown"]) if not self.mock else \
                [e for e in EMAIL_RE.findall(p["markdown"])]
            from .. import contacts as contacts_mod
            msg = contacts_mod.extract(p["markdown"] + " " + (p.get("raw") or ""), [h for h, _ in p.get("links", [])])
            p.update(whatsapp=msg["whatsapp"], phones=msg["phones"], telegram_links=msg["telegram"])
            return p

        from .. import heal
        host0 = urllib.parse.urlparse(url).netloc.lower()
        if site_key(host0) in self.blocked:      # 这个网站已经出现人机验证：不再请求，绝不循环重试
            raise Skip("ev.crawl_challenge", url=url, reason="blocked_earlier", kind=self.blocked[site_key(host0)],
                       domain=site_key(host0))
        until = heal.domain_paused(host0) or heal.domain_paused(site_key(host0))
        if until:
            raise Skip("ev.domain_paused", domain=host0, minutes=max(1, int((until - time.time()) // 60)),
                       kind=heal.domain_kind(site_key(host0)) or heal.domain_kind(host0))
        ok, robots_delay = self._robots_ok(url)
        if not ok:
            raise Skip("ev.robots_blocked", url=url)
        if self.count_daily:
            self._count_daily()
        host = urllib.parse.urlparse(url).netloc
        self._wait_turn(host, max(float(self.cfg["delay_seconds"]), robots_delay))
        sk = site_key(host0)
        if sk in self._deferred:                 # 这个网站要隐身重试、浏览器暂时用不了：先跳过，之后“重试”再读
            raise Skip("ev.crawl_deferred", url=url, reason=self._deferred[sk], domain=sk)
        self.pages += 1
        stealth_on = bool(self.cfg.get("stealth_mode", True))
        tried_stealth = False
        resp = None
        if stealth_on and sk in self._stealth_sites:
            resp, _ = self._browser_fetch(url, stealth=True)
            tried_stealth = resp is not None
        elif sk in self._js_sites:
            resp, _ = self._browser_fetch(url, stealth=False)
        if resp is None:
            try:
                from scrapling.fetchers import Fetcher
                kw = {"cookies": self.cookies} if self.cookies else {}
                resp = Fetcher.get(url, timeout=int(self.cfg.get("timeout", 20)), stealthy_headers=True,
                                   retries=1, **kw)
            except UserError:
                raise
            except Exception as e:  # noqa: BLE001
                self._fail(host0, type(e).__name__, url)
        why, kind = self._check(url, resp, sk)
        if why and kind == "browser_check" and stealth_on and not tried_stealth:
            # 隐身模式开启：Cloudflare 这类检查页 → 用真实浏览器（隐身，等验证页自己通过）重试这一页一次，不循环
            r2, why_not = self._browser_fetch(url, stealth=True)
            if why_not == "cancelled":
                raise Skip("ev.cancelled")
            if why_not in ("browser_not_ready", "browser_busy", "browser_error", "cookies"):
                # 浏览器还在下载 / 正忙 / 出错，或者是登录后读取（带 cookie，不用浏览器）：不算隐身重试失败，不暂停网站；
                # 这个网站这一轮先跳过（不再请求），之后“重试”再读
                self._deferred[sk] = why_not
                raise Skip("ev.crawl_deferred", url=url, reason=why_not, domain=sk)
            if r2 is not None:
                tried_stealth = True
                self.stats["stealth_retries"] += 1
                why2, kind2 = self._check(url, r2, sk)
                if not why2:
                    resp, why = r2, ""
                    self.stats["stealth_ok"] += 1
                    self._stealth_sites.add(sk)
                else:
                    why, kind = why2, kind2
        if why:
            # 人机验证 / 拦截 / 限流：整个网站（含子域名）立即停止读取并记录，暂停一段时间；其他工作照常
            if kind == "browser_check":
                kind = "browser_check_stealth_failed" if tried_stealth else "browser_check"
            self.blocked[sk] = kind
            heal.domain_failed(sk, "challenge", kind=kind)
            raise Skip("ev.crawl_challenge", url=url, reason=why, kind=kind, domain=sk, stealth=stealth_on)
        if resp.status >= 400:
            if resp.status in (403, 429) or resp.status >= 500:
                self._fail(host0, f"HTTP {resp.status}", url)
            raise Skip("ev.crawl_failed", url=url, reason=f"HTTP {resp.status}")
        final_host = urllib.parse.urlparse(str(resp.url or url)).netloc.lower()
        if self.same_site_only and final_host and site_key(final_host) != sk:
            # 跳到了别的网站：不读（读官网时只读这个公司自己的内容）
            raise Skip("ev.crawl_failed", url=url, reason="offsite_redirect", to=final_host)
        heal.domain_ok(host0)
        md = self._markdown(resp)
        if sk not in self._js_sites and self.cfg.get("render_js", False) and \
                looks_js_rendered(md, resp.body[:200000].decode("utf-8", "ignore") if isinstance(resp.body, bytes) else ""):
            # 需要加载后才显示内容的网站：用真实浏览器（不隐身）读一次
            r3, deferred = self._browser_fetch(url, stealth=False)
            if r3 is None:
                self._deferred[sk] = deferred
                raise Skip("ev.crawl_deferred", url=url, reason=deferred, domain=sk)
            if r3.status >= 400:
                raise Skip("ev.crawl_failed", url=url, reason=f"HTTP {r3.status}")
            render_why, render_kind = self._check(url, r3, sk)
            if render_why:
                raise Skip("ev.crawl_challenge", url=url, reason=render_why, kind=render_kind, domain=sk)
            if r3 is not None and r3.status < 400 and not self._check(url, r3, sk)[0]:
                md3 = self._markdown(r3)
                if len(md3) > len(md):
                    resp, md = r3, md3
                    self._js_sites.add(sk)
                    self.stats["dynamic"] += 1
        return self._to_page(resp, url, md)

    def _to_page(self, resp, url: str, md: str | None = None) -> dict:
        """把抓到的响应整理成页面（标题、正文 markdown、链接、邮箱、电话、TG 链接）。"""
        md = self._markdown(resp) if md is None else md
        title = (resp.css("title::text").get() or "").strip() if hasattr(resp, "css") else ""
        links = []
        try:
            for a in resp.css("a"):
                href = a.attrib.get("href")
                if href and not href.startswith(("javascript:", "#")):
                    links.append((urllib.parse.urljoin(str(resp.url or url), href), (a.text or "").strip()))
        except Exception:  # noqa: BLE001
            pass
        raw = resp.body.decode(resp.encoding or "utf-8", errors="ignore") if isinstance(resp.body, bytes) else ""
        emails = clean_emails(md + " " + " ".join(h for h, _ in links if h.startswith("mailto:")) + " " +
                              " ".join(re.findall(r"mailto:([^\"'?>\s]+)", raw)))
        from .. import contacts as contacts_mod
        msg = contacts_mod.extract(md + " " + re.sub(r"<[^>]+>", " ", raw[:200000]), [h for h, _ in links])
        out = {"url": str(resp.url or url), "title": title, "markdown": md[:MAX_MD_CHARS],
               "truncated": len(md) > MAX_MD_CHARS, "links": links, "emails": emails,
               "whatsapp": msg["whatsapp"], "phones": msg["phones"], "telegram_links": msg["telegram"]}
        units = forum_units(resp, out["url"])
        if units:
            out["units"] = units
        if self.keep_raw:
            out["raw"] = raw[:400000]
        return out

    def stealth_page(self, url: str) -> dict | None:
        """按需隐身读一页（只在隐身模式开启、浏览器可用时；不识别验证码）：用于免费搜索被拦时的一次重试。
        读不了返回 None。"""
        if self.mock or not self.cfg.get("stealth_mode", True):
            return None
        resp, why = self._browser_fetch(url, stealth=True)
        if resp is None or resp.status >= 400:
            return None
        self.stats["stealth_retries"] += 1
        return self._to_page(resp, url)

    def fetch_bytes(self, url: str, max_bytes=20 * 1024 * 1024) -> bytes:
        """下载文件（如网站上的 PDF 产品目录）。同样遵守 robots.txt 和限速，只发 GET。"""
        url = normalize_url(url)
        if self.should_stop():
            raise Skip("ev.cancelled")
        if self.mock or mockweb.is_mock_url(url):
            return mockweb.pdf_bytes(url)
        sk = site_key(urllib.parse.urlparse(url).netloc.lower())
        if sk in self.blocked:                   # 这个网站已经出现人机验证：PDF 也不再下载
            raise Skip("ev.crawl_challenge", url=url, reason="blocked_earlier", kind=self.blocked[sk], domain=sk)
        ok, robots_delay = self._robots_ok(url)
        if not ok:
            raise Skip("ev.robots_blocked", url=url)
        host = urllib.parse.urlparse(url).netloc
        self._wait_turn(host, max(float(self.cfg["delay_seconds"]), robots_delay))
        try:
            from scrapling.fetchers import Fetcher
            kw = {"cookies": self.cookies} if self.cookies else {}
            resp = Fetcher.get(url, timeout=60, stealthy_headers=False, retries=1, **kw)
        except Exception as e:  # noqa: BLE001
            raise Skip("ev.crawl_failed", url=url, reason=type(e).__name__)
        if resp.status >= 400:
            raise Skip("ev.crawl_failed", url=url, reason=f"HTTP {resp.status}")
        body = resp.body if isinstance(resp.body, bytes) else b""
        if len(body) > max_bytes:
            raise Skip("ev.crawl_failed", url=url, reason="too large")
        return body

    def _fail(self, host: str, reason: str, url: str):
        """反复失败：记一次；达到上限就让该网站冷却一段时间（其他工作照常）。"""
        from .. import alerts, heal
        mins = heal.domain_failed(host, reason)
        if mins:
            alerts.raise_alert(f"site_paused:{host}", "general", "site_paused", link="#/tasks", domain=host,
                               minutes=mins, reason=reason)
            raise Skip("ev.domain_cooldown", domain=host, minutes=mins, reason=reason)
        raise Skip("ev.crawl_challenge" if reason == "challenge" else "ev.crawl_failed", url=url, reason=reason)

    def _check(self, url: str, resp, sk: str) -> tuple[str, str]:
        """返回（原因, 类型）；不是验证 / 拦截页时原因为空。"""
        head = (resp.body[:20000] if isinstance(resp.body, bytes) else b"").decode("utf-8", "ignore").lower()
        final = str(resp.url or "")
        why = challenge_reason(url, final, resp.status, head)
        if resp.status == 403:
            self._http403[sk] = self._http403.get(sk, 0) + 1
            if self._http403[sk] >= 2:       # 连续被拒：当作拦截
                why = why or "forbidden_burst"
        else:
            self._http403[sk] = 0
        return (why, challenge_kind(why, final.lower(), head)) if why else ("", "")

    @staticmethod
    def _markdown(resp) -> str:
        try:
            md = resp.markdown(main_content_only=True)
        except Exception:  # noqa: BLE001
            md = resp.get_all_text(separator="\n", strip=True) if hasattr(resp, "get_all_text") else ""
        return re.sub(r"\n{3,}", "\n\n", md or "").strip()

    BROWSER_WAIT_S = 90

    def _browser_fetch(self, url: str, stealth: bool):
        """用真实浏览器读一页（隐身 = StealthyFetcher；否则 DynamicFetcher）。每次开一个、用完就关；
        全软件同一时间最多 1 个。返回 (页面, 没读的原因)：
        cookies = 登录后只读的会员页（只用普通 GET）/ browser_not_ready = 浏览器组件还在下载 /
        browser_busy = 等不到浏览器 / browser_error = 浏览器出错 / cancelled = 任务被停止。"""
        from .. import browsers
        if self.cookies:
            return None, "cookies"
        if not browsers.ready():
            browsers.ensure_background()
            return None, "browser_not_ready"
        waited = 0.0
        while not _browser_slot.acquire(timeout=1.0):      # 短等待循环：任务被停止时马上退出
            waited += 1.0
            if self.should_stop():
                return None, "cancelled"
            if waited >= self.BROWSER_WAIT_S:
                return None, "browser_busy"
        try:
            if self.should_stop():
                return None, "cancelled"
            if stealth:
                # 隐身重试（只在隐身模式开启时）：等 Cloudflare 的“Just a moment”/ Turnstile 验证页自己通过
                # （solve_cloudflare，按文档超时至少 60 秒）；不屏蔽页面资源（否则验证页可能完成不了），屏蔽广告省内存。
                # 图片 / 滑块验证码不处理——之后照样识别为验证页并停止。
                from scrapling.fetchers import StealthyFetcher
                return StealthyFetcher.fetch(url, **self._stealth_kwargs()), ""
            from scrapling.fetchers import DynamicFetcher
            return DynamicFetcher.fetch(url, headless=True, network_idle=False, load_dom=True, wait=1500,
                                        wait_selector="body", disable_resources=True, block_ads=True,
                                        retries=1, timeout=max(30, int(self.cfg.get("timeout", 20))) * 1000), ""
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return None, "browser_error"
        finally:
            _browser_slot.release()

    def _stealth_kwargs(self) -> dict:
        return {"headless": True, "network_idle": True, "solve_cloudflare": True, "disable_resources": False,
                "block_ads": True, "retries": 1, "timeout": max(60, int(self.cfg.get("timeout", 20))) * 1000}


KEY_PAGE = re.compile(r"contact|kontakt|contacto|contatti|about|ueber|über|impressum|imprint|team|"
                      r"empresa|azienda|societe|société|qui-sommes|o-nas|chi-siamo|quienes", re.I)


def key_pages(page: dict, limit=2) -> list[str]:
    """从官网首页找“联系我们/关于我们/Impressum”等页面，用于补全联系方式和公司信息。"""
    host = urllib.parse.urlparse(page["url"]).netloc.removeprefix("www.")
    out = []
    for href, text in page.get("links", []):
        u = urllib.parse.urlparse(href)
        if u.scheme not in ("http", "https") or u.netloc.removeprefix("www.") != host:
            continue
        if KEY_PAGE.search(u.path + " " + text):
            clean = href.split("#")[0]
            if clean not in out and clean.rstrip("/") != page["url"].rstrip("/"):
                out.append(clean)
        if len(out) >= limit:
            break
    return out

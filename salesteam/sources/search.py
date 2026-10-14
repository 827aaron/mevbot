"""网页搜索（“Google 搜索”数据源）的非 Claude 实现。

优先级（provider=auto）：Serper（Google 结果）> Brave Search > 免费兜底（DuckDuckGo Lite / HTML、Bing 结果页，
走爬虫：遵守 robots.txt 和限速；都被拦时按隐身规则重试一次）。Claude 调研员默认直接用 Claude 服务端联网搜索（见 agents/researcher.py）。
客户自备 Key，Key 加密保存在本地。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import settings
from ..errors import UserError
from . import mockweb
from .crawler import Crawler, Skip

TIMEOUT = 20


def _http_json(req: urllib.request.Request, provider: str):
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or (provider == "Brave" and e.code == 422):
            raise UserError("search_key_invalid", provider=provider)
        if e.code == 429:
            raise UserError("search_quota", provider=provider)
        raise UserError("search_failed", provider=provider, detail=f"HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError):
        raise UserError("network_error", provider=provider)


def serper(query, country="", lang="", n=8, key=None):
    key = key or settings.get_secret("serper_api_key")
    body = {"q": query, "num": n}
    if country:
        body["gl"] = country.lower()
    if lang:
        body["hl"] = lang
    req = urllib.request.Request("https://google.serper.dev/search", data=json.dumps(body).encode(),
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    data = _http_json(req, "Serper")
    return [{"url": r.get("link", ""), "title": r.get("title", ""), "snippet": r.get("snippet", "")}
            for r in data.get("organic", [])][:n]


def brave(query, country="", lang="", n=8, key=None):
    key = key or settings.get_secret("brave_api_key")
    params = {"q": query, "count": min(n, 20)}
    if country:
        params["country"] = country.upper()
    if lang:
        params["search_lang"] = lang.split("-")[0]
    req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params),
                                 headers={"X-Subscription-Token": key, "Accept": "application/json"})
    data = _http_json(req, "Brave")
    return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", "")}
            for r in (data.get("web") or {}).get("results", [])][:n]


# 免费入口（PRD §4「免费搜索要抗拦截」）：DuckDuckGo Lite → DuckDuckGo HTML → Bing（都走爬虫：遵守 robots.txt、限速、
# 遇到人机验证就停）；都不行时按隐身规则用真实浏览器重试一次（隐身模式开启时；不识别验证码）。
# 同一个任务里被拦住的入口不再请求（绝不循环撞验证），后面的搜索词直接用还能用的入口。
FREE_ENDPOINTS = ("ddg_lite", "ddg_html", "bing")
DDG_CHALLENGE = ("bots use duckduckgo", "complete the following challenge", "anomaly-modal", "unusual traffic")


def _endpoint_url(name: str, query: str, country: str, lang: str) -> str:
    region = ""
    if country:
        cc = "uk" if country.upper() == "GB" else country.lower()
        region = f"{cc}-{(lang or 'en').split('-')[0]}"
    if name == "bing":
        return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, **({"cc": country.upper()} if country else {})})
    host = "https://lite.duckduckgo.com/lite/" if name == "ddg_lite" else "https://html.duckduckgo.com/html/"
    return host + "?" + urllib.parse.urlencode({"q": query, **({"kl": region} if region else {})})


def _state(crawler) -> dict:
    """这个任务（爬虫）里免费入口的情况：哪些被拦了、隐身重试过没有。"""
    st = getattr(crawler, "_free_search", None)
    if st is None:
        st = crawler._free_search = {"dead": {}, "stealth_tried": False, "stealth_ok": False, "crawlers": {}}
    return st


def _endpoint_fetch(name: str, url: str, crawler) -> dict:
    """抓一个免费入口的结果页（每个入口用自己的爬虫实例：一个入口被拦不影响另一个；测试里替换成假的）。"""
    st = _state(crawler)
    c = st["crawlers"].get(name)
    if c is None:
        c = st["crawlers"][name] = Crawler(should_stop=crawler.should_stop)
    return c.fetch(url)


def _stealth_fetch(url: str, crawler) -> dict | None:
    return crawler.stealth_page(url)


def _challenged(page: dict) -> bool:
    md = (page.get("markdown") or "").lower()
    return any(m in md for m in DDG_CHALLENGE)


def _parse(name: str, page: dict, n: int) -> list[dict]:
    out = []
    for href, text in page.get("links", []):
        if name == "bing":
            host = urllib.parse.urlparse(href).netloc.lower()
            if not host or any(x in host for x in ("bing.com", "microsoft.com", "msn.com", "live.com")) or \
                    not href.startswith("http"):
                continue
            real = href
        else:
            if "uddg=" not in href:
                continue
            real = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0]
            if not real or "duckduckgo.com" in real:
                continue
        if all(r["url"] != real for r in out):
            out.append({"url": real, "title": text, "snippet": ""})
        if len(out) >= n:
            break
    return out


def free(query, country="", lang="", n=8, crawler: Crawler | None = None):
    """免费兜底（没有 Serper / Brave Key 时）。所有入口都被拦住才报 search_blocked（这一个搜索词）。"""
    from scrapling.fetchers import Fetcher  # noqa: F401  确保依赖存在
    crawler = crawler or Crawler()
    st = _state(crawler)
    if st["stealth_ok"]:                       # 前面隐身重试成功过：这个任务里直接用隐身方式
        page = _stealth_fetch(_endpoint_url("ddg_lite", query, country, lang), crawler)
        if page and not _challenged(page):
            return _parse("ddg_lite", page, n)
        st["stealth_ok"] = False
    for name in FREE_ENDPOINTS:
        if name in st["dead"]:
            continue
        try:
            page = _endpoint_fetch(name, _endpoint_url(name, query, country, lang), crawler)
        except Skip as e:
            if e.key in ("ev.cancelled",):
                raise UserError("search_failed", provider="free", detail="cancelled")
            st["dead"][name] = e.key            # 被拦 / 暂停 / robots 不允许 / 读不到：这个任务里不再用这个入口
            continue
        if _challenged(page):
            st["dead"][name] = "challenge"
            continue
        return _parse(name, page, n)
    if not st["stealth_tried"]:                # 都不行：按隐身规则用真实浏览器重试一次（隐身模式关闭时不试）
        st["stealth_tried"] = True
        page = _stealth_fetch(_endpoint_url("ddg_lite", query, country, lang), crawler)
        if page and not _challenged(page):
            st["stealth_ok"] = True
            return _parse("ddg_lite", page, n)
    raise UserError("search_blocked", provider="DuckDuckGo / Bing")


def which_provider() -> str:
    pref = settings.get("search")["provider"]
    if pref in ("serper", "brave", "free"):
        return pref
    if settings.get_secret("serper_api_key"):
        return "serper"
    if settings.get_secret("brave_api_key"):
        return "brave"
    return "free"


def search(query, country="", lang="", n=8, crawler=None, mock_ctx=None) -> tuple[list[dict], str]:
    if mock_ctx is not None:
        return mockweb.search(query, mock_ctx["countries"], mock_ctx["keyword"], n=min(n, 4)), "mock"
    p = which_provider()
    if p == "serper":
        return serper(query, country, lang, n), p
    if p == "brave":
        return brave(query, country, lang, n), p
    return free(query, country, lang, n, crawler=crawler), p


def test(provider: str, key: str | None = None) -> dict:
    fn = {"serper": serper, "brave": brave}.get(provider)
    if fn is None:
        raise UserError("invalid_value", field="provider")
    # 只测 Key 能不能用：搜一个中性的词（不写死任何业务内容）
    res = fn("business directory Malaysia", "MY", "en", 3, key=key)
    return {"results": len(res)}

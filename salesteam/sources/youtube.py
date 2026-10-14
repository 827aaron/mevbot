"""YouTube 数据源：官方 YouTube Data API v3（客户自备 Google API Key）。

按关键词 + 国家(regionCode) + 语言(relevanceLanguage) 搜视频 → 读频道简介、视频简介 → 读评论区识别买家。
配额：search.list 每次 100 单位，免费额度每天 10,000 单位，所以默认每次只搜少量视频。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import settings
from ..errors import UserError
from . import mockweb

API = "https://www.googleapis.com/youtube/v3/"


def _get(path: str, params: dict, key: str):
    url = API + path + "?" + urllib.parse.urlencode({**params, "key": key})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                    timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        reason = ""
        try:
            err = json.loads(e.read().decode("utf-8", "ignore")).get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
        except ValueError:
            pass
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise UserError("youtube_quota")
        if reason == "commentsDisabled":
            return {"items": []}
        if e.code in (400, 401, 403) and reason in ("keyInvalid", "badRequest", "API_KEY_INVALID", "INVALID_ARGUMENT",
                                                    "forbidden", "accessNotConfigured", "PERMISSION_DENIED", ""):
            raise UserError("youtube_key_invalid", detail=reason)
        raise UserError("youtube_failed", detail=f"HTTP {e.code} {reason}")
    except (urllib.error.URLError, OSError, ValueError):
        raise UserError("network_error", provider="YouTube")


def _key(key=None):
    k = key or settings.get_secret("youtube_api_key")
    if not k:
        raise UserError("youtube_key_missing")
    return k


def search_videos(query, country="", lang="", n=5, key=None) -> list[dict]:
    params = {"part": "snippet", "q": query, "type": "video", "maxResults": max(1, min(n, 25)),
              "order": "relevance"}
    if country:
        params["regionCode"] = country.upper()
    if lang:
        params["relevanceLanguage"] = lang.split("-")[0]
    data = _get("search", params, _key(key))
    out = []
    for it in data.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if vid:
            out.append({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                        "title": sn.get("title", ""), "description": sn.get("description", ""),
                        "channel_id": sn.get("channelId", ""), "channel_title": sn.get("channelTitle", "")})
    return out


def channel_info(channel_ids: list[str], key=None) -> dict:
    if not channel_ids:
        return {}
    data = _get("channels", {"part": "snippet", "id": ",".join(channel_ids[:50])}, _key(key))
    return {it["id"]: {"title": it["snippet"].get("title", ""), "description": it["snippet"].get("description", ""),
                       "country": it["snippet"].get("country", ""),
                       "url": f"https://www.youtube.com/channel/{it['id']}"}
            for it in data.get("items", [])}


def comments(video_id: str, n=30, key=None) -> list[dict]:
    data = _get("commentThreads", {"part": "snippet", "videoId": video_id, "maxResults": max(1, min(n, 100)),
                                   "order": "relevance", "textFormat": "plainText"}, _key(key))
    out = []
    for it in data.get("items", []):
        top = ((it.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
        out.append({"author": top.get("authorDisplayName", ""), "author_channel":
                    (top.get("authorChannelUrl") or ""), "text": top.get("textDisplay", "")})
    return out


def collect(query, country="", lang="", mock_ctx=None) -> list[dict]:
    """搜视频并补齐频道简介和评论，返回给调研员交给 AI 识别。"""
    if mock_ctx is not None:
        return mockweb.youtube(query, mock_ctx["countries"], mock_ctx["keyword"])
    cfg = settings.get("youtube")
    vids = search_videos(query, country, lang, int(cfg["max_videos_per_query"]))
    chans = channel_info(list({v["channel_id"] for v in vids if v["channel_id"]}))
    for v in vids:
        ch = chans.get(v["channel_id"], {})
        v["channel_description"] = ch.get("description", "")
        v["channel_url"] = ch.get("url", "")
        v["channel_country"] = ch.get("country", "")
        try:
            v["comments"] = comments(v["video_id"], int(cfg["max_comments_per_video"]))
        except UserError as e:
            if e.key == "youtube_quota":
                raise
            v["comments"] = []
    return vids


def as_text(v: dict) -> str:
    lines = [f"Video: {v.get('title', '')}", f"Video URL: {v.get('url', '')}",
             f"Channel: {v.get('channel_title', '')} {v.get('channel_url', '')}",
             f"Channel country: {v.get('channel_country', '')}",
             "Channel description:", v.get("channel_description", "") or "(none)",
             "Video description:", v.get("description", "") or "(none)", "", "Comments:"]
    for c in v.get("comments", []):
        lines.append(f"- {c.get('author', '')}: {c.get('text', '')}")
    return "\n".join(lines)


def test(key=None) -> dict:
    res = search_videos("solar inverter", "US", "en", 1, key=_key(key))
    return {"results": len(res)}

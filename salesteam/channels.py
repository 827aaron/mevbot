"""渠道池（范围决定 #5）：所有“找客户的地方”统一一张表 channels —— 不只是 TG 群。

种类 kind：tg_group · tg_channel · fb_group · forum · b2b_board · tender_site · directory · expo_list ·
          youtube_channel · news_source
状态 status：candidate（推荐 / 待定）· monitored（在用）· retired（不用了）
每个渠道的产出：带来的线索数、合格数、被标“这条不好”的数量（leads.channel_id）。

第 2 步：TG 群 / 频道（已加入的 + AI 找群推荐的）同步进这张表；渠道拓展员的卡片由“AI 找群”作业驱动。
第 3 步（渠道拓展员完整上线）：定时发现、打分、在上限内自动加群（一次性风险确认后）、FB 群候选 + 入群问题建议、
群 → 监控员、名录 / 展会 / 论坛 → 调研员抓取、淘汰低产出渠道 —— 都写进同一张表（register / set_status）。
"""
import re
import time
from functools import wraps

from . import db
from .errors import UserError

KINDS = ["tg_group", "tg_channel", "fb_group", "forum", "b2b_board", "tender_site", "directory", "expo_list",
         "youtube_channel", "news_source"]
STATUSES = ["candidate", "monitored", "retired"]


def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from . import background
        with background.operation("channels:" + fn.__name__):
            return fn(*args, **kwargs)
    return wrapped


def _tg_key(username: str = "", chat_id: str = "") -> str:
    u = (username or "").lstrip("@").lower()
    return f"tg:@{u}" if u else f"tg:#{chat_id}"


@protected
def register(kind: str, name: str, url: str = "", key: str = "", country: str = "", language: str = "",
             members: int = 0, activity: str = "", status: str = "candidate", source: str = "", tg_chat_id=None,
             discovery_id=None) -> int:
    """登记或更新一个渠道（按 key 去重；key 默认用网址）。返回渠道 id。"""
    if kind not in KINDS:
        raise UserError("invalid_value", field="kind")
    if status not in STATUSES:
        raise UserError("invalid_value", field="status")
    key = key or (url or name).strip().lower()
    now = time.time()
    from . import settings
    override = (settings.get("channel_overrides", {}) or {}).get(key)
    if override == "retired":
        status = "retired"
    r = db.query_one("SELECT id, status FROM channels WHERE key=?", (key,))
    if r:
        db.execute("UPDATE channels SET kind=?, name=?, url=COALESCE(NULLIF(?,''), url), country=COALESCE(NULLIF(?,''), "
                   "country), language=COALESCE(NULLIF(?,''), language), members=?, activity=COALESCE(NULLIF(?,''), "
                   "activity), status=?, tg_chat_id=COALESCE(?, tg_chat_id), discovery_id=COALESCE(?, discovery_id), "
                   "updated_at=? WHERE id=?", (kind, name, url, country, language, int(members or 0), activity, status,
                                               tg_chat_id, discovery_id, now, r["id"]))
        return r["id"]
    return db.execute("INSERT INTO channels(key, kind, name, url, country, language, members, activity, status, source, "
                      "tg_chat_id, discovery_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, kind, name, url, country, language, int(members or 0), activity, status, source,
                       tg_chat_id, discovery_id, now, now))


def _guess(text: str) -> tuple[str, str]:
    """群名 / 简介 → 国家、语言（v1：马来西亚，中 / 英 / 马来语）。纯规则。"""
    from . import countries, langdetect
    code, _ = countries.infer_country(text or "", "", [])
    lg = langdetect.detect(text or "")
    return code or "", lg if lg in langdetect.LANGS else ""


@protected
def for_tg_chat(chat) -> int:
    """监控员的群 → 渠道 id（没有就登记）。"""
    c = dict(chat)
    from . import settings
    key = _tg_key(c.get("username", ""), c.get("chat_id", ""))
    if (settings.get("channel_overrides", {}) or {}).get(key) == "retired" and c.get("monitored"):
        db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (c["id"],))
        c["monitored"] = 0
    kind = "tg_channel" if c.get("kind") == "channel" else "tg_group"
    country, lang = _guess(f"{c.get('title', '')} {c.get('about', '')}")
    url = f"https://t.me/{c['username']}" if c.get("username") else ""
    return register(kind, c.get("title") or "", url, key=_tg_key(c.get("username", ""), c.get("chat_id", "")),
                    country=country, language=lang, members=c.get("members") or 0,
                    status="monitored" if c.get("monitored") else "retired", source="tg_joined", tg_chat_id=c["id"])


@protected
def sync_tg():
    """把 TG 群 / 频道（已加入的、AI 找群推荐的）同步进渠道池。同一个群被多个账号加入只算一个渠道。"""
    seen = set()
    for c in db.query("SELECT * FROM tg_chats ORDER BY monitored DESC, id"):
        k = _tg_key(c["username"], c["chat_id"])
        if k in seen:
            continue
        seen.add(k)
        for_tg_chat(c)
    for d in db.query("SELECT * FROM tg_discoveries WHERE status IN ('suggested','approved')"):
        m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", d["link"] or "")
        if not m:
            continue
        k = _tg_key(m.group(1))
        if k in seen:
            continue
        country, lang = _guess(f"{d['title']} {d['about']}")
        register("tg_group", d["title"], d["link"], key=k, country=country, language=lang, members=d["members"],
                 activity=d["activity"], status="candidate", source="ai_discovery", discovery_id=d["id"])


@protected
def list_all(kind: str = "", status: str = "") -> list[dict]:
    sync_tg()
    sql, args = ("SELECT c.*, COUNT(l.id) leads, COALESCE(SUM(l.qualified=1),0) qualified, "
                 "COALESCE(SUM(l.feedback=-1),0) bad, COALESCE(SUM(l.deal=1),0) deals "
                 "FROM channels c LEFT JOIN leads l ON l.channel_id=c.id"), []
    where = []
    if kind:
        where.append("c.kind=?")
        args.append(kind)
    if status:
        where.append("c.status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY c.id ORDER BY CASE c.status WHEN 'monitored' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, leads DESC, c.id"
    return db.query(sql, args)


@protected
def set_status(channel_id: int, status: str) -> dict:
    """在用 / 不用了。TG 群：同步到监控开关（不用了 = 停止监控，不退群）。推荐中的渠道请在“AI 找群”里处理。"""
    if status not in ("monitored", "retired"):
        raise UserError("invalid_value", field="status")
    c = db.query_one("SELECT * FROM channels WHERE id=?", (channel_id,))
    if not c:
        raise UserError("channel_not_found", 404)
    if c["status"] == "candidate":
        raise UserError("channel_candidate")
    from . import settings
    overrides = settings.get("channel_overrides", {}) or {}
    overrides[c["key"]] = status
    settings.put("channel_overrides", overrides)
    if c["tg_chat_id"]:
        from .tg import pool
        chat = db.query_one("SELECT username,chat_id FROM tg_chats WHERE id=?", (c["tg_chat_id"],))
        ids = [c["tg_chat_id"]]
        if chat:
            if chat["username"]:
                ids = [r["id"] for r in db.query("SELECT id FROM tg_chats WHERE LOWER(username)=LOWER(?)", (chat["username"],))]
            else:
                ids = [r["id"] for r in db.query("SELECT id FROM tg_chats WHERE chat_id=?", (chat["chat_id"],))]
        pool.set_monitored(ids, status == "monitored")
    db.execute("UPDATE channels SET status=?, updated_at=? WHERE id=?", (status, time.time(), channel_id))
    return {"id": channel_id, "status": status}


def summary() -> dict:
    rows = list_all()
    return {"total": len(rows), "monitored": sum(r["status"] == "monitored" for r in rows),
            "candidate": sum(r["status"] == "candidate" for r in rows),
            "kinds": sorted({r["kind"] for r in rows})}

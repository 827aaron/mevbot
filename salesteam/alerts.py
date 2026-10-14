"""异常提醒（PRD 7.2）+ 软件内提醒中心。

- raise_alert(key, level, category, link, **params)：同一个 key 未恢复前只记一次（防刷屏）
- resolve(key)：问题恢复；紧急提醒会补发“已恢复”
- 紧急（urgent）：立即通过通知号发到“异常接收人”；通知号不可用 → 电脑桌面弹窗，之后补发
- 一般（general）：每 2 小时汇总发送一次
- info：只在软件内显示（如高意向线索），可配合桌面弹窗

提醒文案用 i18n key：alert.<category>.title / alert.<category>.impact / alert.<category>.action
"""
import threading
import time

from . import background, db, i18n, notify, settings

_lock = threading.Lock()


@background.tracked("alert-write")
def raise_alert(key: str, level: str, category: str, link: str = "", desktop: bool = False, notice: bool = False,
                **params) -> int | None:
    """notice=True：只是通知（如“这一轮发现 N 个高意向买家”）—— 不进“待处理”、默认已读（U58）。"""
    with _lock:
        row = db.query_one("SELECT * FROM alerts WHERE key=? AND status IN ('open','notice')", (key,))
        now = db.now()
        if row:
            db.execute("UPDATE alerts SET count=count+1, updated_at=?, params=? WHERE id=?",
                       (now, db.dumps(params), row["id"]))
            return None
        aid = db.execute("INSERT INTO alerts(key, level, category, params, link, created_at, updated_at, status, read) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (key, level, category, db.dumps(params), link, now, now,
                                                      "notice" if notice else "open", 1 if notice else 0))
    if level == "urgent":
        background.spawn(_send_urgent, aid, name="urgent-alert")
    elif desktop:
        notify.desktop(i18n.t("app.name"), text_of(db.query_one("SELECT * FROM alerts WHERE id=?", (aid,)), short=True))
    return aid


@background.tracked("alert-resolve")
def resolve(key: str):
    with _lock:
        rows = db.query("SELECT * FROM alerts WHERE key=? AND status='open'", (key,))
        if not rows:
            return
        db.execute("UPDATE alerts SET status='resolved', resolved_at=? WHERE key=? AND status='open'", (db.now(), key))
    for r in rows:
        if r["level"] == "urgent" and r["notified_at"]:
            background.spawn(_send_recovered, r["id"], name="recovered-alert")


def text_of(row: dict, short=False, lg=None) -> str:
    p = db.loads(row["params"], {}) if isinstance(row.get("params"), str) else (row.get("params") or {})
    c = row["category"]
    icon = {"urgent": "🔴", "general": "🟡", "info": "🟢"}.get(row["level"], "")
    title = i18n.t(f"alert.{c}.title", p, lg)
    if short:
        return f"{icon} {title}"
    lines = [f"{icon} {title}"]
    for part in ("impact", "handled", "action"):
        k = f"alert.{c}.{part}"
        txt = i18n.t(k, p, lg)
        if txt != k and txt:
            lines.append(f"{i18n.t('alert.label_' + part, None, lg)}{txt}")
    if row.get("link"):
        lines.append(i18n.t("alert.label_where", None, lg) + i18n.t("alert.where_app", {"link": row["link"]}, lg))
    return "\n".join(lines)


def _recipients():
    return [r for r in settings.get("notify").get("alert_recipients") or [] if r]


@background.tracked("alert-send")
def _send_urgent(aid: int):
    from .tg import pool
    row = db.query_one("SELECT * FROM alerts WHERE id=?", (aid,))
    if not row:
        return
    ok = False
    if _recipients():
        ok = pool.notify_send(_recipients(), text_of(row), kind="alert")
    if ok:
        db.execute("UPDATE alerts SET notified_at=?, notify_failed=0 WHERE id=?", (db.now(), aid))
    else:
        db.execute("UPDATE alerts SET notify_failed=1 WHERE id=?", (aid,))
        notify.desktop(i18n.t("app.name"), text_of(row, short=True))


@background.tracked("alert-recovered")
def _send_recovered(aid: int):
    from .tg import pool
    row = db.query_one("SELECT * FROM alerts WHERE id=?", (aid,))
    if not row or row["resolved_notified"] or not _recipients():
        return
    msg = "✅ " + i18n.t("alert.recovered", {"title": i18n.t(f"alert.{row['category']}.title",
                                                             db.loads(row["params"], {}))})
    if pool.notify_send(_recipients(), msg, kind="alert"):
        db.execute("UPDATE alerts SET resolved_notified=1 WHERE id=?", (aid,))


@background.tracked("alert-flush")
def flush(force=False):
    """调度器调用：补发失败的紧急提醒；每 2 小时汇总发送一般提醒。"""
    from .tg import pool
    if not _recipients():
        return
    for r in db.query("SELECT id FROM alerts WHERE level='urgent' AND notify_failed=1 AND status='open'"):
        _send_urgent(r["id"])
    cfg = settings.get("notify")
    if not force and time.time() - float(cfg.get("last_general_at") or 0) < float(cfg.get("general_every_h", 2)) * 3600:
        return
    rows = db.query("SELECT * FROM alerts WHERE level='general' AND notified_at IS NULL ORDER BY id")
    cfg["last_general_at"] = time.time()
    settings.put("notify", cfg)
    if not rows:
        return
    text = "🟡 " + i18n.t("alert.digest_title", {"n": len(rows)}) + "\n\n" + "\n\n".join(text_of(r) for r in rows)
    if pool.notify_send(_recipients(), text, kind="alert"):
        db.executemany("UPDATE alerts SET notified_at=? WHERE id=?", [(db.now(), r["id"]) for r in rows])


def list_alerts(status=None, limit=200):
    sql, args = "SELECT * FROM alerts", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    rows = db.query(sql + " ORDER BY status='open' DESC, updated_at DESC LIMIT ?", args + [limit])
    for r in rows:
        r["params"] = db.loads(r["params"], {})
    return rows


def resolve_all() -> int:
    """「全部标为已解决」（U58）：把所有待处理的提醒标为已解决（紧急的照常补发“已恢复”）。"""
    keys = [r["key"] for r in db.query("SELECT DISTINCT key FROM alerts WHERE status='open'")]
    for k in keys:
        resolve(k)
    return len(keys)


def unread_count() -> dict:
    r = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(level='urgent'),0) u FROM alerts WHERE read=0")
    return {"unread": r["n"], "urgent": r["u"]}


def mark_read(ids=None):
    if ids:
        ph = ",".join("?" * len(ids))
        db.execute(f"UPDATE alerts SET read=1 WHERE id IN ({ph})", ids)
    else:
        db.execute("UPDATE alerts SET read=1")


def latest_unread_info(after_id: int) -> list:
    """给网页轮询：新出现的提醒（网页可以弹浏览器通知）。"""
    rows = db.query("SELECT * FROM alerts WHERE id>? ORDER BY id LIMIT 20", (after_id,))
    for r in rows:
        r["params"] = db.loads(r["params"], {})
    return rows

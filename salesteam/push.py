"""线索推送到人工 TG（PRD 7.1）：通知号把当天线索按天批量发给“线索接收人”。

- 触发：当天任务完成后自动发送；没完成的，到截止时间（默认 20:00）照常发送已找到的线索
- 内容：只发两样 —— ① 当日汇总（完成数/目标、各等级数量、来源分布）② Excel 附件（按等级排序的全部线索完整信息）
- 统一去重（只推送没推送过的线索）、统一排序（等级 → 评分）
- 只做通知；审核仍在软件审核台完成。手动「发送到 TG」用于补发 / 转发
- 配好通知号和接收人后默认开启；每日上限与随机间隔由 tg.pool.notify_send 把关
"""
import threading
import time

from . import leads as leads_mod
from . import background, countries, db, i18n, paths, settings
from .errors import UserError
from .tg import pool

_lock = threading.Lock()
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "-": 3}


def configured() -> bool:
    return bool(pool.notify_account()) and bool(settings.get("notify").get("lead_recipients"))


def _leads(ids=None, unpushed=True):
    if ids:
        ph = ",".join("?" * len(ids))
        rows = db.query(f"SELECT * FROM leads WHERE id IN ({ph})", list(ids))
    else:
        # v1 没有人工审核（PRD §1）：所有还没推送的合格线索都推送给客户的销售；有审核台时只推送审核通过的
        st = "('approved')" if leads_mod.review_on() else "('qualified','approved')"
        rows = db.query(f"SELECT * FROM leads WHERE qualified=1 AND status IN {st} "
                        + ("AND pushed_at IS NULL " if unpushed else "") + "ORDER BY id")
    for r in rows:
        r["profile"] = db.loads(r["profile"], {})
        r["contacts"] = db.loads(r["contacts"], [])
        r["sources"] = db.loads(r["sources"], [])
    rows.sort(key=lambda r: (GRADE_ORDER.get(r["grade"], 9), -(r["score"] or 0)))
    return rows


def _today_target():
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    rows = db.query("SELECT target, result FROM tasks WHERE kind='research' AND created_at>=?", (start,))
    return sum(r["target"] or 0 for r in rows), [r["result"] for r in rows]


def summary_text(leads, lg=None) -> str:
    target, results = _today_target()
    by_grade = {}
    by_src = {}
    for r in leads:
        by_grade[r["grade"]] = by_grade.get(r["grade"], 0) + 1
        by_src[r["first_source"]] = by_src.get(r["first_source"], 0) + 1
    lines = [i18n.t("push.title", {"date": time.strftime("%Y-%m-%d")}, lg)]
    if target:
        # 今日目标完成度按“今天找到的合格线索”算；推送的是其中已审核通过的
        t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        found = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND created_at>=?", (t0,))["n"]
        lines.append(i18n.t("push.progress", {"n": found, "target": target,
                                              "pct": round(100 * found / max(1, target))}, lg))
        if found < target:
            reason = "push.reason_budget" if "budget" in results else (
                "push.reason_running" if "" in results else "push.reason_sources")
            lines.append(i18n.t(reason, None, lg))
    lines.append(i18n.t("push.count", {"n": len(leads)}, lg))
    if by_grade:
        lines.append(i18n.t("push.grades", {"x": " · ".join(f"{g} {n}" for g, n in sorted(by_grade.items()))}, lg))
    if by_src:
        lines.append(i18n.t("push.sources", {"x": " · ".join(f"{i18n.t('source.' + s, None, lg)} {n}"
                                                             for s, n in by_src.items())}, lg))
    from . import quality
    q = quality.rates()
    if q["pushed"]:
        lines.append(i18n.t("push.quality", {"bad": round(q["bad_rate"] * 100), "deal": round(q["deal_rate"] * 100)}, lg))
    try:
        from . import leadpool
        lines += [""] + leadpool.status_lines(lg)
    except Exception:  # noqa: BLE001  线索池统计失败不影响推送
        pass
    return "\n".join(lines)


def card_text(r, lg=None) -> str:
    p = r["profile"]
    contact = next((c for c in r["contacts"] if c.get("name")), {})
    email = next((c["email"] for c in r["contacts"] if c.get("email")), "")
    tg = next((c["telegram"] for c in r["contacts"] if c.get("telegram")), "")
    src = (r["sources"] or [{}])[0]
    lines = [f"#{r['id']} · {r['grade']} · {countries.name(r['country'], 'zh' if (lg or i18n.lang()) == 'zh' else 'en') or '-'}"
             + (f" · {i18n.t('mtype.' + r['message_type'], None, lg)}" if r.get("message_type") else ""),
             f"🏢 {r['company_name']}" + (f" ({r['website']})" if r["website"] else "")]
    who = " ".join(x for x in (contact.get("name", ""), contact.get("title", ""), email, tg) if x)
    if who:
        lines.append(f"👤 {who}")
    need = (p.get("buying_signals") or [""])[0] or p.get("summary", "")
    if need:
        lines.append(f"📝 {need[:200]}")
    if src.get("quote"):
        lines.append(f"💬 “{src['quote'][:160]}”")
    if p.get("grade_reason"):
        lines.append(f"🧩 {p['grade_reason'][:160]}")
    if src.get("url"):
        lines.append(f"🔗 {src['url']}")
    return "\n".join(lines)


XLS_GROUPS = [
    ("basic", ["id", "score"]),          # PRD 7.1：基本只留 编号、分数（等级 / 国家 / 公司 / 官网 不放）
    ("contact", ["contact", "telegram", "whatsapp", "email"]),
    ("profile", ["need", "company_profile"]),
    ("basis", ["grade_reason", "signals", "fit"]),
    ("source", ["channel", "link", "quote", "posted"]),
    ("other", ["status", "assigned", "note", "found"]),
]
XLS_WIDTH = {"id": 7, "grade": 6, "score": 6, "country": 12, "company": 28, "website": 28, "contact": 24, "telegram": 18,
             "whatsapp": 20, "email": 28, "need": 45, "company_profile": 50, "grade_reason": 45, "signals": 35,
             "fit": 35, "channel": 12, "link": 38, "quote": 50, "posted": 16, "status": 14, "assigned": 14, "note": 30,
             "found": 16}


def _row(r, lg):
    p = r["profile"]
    zh = (lg or i18n.lang()) == "zh"
    contact = next((c for c in r["contacts"] if c.get("name")), {})
    was = []
    for c in r["contacts"]:
        if c.get("whatsapp"):
            was.append(c["whatsapp"] + ("" if c.get("wa_explicit") else (" (可能)" if zh else " (maybe)")))
    need = "; ".join(x for x in [(p.get("buying_signals") or [""])[0], *(p.get("pain_points") or [])[:1]] if x)
    prof = " / ".join(x for x in [p.get("summary", ""), p.get("size", ""), "; ".join(p.get("recent_news") or [])] if x)
    src = (r["sources"] or [{}])[0]
    fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""   # noqa: E731
    return {"id": r["id"], "grade": r["grade"], "score": r["score"],
            "country": countries.name(r["country"], "zh" if zh else "en"), "company": r["company_name"],
            "website": r["website"], "contact": (contact.get("name") or "").strip(),     # PRD 7.1：联系人只写姓名，不写职位
            "telegram": "; ".join(c["telegram"] for c in r["contacts"] if c.get("telegram")),
            "whatsapp": "; ".join(was), "email": "; ".join(c["email"] for c in r["contacts"] if c.get("email")),
            "need": need, "company_profile": prof, "grade_reason": p.get("grade_reason", ""),
            "signals": "; ".join(p.get("buying_signals") or []), "fit": "; ".join(p.get("fit_points") or []),
            "channel": i18n.t("source." + r["first_source"], None, lg), "link": src.get("url", ""),
            "quote": src.get("quote", ""), "posted": fmt(r.get("posted_at")),
            "status": i18n.t("lstatus." + r["status"], None, lg), "assigned": r.get("assigned_to") or "",
            "note": r.get("review_note") or "", "found": fmt(r["created_at"])}


def _grade_of(score) -> str:
    """行颜色按分数推出等级（与数据师的分数线一致：A ≥80，B 60–79）。"""
    try:
        s = int(score or 0)
    except (TypeError, ValueError):
        return ""
    return "A" if s >= 80 else "B" if s >= 60 else ""


def make_excel(leads, lg=None) -> str:
    """线索 Excel（TG 推送与线索库导出共用）：分组表头、冻结表头、自动换行；按分数上色（A ≥80 绿色、B 60–79 蓝色）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    cols = [c for _, cs in XLS_GROUPS for c in cs]
    col = 1
    for g, cs in XLS_GROUPS:
        ws.cell(row=1, column=col, value=i18n.t("xls.g." + g, None, lg))
        if len(cs) > 1:
            ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + len(cs) - 1)
        col += len(cs)
    for i, c in enumerate(cols, 1):
        ws.cell(row=2, column=i, value=i18n.t("xls." + c, None, lg))
        ws.column_dimensions[get_column_letter(i)].width = XLS_WIDTH.get(c, 16)
    head = PatternFill("solid", fgColor="E5E7EB")
    for row in (1, 2):
        for cell in ws[row]:
            cell.font = Font(bold=True)
            cell.fill = head
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    fills = {"A": PatternFill("solid", fgColor="E3F4E8"), "B": PatternFill("solid", fgColor="E7EEFC")}
    wrap = Alignment(wrap_text=True, vertical="top")
    for r in leads:
        d = _row(r, lg)
        ws.append([d[c] for c in cols])
        for cell in ws[ws.max_row]:
            cell.alignment = wrap
            g = _grade_of(r["score"])
            if g in fills:
                cell.fill = fills[g]
    ws.freeze_panes = "A3"
    paths.ensure_dirs()
    path = paths.EXPORT_DIR / time.strftime("leads-%Y%m%d-%H%M%S.xlsx")
    wb.save(path)
    return str(path)


@background.tracked("push")
def push(trigger="manual", lead_ids=None, recipients=None) -> dict:
    with _lock:
        if not pool.notify_account():
            raise UserError("notify_account_missing")
        rcps = [r for r in (recipients or settings.get("notify").get("lead_recipients") or []) if r]
        if not rcps:
            raise UserError("notify_recipients_missing")
        if not pool.notify_ready():
            raise UserError("notify_account_unhealthy")
        leads = _leads(lead_ids)
        lg = i18n.lang()
        # 只发两样：汇总消息 + Excel 附件（PRD 7.1，不逐条发卡片）
        text = summary_text(leads, lg) if trigger != "manual" else i18n.t("push.manual_title", {"n": len(leads)}, lg)
        xls = make_excel(leads, lg) if leads else None
        ok = pool.notify_send(rcps, text, kind="lead_push" if trigger != "manual" else "manual",
                              file_path=xls, file_name=(xls or "").rsplit("/", 1)[-1])
        if ok and leads:
            # 推送 = 交给销售：状态变为“已推送给销售”（之后客户可以标“这条不好”或“已成交”）
            db.executemany("UPDATE leads SET pushed_at=?, status=CASE WHEN status IN ('qualified','approved') "
                           "THEN 'pushed' ELSE status END WHERE id=?", [(db.now(), r["id"]) for r in leads])
        return {"ok": ok, "leads": len(leads), "recipients": len(rcps), "file": (xls or "").rsplit("/", 1)[-1]}


@background.tracked("auto-push")
def auto_push(trigger: str) -> dict | None:
    """自动推送：v1 每天一次，到客户设定的推送时间（默认 20:00，本机时间）把所有还没推送过的合格线索
    汇总 + Excel 发出（PRD 7.1）。“任务完成就推送”保留为内部开关，默认关闭。没配好通知号和接收人时静默跳过。"""
    cfg = settings.get("notify")
    if not cfg.get("push_enabled") or not configured():
        return None
    if trigger == "task_done" and not cfg.get("push_on_task_done", False):
        return None
    today = time.strftime("%Y-%m-%d")
    if trigger == "cutoff":
        cfg["last_cutoff_day"] = today          # 推送时间每天只触发一次
        settings.put("notify", cfg)
    try:
        res = push(trigger)
    except UserError as e:
        from . import alerts
        alerts.raise_alert(f"push_failed:{today}", "urgent" if e.key == "notify_account_unhealthy" else "general",
                           "push_failed", link="#/settings/notify", error=e.key)
        return {"ok": False, "error": e.key}
    if res["ok"]:
        cfg = settings.get("notify")
        cfg["last_push_day"] = today
        settings.put("notify", cfg)
    return res


def cutoff_due(now=None) -> bool:
    now = now or time.time()
    cfg = settings.get("notify")
    try:
        hh, mm = (cfg.get("cutoff") or "20:00").split(":")
        at = time.mktime(time.localtime(now)[:3] + (int(hh), int(mm), 0, 0, 0, -1))
    except ValueError:
        return False
    return now >= at and cfg.get("last_cutoff_day") != time.strftime("%Y-%m-%d", time.localtime(now))

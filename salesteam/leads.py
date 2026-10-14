"""线索库：查询 / 去重 / 合并 / 导出 CSV / 删除。"""
import csv
import io
import re
import threading
import urllib.parse

from . import countries, db
from .errors import UserError

# 去重 + 写入要原子（多个数据师 worker 并行）
write_lock = threading.Lock()


def review_on() -> bool:
    from .agents.registry import FEATURES
    return bool(FEATURES.get("review"))


def qualified_status() -> str:
    """合格线索的初始状态：有审核台时是“待审核”，v1 没有审核环节时是“合格·待推送”。"""
    return "pending_review" if review_on() else "qualified"


UNSENT = ("qualified", "pending_review")      # 合格但还没交给销售（没推送 / 没审核）

_LEGAL = re.compile(
    r"\b(gmbh|ag|kg|ohg|ug|mbh|ltd|limited|llc|l\.l\.c|inc|corp|corporation|co|company|plc|pty|bv|b\.v|nv|n\.v|sa|s\.a|"
    r"sas|sarl|s\.a\.r\.l|srl|s\.r\.l|spa|s\.p\.a|sl|s\.l|sp\. z o\.o|sp z oo|oy|ab|as|a/s|aps|kft|zrt|sro|s\.r\.o|"
    r"ltda|de c\.v|cv|k\.k|kk|a\.ş|as|fze|fzco|fzc|est|group|holding|international|intl)\b\.?", re.I)


def company_key(name: str) -> str:
    n = (name or "").lower()
    n = _LEGAL.sub(" ", n)
    n = re.sub(r"[^\w]+", " ", n, flags=re.UNICODE)
    return re.sub(r"\s+", " ", n).strip()


def domain_of(url: str) -> str:
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    host = host.removeprefix("www.")
    # 社交平台/名录网站上的主页不能作为公司唯一域名
    if any(host == d or host.endswith("." + d) for d in
           ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "t.me", "twitter.com", "x.com",
            "google.com", "wa.me", "tiktok.com")):
        return ""
    return host


def settle_review_statuses() -> int:
    """v1 没有审核环节：旧数据里“合格·待审核”的线索改为“合格·待推送”（审核台打开时不动）。"""
    if review_on():
        return 0
    n = db.query_one("SELECT COUNT(*) n FROM leads WHERE status='pending_review'")["n"]
    if n:
        db.execute("UPDATE leads SET status='qualified', updated_at=? WHERE status='pending_review'", (db.now(),))
    return n


def find_duplicate(tgs=(), was=()):
    """同一个客户 = 同一个 TG 用户名 或 同一个 WhatsApp 号码（统一成 +国家码格式）。只用这两个键判重（老板决定：
    v1 不按域名 / 公司名 / 邮箱判重，也不做跨语言公司名匹配）。返回已有线索 id 或 None。"""
    from .contacts import norm_phone
    for t in tgs or ():
        v = (t or "").lower().lstrip("@")
        if v:
            r = db.query_one("SELECT lead_id FROM lead_identities WHERE kind='tg' AND value=?", (v,))
            if r:
                return r["lead_id"]
    for w in was or ():
        n = norm_phone(w)
        if n:
            r = db.query_one("SELECT lead_id FROM lead_identities WHERE kind='wa' AND value=?", (n,))
            if r:
                return r["lead_id"]
    return None


def numbers_of(contacts: list[dict], extra=()) -> list[str]:
    """联系人里的 WhatsApp / 电话号码（统一格式，去重），用于判重和黑名单。"""
    from .contacts import norm_phone
    out = [norm_phone(c.get("whatsapp") or "") for c in contacts or [] if c.get("whatsapp")]
    out += [norm_phone(x) for x in extra or () if x]
    return [x for x in dict.fromkeys(out) if x]


def _add_identities(lead_id, domain, contacts):
    """判重身份：TG 用户名 + WhatsApp 号码（联系人里的号码都算，统一格式）。domain 参数保留只为兼容调用方。"""
    from .contacts import norm_phone
    rows = []
    for c in contacts:
        if c.get("telegram"):
            rows.append(("tg", c["telegram"].lower().lstrip("@"), lead_id))
        n = norm_phone(c.get("whatsapp") or "")
        if n:
            rows.append(("wa", n, lead_id))
    db.executemany("INSERT OR IGNORE INTO lead_identities(kind, value, lead_id) VALUES(?,?,?)", rows)


def insert(lead: dict) -> int:
    now = db.now()
    lead_id = db.execute(
        "INSERT INTO leads(company_name, company_key, domain, website, country, customer_type, industry, grade, score, "
        "qualified, status, not_qualified_reason, profile, contacts, sources, first_source, icp_id, task_id, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead["company_name"], company_key(lead["company_name"]), lead.get("domain", ""), lead.get("website", ""),
         lead.get("country", ""), lead.get("customer_type", ""), lead.get("industry", ""), lead["grade"],
         int(lead.get("score") or 0), int(bool(lead.get("qualified"))), lead["status"],
         lead.get("not_qualified_reason", ""), db.dumps(lead.get("profile") or {}), db.dumps(lead.get("contacts") or []),
         db.dumps(lead.get("sources") or []), lead.get("first_source", ""), lead.get("icp_id"), lead.get("task_id"),
         now, now))
    extra = {k: lead.get(k) for k in ("message_type", "intent", "found_by_account", "posted_at", "channel_id")
             if lead.get(k) not in (None, "")}
    if extra:
        db.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in extra)} WHERE id=?", (*extra.values(), lead_id))
    _add_identities(lead_id, lead.get("domain", ""), lead.get("contacts") or [])
    return lead_id


def merge_into(lead_id: int, contacts: list[dict], sources: list[dict], requalify: bool = True):
    """重复线索：把新发现的联系人和来源合并进已有线索（同一公司多人算 1 个）。"""
    row = db.query_one("SELECT contacts, sources FROM leads WHERE id=?", (lead_id,))
    if not row:
        return
    old_c = db.loads(row["contacts"], [])
    old_s = db.loads(row["sources"], [])
    from .contacts import norm_phone

    def _key(c):    # WhatsApp 按统一格式比较（+60 12-345 6789 与 +60123456789 是同一个号码）
        return ((c.get("email") or "").lower(), (c.get("telegram") or "").lower().lstrip("@"),
                norm_phone(c.get("whatsapp") or ""), c.get("name") or "")
    keys = {_key(c) for c in old_c}
    for c in contacts:
        k = _key(c)
        if k not in keys and any(k):
            old_c.append(c)
            keys.add(k)
    urls = {s.get("url") for s in old_s}
    for s in sources:
        if s.get("url") and s["url"] not in urls:
            old_s.append(s)
            urls.add(s["url"])
    db.execute("UPDATE leads SET contacts=?, sources=?, updated_at=? WHERE id=?",
               (db.dumps(old_c), db.dumps(old_s), db.now(), lead_id))
    _add_identities(lead_id, "", contacts)
    # 合并带来了 TG / 明确的 WhatsApp → 备用线索立刻重新判定（PRD 5.2.2）
    if requalify:
        from . import leadpool
        leadpool.requalify(lead_id, "merge")


# ------------------------------------------------------------------ 查询

def _where(f: dict):
    sql, args = [], []
    if f.get("grade"):
        g = [x for x in str(f["grade"]).split(",") if x in ("A", "B", "C")]
        if g:
            sql.append(f"grade IN ({','.join('?' * len(g))})")
            args += g
    if f.get("country"):
        sql.append("country=?")
        args.append(f["country"])
    if f.get("source"):
        sql.append("first_source=?")
        args.append(f["source"])
    if f.get("status"):
        sql.append("status=?")
        args.append(f["status"])
    if f.get("qualified") in ("1", "0", 1, 0, True, False):
        sql.append("qualified=?")
        args.append(int(f["qualified"] in ("1", 1, True)))
    if f.get("icp_id"):
        sql.append("icp_id=?")
        args.append(int(f["icp_id"]))
    if f.get("task_id"):
        sql.append("task_id=?")
        args.append(f["task_id"])
    if f.get("feedback") in ("1", "-1", "0"):
        sql.append("feedback=?")
        args.append(int(f["feedback"]))
    if f.get("q"):
        sql.append("(company_name LIKE ? OR website LIKE ? OR contacts LIKE ? OR profile LIKE ?)")
        args += [f"%{f['q']}%"] * 4
    return (" WHERE " + " AND ".join(sql)) if sql else "", args


def _decode(r: dict) -> dict:
    r = dict(r)
    for k, d in (("profile", {}), ("contacts", []), ("sources", [])):
        r[k] = db.loads(r[k], d)
    return r


def list_leads(f: dict, page=1, size=50):
    where, args = _where(f)
    total = db.query_one(f"SELECT COUNT(*) n FROM leads{where}", args)["n"]
    rows = db.query(f"SELECT * FROM leads{where} ORDER BY qualified DESC, grade ASC, score DESC, id DESC "
                    f"LIMIT ? OFFSET ?", args + [size, (max(1, page) - 1) * size])
    items = []
    for r in rows:
        r = _decode(r)
        r["primary_email"] = next((c["email"] for c in r["contacts"] if c.get("email")), "")
        r["primary_tg"] = next((c["telegram"] for c in r["contacts"] if c.get("telegram")), "")
        r["primary_wa"] = next((c["whatsapp"] for c in r["contacts"] if c.get("whatsapp") and c.get("wa_explicit")), "")
        r.pop("profile", None)
        items.append(r)
    return {"total": total, "page": page, "size": size, "items": items}


def facets():
    return {
        "countries": [r["country"] for r in db.query("SELECT DISTINCT country FROM leads WHERE country<>'' "
                                                     "ORDER BY country")],
        "sources": [r["first_source"] for r in db.query("SELECT DISTINCT first_source FROM leads "
                                                        "WHERE first_source<>''")],
        "statuses": [r["status"] for r in db.query("SELECT DISTINCT status FROM leads")],
        "counts": db.query_one("SELECT COUNT(*) total, COALESCE(SUM(qualified),0) qualified, "
                               "COALESCE(SUM(grade='A'),0) a, COALESCE(SUM(grade='B'),0) b, "
                               "COALESCE(SUM(grade='C'),0) c FROM leads"),
    }


def get(lead_id: int):
    r = db.query_one("SELECT * FROM leads WHERE id=?", (lead_id,))
    if not r:
        raise UserError("lead_not_found", 404)
    d = _decode(r)
    icp = db.query_one("SELECT name FROM icp_templates WHERE id=?", (r["icp_id"],)) if r["icp_id"] else None
    d["icp_name"] = icp["name"] if icp else ""
    d["raw"] = db.query("SELECT id, source, source_url, source_title, status, reason, created_at FROM raw_leads "
                        "WHERE lead_id=? ORDER BY id", (lead_id,))
    return d


def delete(ids: list[int]):
    ids = [int(i) for i in ids][:5000]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    with db.transaction():
        db.execute(f"DELETE FROM lead_identities WHERE lead_id IN ({ph})", ids)
        db.execute(f"UPDATE raw_leads SET lead_id=NULL WHERE lead_id IN ({ph})", ids)
        db.execute(f"UPDATE icp_samples SET lead_id=NULL WHERE lead_id IN ({ph})", ids)   # 样本保留快照
        db.execute(f"DELETE FROM leads WHERE id IN ({ph})", ids)
    return len(ids)


CSV_COLUMNS = ["id", "company_name", "website", "country", "country_name", "customer_type", "industry", "grade",
               "score", "qualified", "status", "emails", "telegram", "whatsapp", "contacts", "summary", "grade_reason",
               "buying_signals", "fit_points", "sources", "first_source", "icp", "feedback", "created_at"]


def export_rows(f: dict, ids=None) -> list[dict]:
    """导出用：按当前筛选（或勾选的线索）取完整数据，排序同推送（等级 → 评分）。"""
    if ids:
        ph = ",".join("?" * len(ids))
        rows = db.query(f"SELECT * FROM leads WHERE id IN ({ph})", [int(i) for i in ids])
    else:
        where, args = _where(f)
        rows = db.query(f"SELECT * FROM leads{where}", args)
    for r in rows:
        r.update(_decode(r))
    order = {"A": 0, "B": 1, "C": 2}
    rows.sort(key=lambda r: (order.get(r["grade"], 9), -(r["score"] or 0)))
    return rows


def export_csv(f: dict, lang="zh") -> str:
    where, args = _where(f)
    rows = db.query(f"SELECT l.*, t.name icp_name FROM leads l LEFT JOIN icp_templates t ON t.id=l.icp_id"
                    f"{where} ORDER BY qualified DESC, grade, score DESC",
                    args)
    buf = io.StringIO()
    buf.write("﻿")   # 让 Excel 正确识别 UTF-8
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    import time as _t
    for r in rows:
        d = _decode(r)
        p = d["profile"]
        w.writerow([
            d["id"], d["company_name"], d["website"], d["country"], countries.name(d["country"], lang),
            d["customer_type"], d["industry"], d["grade"], d["score"], d["qualified"], d["status"],
            "; ".join(c["email"] for c in d["contacts"] if c.get("email")),
            "; ".join(c["telegram"] for c in d["contacts"] if c.get("telegram")),
            "; ".join(c["whatsapp"] + ("" if c.get("wa_explicit") else " (?)") for c in d["contacts"] if c.get("whatsapp")),
            "; ".join(f"{c.get('name', '')} {c.get('title', '')}".strip() for c in d["contacts"]
                      if c.get("name")),
            p.get("summary", ""), p.get("grade_reason", ""), "; ".join(p.get("buying_signals") or []),
            "; ".join(p.get("fit_points") or []), " | ".join(s.get("url", "") for s in d["sources"]),
            d["first_source"], r.get("icp_name") or "", d["feedback"],
            _t.strftime("%Y-%m-%d %H:%M", _t.localtime(d["created_at"])),
        ])
    return buf.getvalue()

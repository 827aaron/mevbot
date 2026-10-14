"""进群（PRD 6.3）。

- A 人工进群：账号已有的群由 pool.sync_dialogs 识别，客户勾选监控（默认开启）
- B 辅助进群：粘贴群链接 → 排队，慢速自动加入并分配到不同账号（默认关闭，开启需风险确认）
- C AI 找群：TG 内搜索 + Google site:t.me + 群导航网站 → AI 评估人数/活跃度/相关度/是否广告群 → 推荐清单，
  客户确认后走 B 加入（加入仍需开启 B）
"""
import random
import re
import time
from functools import wraps

from .. import alerts, db, llm, prompts, settings
from ..errors import UserError
from ..llm import decision
from . import base, pool

LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)?[\w-]{4,}|@[A-Za-z0-9_]{4,}")



def protected(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from .. import background
        with background.operation("joining:" + fn.__name__):
            return fn(*args, **kwargs)
    return wrapped

# ------------------------------------------------------------------ B. 辅助进群

@protected
def enqueue(links_text: str, source="manual", icp_id=None, discovery_ids=None) -> int:
    with db.transaction():
        return _enqueue(links_text, source, icp_id, discovery_ids)


def _enqueue(links_text: str, source="manual", icp_id=None, discovery_ids=None) -> int:
    links = []
    for m in LINK_RE.finditer(links_text or ""):
        link = m.group(0)
        if link.startswith("@"):
            link = "https://t.me/" + link[1:]
        if not link.startswith("http"):
            link = "https://" + link
        if link not in links:
            links.append(link)
    n = 0
    for i, link in enumerate(links):
        if db.query_one("SELECT id FROM tg_join_queue WHERE link=? AND status IN ('pending','joined','needs_approval')",
                        (link,)):
            continue
        db.execute("INSERT INTO tg_join_queue(link, status, source, discovery_id, icp_id, created_at, next_at) "
                   "VALUES(?,?,?,?,?,?,?)", (link, "pending", source, (discovery_ids or [None] * len(links))[i]
                                             if discovery_ids else None, icp_id, db.now(), 0))
        n += 1
    if not links:
        raise UserError("tg_no_links")
    return n


def queue(limit=200):
    return db.query("SELECT q.*, a.phone, a.label account_label FROM tg_join_queue q LEFT JOIN tg_accounts a "
                    "ON a.id=q.account_id ORDER BY q.id DESC LIMIT ?", (limit,))


def cancel(qid: int):
    db.execute("UPDATE tg_join_queue SET status='cancelled' WHERE id=? AND status='pending'", (qid,))


@protected
def process_one() -> dict | None:
    """调度器调用：辅助进群开启时，每次最多加入一个群，两次之间随机间隔。"""
    cfg = settings.get("joining")
    if not cfg.get("assisted_join"):
        return None
    st = settings.get("join_state", {}) or {}
    if time.time() < float(st.get("next_at") or 0):
        return None
    item = db.query_one("SELECT * FROM tg_join_queue WHERE status='pending' AND next_at<=? ORDER BY id LIMIT 1",
                        (time.time(),))
    if not item:
        return None
    acc = pool.pick_account_for_join()          # 新群分给群最少的监控账号，由它加入（PRD 6.2.1）
    if not acc:
        return {"skipped": "no_account"}
    from .. import lease
    # 进群优先级最低：只在这个账号空闲时进行（监控员读消息、渠道拓展员搜索优先）
    ls = lease.acquire("tg", "monitor", purpose="join", agent="scout", account_id=acc["id"], idle_only=True)
    if not ls:
        return {"skipped": "account_busy"}
    try:
        return _join_with(ls, acc, item, cfg)
    finally:
        ls.release()


def _join_with(ls, acc, item, cfg) -> dict:
    from .. import lease
    if lease.remaining("tg", acc["id"], "join") < 1:
        return {"skipped": "daily_cap"}
    gap = random.uniform(float(cfg["min_gap_min"]), float(cfg["max_gap_min"])) * 60
    settings.put("join_state", {"next_at": time.time() + gap})
    be = pool.backend()
    db.execute("UPDATE tg_join_queue SET attempts=attempts+1, account_id=? WHERE id=?", (acc["id"], item["id"]))
    try:
        chat = be.join(pool._acc(acc), item["link"])
        ls.use("join")
        pool.mark_ok(acc["id"])
        pool.upsert_chat(acc["id"], chat, "C" if item["source"] == "discovery" else "B")
        db.execute("UPDATE tg_join_queue SET status='joined', title=?, done_at=? WHERE id=?",
                   (chat.get("title", ""), db.now(), item["id"]))
        if item["discovery_id"]:
            db.execute("UPDATE tg_discoveries SET status='joined' WHERE id=?", (item["discovery_id"],))
        # 新加入的群默认勾选监控（客户可取消）
        row = db.query_one("SELECT id FROM tg_chats WHERE account_id=? AND chat_id=?", (acc["id"], str(chat["chat_id"])))
        if row:
            pool.set_monitored([row["id"]], True, item["icp_id"])
        return {"joined": item["link"]}
    except base.NeedsApproval:
        ls.use("join")
        db.execute("UPDATE tg_join_queue SET status='needs_approval', done_at=? WHERE id=?", (db.now(), item["id"]))
        return {"needs_approval": item["link"]}
    except base.FloodWait as e:
        pool.handle_error(acc["id"], e)
        db.execute("UPDATE tg_join_queue SET next_at=?, error=? WHERE id=?", (time.time() + e.seconds + 60,
                                                                              "flood_wait", item["id"]))
        return {"flood": e.seconds}
    except base.InvalidLink as e:
        db.execute("UPDATE tg_join_queue SET status='failed', error=?, done_at=? WHERE id=?",
                   ("invalid_link", db.now(), item["id"]))
        return {"failed": e.detail}
    except base.TGError as e:
        pool.handle_error(acc["id"], e)
        db.execute("UPDATE tg_join_queue SET error=?, next_at=? WHERE id=?", (e.key, time.time() + 3600, item["id"]))
        if item["attempts"] + 1 >= 3:
            db.execute("UPDATE tg_join_queue SET status='failed' WHERE id=?", (item["id"],))
        return {"error": e.key}


# ------------------------------------------------------------------ C. AI 找群

# 评估提示词在提示词库 scout.group_eval（渠道拓展员）

DISC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["groups"], "properties": {
    "groups": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                          "required": ["idx", "relevance", "ad_prob", "reason"],
                                          "properties": {"idx": {"type": "integer"},
                                                         "relevance": {"type": "number"},
                                                         "ad_prob": {"type": "number"},
                                                         "reason": {"type": "string"}}}}}}


def scout_op(action: str, fn):
    """渠道拓展员的一次 TG 操作（搜索 / 预览）：借一个有“找群”用途的空闲账号（中优先级，让给监控员读消息），
    记这个账号今天的次数（到上限就换账号），做完立刻还。没有可用账号返回 None。"""
    from .. import lease
    skip = set()
    while True:
        ls = lease.acquire("tg", "scout", purpose="scout", agent="scout", timeout=120, skip=skip)
        if not ls:
            return None
        try:
            if lease.remaining("tg", ls.account_id, action) < 1:
                skip.add(ls.account_id)
                continue
            ls.use(action)
            out = fn(pool._acc(pool._row(ls.account_id)))
            pool.mark_ok(ls.account_id)
            return out
        except base.InvalidLink:
            return {}
        except base.TGError as e:
            pool.handle_error(ls.account_id, e)
            skip.add(ls.account_id)
        finally:
            ls.release()


@protected
def discover(icp_id: int, emit=None, max_groups=25, auto_queue=True) -> dict:
    """AI 找群：生成推荐清单（不会自动加入）。"""
    from ..agents import icp as icp_mod
    from ..agents.researcher import icp_brief
    from ..sources import search as search_mod
    emit = emit or (lambda *a, **k: None)
    icp = icp_mod.normalize(icp_mod.get(icp_id)["data"])
    if not llm.configured("scout"):
        raise UserError("agent_not_configured", agent="@agent.scout")
    if not pool.scout_accounts():
        raise UserError("tg_no_active_account")
    be = pool.backend()
    from .. import countries
    # 搜索词全部来自客户模板的线索词（PRD §3）：产品词（+ 客户说的目标国家的国名，没说国家就不加）+ 几个求购词
    # （程序不写死任何国家或业务词，PRD 2.4.2 / §1）
    prods = (icp.get("product_words") or [])
    local = [f"{k} {countries.name(c)}" for k in prods[:2] for c in countries.profile_countries(icp)[:3]]
    kws = list(dict.fromkeys(local + prods[:6] + (icp.get("buy_words") or [])[:2]))[:10]
    found: dict[str, dict] = {}
    # 1) TG 内搜索
    for kw in kws:
        emit("ev.tg_search", query=kw)
        res = scout_op("search", lambda a, kw=kw: be.search(a, kw, 10))
        if res is None:
            break                               # 没有可用账号 / 今天的搜索上限到了
        for c in res:
            if c.get("username"):
                found.setdefault(f"https://t.me/{c['username']}", {**c, "source": "tg_search"})
    # 2) Google site:t.me（用已配置的搜索方式；mock 后端时跳过网络）
    if not be.is_mock:
        for kw in kws[:3]:
            try:
                results, _ = search_mod.search(f"site:t.me {kw}", n=8)
            except UserError:
                break
            for r in results:
                m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})/?$", r["url"])
                if m and m.group(1).lower() not in ("s", "joinchat", "addstickers"):
                    found.setdefault(f"https://t.me/{m.group(1)}", {"title": r.get("title", ""), "username": m.group(1),
                                                                    "source": "web"})
    # 已在名单或已加入的跳过
    joined = {r["username"].lower() for r in db.query("SELECT username FROM tg_chats WHERE username<>'' AND left_at IS NULL")}
    cands = [(link, c) for link, c in found.items() if (c.get("username") or "").lower() not in joined][:max_groups]
    # 3) 读取公开信息（人数、简介、最近消息，估算活跃度）
    infos = []
    for link, c in cands:
        p = scout_op("preview", lambda a, link=link: be.preview(a, link, sample=20))
        if p:
            infos.append((link, {**c, **p}))
    if not infos:
        return {"added": 0}
    # 4) 判断模型先筛广告群（拿不准的交给大模型）；大模型批量评估
    text_items = []
    for i, (link, c) in enumerate(infos):
        sample = " | ".join((s or "")[:140] for s in (c.get("samples") or [])[:8])
        text_items.append(f"[{i}] {c.get('title')} — members {c.get('members')}, ~{c.get('msgs_per_day', '?')} msgs/day\n"
                          f"about: {c.get('about', '')[:300]}\nsample messages: {sample}")
    user = icp_brief(icp) + "\n\nGroups:\n\n" + "\n\n".join(text_items)
    emit("ev.tg_evaluating", n=len(infos))
    data, _ = llm.call_json("scout", "group_eval",          # 渠道拓展员（第 2 步由“AI 找群”驱动）
                            prompts.render("scout.group_eval", scope=prompts.get("scope.v1") if countries.V1_SCOPE else ""),
                            user, DISC_SCHEMA,
                            context={"groups": [c for _, c in infos], "icp": icp})
    added = 0
    for g in data.get("groups") or []:
        i = g.get("idx")
        if not isinstance(i, int) or not 0 <= i < len(infos):
            continue
        link, c = infos[i]
        rel, ad = float(g.get("relevance") or 0), float(g.get("ad_prob") or 0)
        if decision.enabled():
            q, opts = prompts.options("decision.ad_group")
            d = decision.decide(text_items[i], opts, q, agent="scout",
                                context={"mock_hint": "ad_group" if ad > 0.5 else "buyer_group"})
            if d and not d.escalate:
                # 判断模型只能淘汰、不能放行（PRD 2.3）：只能把“像广告群”的概率往上调，不能把大模型判断的调低
                ad = max(ad, float(d.probs.get("ad_group", ad)))
        score = round(rel * (1 - ad) * 100)
        try:
            db.execute("INSERT INTO tg_discoveries(icp_id, link, title, about, members, activity, relevance, ad_prob, "
                       "score, reason, source, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (icp_id, link, c.get("title", ""), c.get("about", "")[:500], int(c.get("members") or 0),
                        str(c.get("msgs_per_day", "")), rel, ad, score, g.get("reason", "")[:300],
                        c.get("source", ""), db.now()))
            added += 1
        except Exception:  # noqa: BLE001  已推荐过（UNIQUE link）
            db.execute("UPDATE tg_discoveries SET relevance=?, ad_prob=?, score=?, reason=? WHERE link=? AND "
                       "status='suggested'", (rel, ad, score, g.get("reason", "")[:300], link))
    return {"added": added, "evaluated": len(infos), "queued": auto_queue_discoveries(icp_id) if auto_queue else 0}


def discoveries(status=None):
    sql, args = "SELECT * FROM tg_discoveries", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    return db.query(sql + " ORDER BY status='suggested' DESC, score DESC LIMIT 300", args)


@protected
def approve_discoveries(ids: list[int]) -> int:
    """客户确认推荐的群 → 进入 B 的排队（需要开启辅助进群才会真正加入）。"""
    n = 0
    for i in ids:
        with db.transaction():
            d = db.query_one("SELECT * FROM tg_discoveries WHERE id=? AND status='suggested'", (int(i),))
            if not d:
                continue
            try:
                n += _enqueue(d["link"], source="discovery", icp_id=d["icp_id"], discovery_ids=[d["id"]])
            except UserError:
                continue
            db.execute("UPDATE tg_discoveries SET status='approved' WHERE id=?", (d["id"],))
    if n and not settings.get("joining").get("assisted_join"):
        alerts.raise_alert("join_disabled_pending", "info", "join_disabled", link="#/groups", n=n)
    return n


def reject_discoveries(ids: list[int]):
    for i in ids:
        db.execute("UPDATE tg_discoveries SET status='rejected' WHERE id=?", (int(i),))


@protected
def auto_queue_discoveries(icp_id=None):
    """Explicit opt-in only; >=70 still uses the ordinary capped join queue."""
    cfg = settings.get("scout_settings", {}) or {}
    if (not cfg.get("auto_queue") or not settings.get("joining").get("assisted_join")
            or settings.ai_paused() or not settings.agent_enabled("scout")):
        return 0
    allowed_icp = cfg.get("icp_id")
    if not allowed_icp or (icp_id is not None and allowed_icp != icp_id):
        return 0
    overrides = settings.get("channel_overrides", {}) or {}
    ids = []
    for d in db.query("SELECT id,link FROM tg_discoveries WHERE status='suggested' AND score>=70 AND icp_id=?", (allowed_icp,)):
        match = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", d["link"] or "")
        if match and overrides.get("tg:@" + match.group(1).lower()) != "retired":
            ids.append(d["id"])
    return approve_discoveries(ids)

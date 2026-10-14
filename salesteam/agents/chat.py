"""单点对话（PRD 2.1.3）：客户可以单独和任一 AI 对话。

省 token：每次只带 ①该 AI 的岗位说明书 ②已确认的备忘录 ③它自己的工作数据摘要 ④最近 6 轮对话；
不带其他 AI 的数据、不带完整历史。
备忘录：客户说出指令性的话（“以后…/不要…/只…”），AI 提炼成明确规则 → 出现“备忘录卡片”，客户确认后长期生效。

可嵌入（工作台的主对话区 + 每个页面的主管对话抽屉）：
- history(after=…) 增量拉取；unread / seen 未读数（抽屉角标）
- send(page=…) 带上客户当前所在页面（一句话），主管能就地回答“这条为什么是 A”
- 回复可以带“行内动作”（actions）：撤销、去审核台/某页面、确认/不要备忘录。
  动作只能是白名单里的类型；撤销类动作只由程序生成（不让模型生成），模型只能建议“去某页面”。
- post()：程序把事件写进对话（例如客户在审核台点「这条不好」→ 主管回复“已调整… [撤销]”），抽屉里能看到
"""
import threading
from functools import wraps

from .. import db, i18n, llm, prompts, settings
from ..errors import UserError
from . import memory, schemas
from .registry import AGENT_KEYS, active_keys, is_active

HISTORY_TURNS = 6
# Serialize context mutations, never the network request. A cleared conversation
# must not be recreated by a response that was already in flight.
_context_lock = threading.RLock()
_generations = {agent: 0 for agent in AGENT_KEYS}


def _locked(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _context_lock:
            return fn(*args, **kwargs)
    return wrapped

# 模型可建议的页面（goto 动作）；其余动作类型只由程序生成
ROUTES = ["leads", "channels", "run", "learning", "accounts", "icp", "settings", "onboarding"]   # v1 没有审核台
ACTION_TYPES = {"goto", "undo_review", "confirm_memo", "reject_memo", "open_lead"}
SCHEMA = schemas.obj({"reply": schemas.S,
                      "memos": {"type": "array", "items": schemas.S},
                      "goto": {"type": "string", "enum": [""] + ROUTES}})

PAGE_HINT = "\n\n## Where the customer is right now\n{page}"


def _row(r: dict) -> dict:
    r["meta"] = db.loads(r["meta"], {}) if isinstance(r.get("meta"), str) else (r.get("meta") or {})
    acts = r["meta"].get("actions") or []
    for a in acts:                       # 备忘录动作：带上当前状态（在别处确认过也能正确显示）
        if a.get("type") in ("confirm_memo", "reject_memo") and not a.get("done"):
            m = db.query_one("SELECT status FROM memos WHERE id=?", (a.get("memo_id"),))
            if not m or m["status"] != "pending":
                a["done"] = True
                a["result"] = m["status"] if m else "deleted"
    r["actions"] = acts
    return r


def shape(r: dict) -> dict:
    """前端（工作台主对话 / 主管抽屉）用的统一消息格式：{id, from, time, text, card?, memo?, actions}。
    actions 的 act 对应前端一个处理函数：go（跳转页面）/ chatAct（由后端执行的动作）/ openLead（打开线索详情）。"""
    if "actions" not in r:
        r = _row(r)
    meta = r["meta"]
    m = {"id": r["id"], "agent": r["agent"], "from": "me" if r["role"] == "user" else "agent", "time": r["ts"],
         "text": r["content"], "actions": []}
    if meta.get("key"):
        m["key"], m["params"] = meta["key"], meta.get("params") or {}
    for i, a in enumerate(r["actions"]):
        t = a.get("type")
        if t == "goto":
            m["actions"].append({"k": "chat.goto", "p": {"page": f"@nav.{a.get('route')}"}, "act": "go",
                                 "route": a.get("route")})
        elif t == "open_lead":
            m["actions"].append({"k": "chat.openLead", "act": "openLead", "id": a.get("lead_id")})
        elif t == "undo_review":
            m["actions"].append({"k": "nf.undo", "act": "chatAct", "id": f"{r['id']}:{i}",
                                 "done": bool(a.get("done")), "result": a.get("result", "")})
        elif t == "confirm_memo":
            rej = next((j for j, b in enumerate(r["actions"]) if b.get("type") == "reject_memo"
                        and b.get("memo_id") == a.get("memo_id")), None)
            m["card"] = "memo"
            m["memo"] = {"id": a.get("memo_id"), "text": a.get("text", ""),
                         "status": a.get("result") if a.get("done") else "pending",
                         "ok": f"{r['id']}:{i}", "no": f"{r['id']}:{rej}" if rej is not None else ""}
    return m


def history(agent: str, limit=60, after: int = 0):
    if after:
        rows = db.query("SELECT * FROM agent_chats WHERE agent=? AND id>? ORDER BY id LIMIT ?", (agent, after, limit))
        return [_row(r) for r in rows]
    rows = db.query("SELECT * FROM agent_chats WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit))
    return [_row(r) for r in reversed(rows)]


def unread(agent: str | None = None) -> dict:
    seen = settings.get("chat_seen", {}) or {}
    out = {}
    for a in ([agent] if agent else active_keys()):
        out[a] = db.query_one("SELECT COUNT(*) n FROM agent_chats WHERE agent=? AND role='assistant' AND id>?",
                              (a, int(seen.get(a, 0))))["n"]
    return out


@_locked
def mark_seen(agent: str):
    last = db.query_one("SELECT COALESCE(MAX(id),0) m FROM agent_chats WHERE agent=?", (agent,))["m"]
    seen = settings.get("chat_seen", {}) or {}
    seen[agent] = last
    settings.put("chat_seen", seen)


def _insert(agent, role, content, meta=None) -> dict:
    mid = db.execute("INSERT INTO agent_chats(agent, role, content, meta, ts) VALUES(?,?,?,?,?)",
                     (agent, role, content, db.dumps(meta or {}), db.now()))
    return _row(db.query_one("SELECT * FROM agent_chats WHERE id=?", (mid,)))


def _check_actions(actions):
    out = []
    for a in actions or []:
        if a.get("type") not in ACTION_TYPES:
            raise ValueError(f"unknown chat action {a.get('type')}")
        if a["type"] == "goto" and a.get("route") not in ROUTES:
            continue
        out.append(dict(a))
    return out


@_locked
def post(agent: str, text: str, actions=None, user_text: str = "", key: str = "", params=None) -> dict:
    """程序把一件事写进与某个 AI 的对话（不调用模型）。user_text 非空时先记一条客户的话。
    key/params 让前端可按当前界面语言重新渲染。"""
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    if user_text:
        _insert(agent, "user", user_text, {"origin": "app"})
    meta = {"origin": "app", "actions": _check_actions(actions)}
    if key:
        meta.update(key=key, params=params or {})
    return _insert(agent, "assistant", text, meta)


def _page_text(page) -> str:
    """客户当前页面 → 一句话（只给 ID 和少量字段，省 token）。"""
    if not page:
        return ""
    if isinstance(page, str):
        page = {"route": page}
    route = str(page.get("route") or "")[:40]
    s = f"page: {route}"
    lid = page.get("lead_id")
    if lid:
        r = db.query_one("SELECT id, company_name, country, grade, score, status, first_source, profile, "
                         "message_type, intent FROM leads WHERE id=?", (int(lid),))
        if r:
            pf = db.loads(r["profile"], {})
            s += (f"; looking at lead #{r['id']} {r['company_name']} ({r['country']}), grade {r['grade']} "
                  f"score {r['score']}, status {r['status']}, source {r['first_source']}, intent {r['intent'] or '-'}; "
                  f"summary: {str(pf.get('summary') or '')[:200]}; grade reason: {str(pf.get('grade_reason') or '')[:300]}")
    return PAGE_HINT.format(page=s)


def send(agent: str, message: str, page=None) -> dict:
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    if not is_active(agent):
        raise UserError("agent_not_in_v1", agent=f"@agent.{agent}")
    message = (message or "").strip()
    if not message:
        raise UserError("chat_empty")
    if not settings.agent_enabled(agent):
        raise UserError("agent_disabled", agent=f"@agent.{agent}")
    if not llm.configured(agent):
        raise UserError("agent_not_configured", agent=f"@agent.{agent}")
    with _context_lock:
        generation = _generations[agent]
        lang = settings.get("ui_lang")
        prev = history(agent, HISTORY_TURNS * 2)
        convo = "\n".join(f"{'Customer' if r['role'] == 'user' else 'You'}: {r['content']}" for r in prev)
        system = prompts.render("chat.system", role=memory.role(agent),
                                lang="Simplified Chinese" if lang == "zh" else "English", routes=", ".join(ROUTES),
                                work=memory.work_summary(agent), memos=memory.memos_block(agent), page=_page_text(page))
        user = (f"Earlier conversation:\n{convo}\n\n" if convo else "") + f"Customer: {message}"
        umsg = _insert(agent, "user", message, {"page": page} if page else {})
    data, _ = llm.call_json(agent, "agent_chat", system, user, SCHEMA, effort="low",
                            context={"message": message, "agent": agent, "lang": lang, "page": page})
    with _context_lock:
        if generation != _generations[agent]:
            raise UserError("chat_cleared", 409)
        reply = (data.get("reply") or "").strip() or "…"
        memo_ids = [memory.add_memo(agent, m, origin="chat", source_ref=f"chat:{umsg['id']}")
                    for m in data.get("memos") or [] if str(m).strip()]
        memos = [db.query_one("SELECT * FROM memos WHERE id=?", (i,)) for i in memo_ids]
        actions = []
        for m in memos:
            if m:
                actions += [{"type": "confirm_memo", "memo_id": m["id"], "text": m["text"]},
                            {"type": "reject_memo", "memo_id": m["id"]}]
        if data.get("goto") in ROUTES:
            actions.append({"type": "goto", "route": data["goto"]})
        msg = _insert(agent, "assistant", reply, {"memo_ids": memo_ids, "actions": actions})
        return {"reply": reply, "memos": memos, "message": shape(msg), "user_message": shape(umsg)}


@_locked
def do_action(msg_id: int, index: int) -> dict:
    """执行对话里某条回复的第 index 个行内动作（只认白名单类型）。返回更新后的消息 + 结果。"""
    r = db.query_one("SELECT * FROM agent_chats WHERE id=?", (msg_id,))
    if not r:
        raise UserError("chat_action_invalid", 404)
    meta = db.loads(r["meta"], {})
    acts = meta.get("actions") or []
    if not (0 <= index < len(acts)):
        raise UserError("chat_action_invalid", 404)
    a = acts[index]
    if a.get("done") and a["type"] != "goto":
        raise UserError("chat_action_done")
    result = {}
    t = a["type"]
    if t == "goto" or t == "open_lead":
        result = {"goto": a.get("route") or "leads", "lead_id": a.get("lead_id")}
    elif t == "undo_review":
        from .. import review
        result = review.undo(int(a["lead_id"]))
        a["done"] = True
        a["result"] = "undone"
        _insert(r["agent"], "assistant", i18n.t("chat.undone_reply"),
                {"origin": "app", "key": "chat.undone_reply", "params": {}, "actions": []})
    elif t in ("confirm_memo", "reject_memo"):
        memory.decide_memo(int(a["memo_id"]), t == "confirm_memo")
        for b in acts:                    # 同一条备忘录的“确认/不要”两个按钮一起变为已处理
            if b.get("memo_id") == a["memo_id"]:
                b["done"] = True
                b["result"] = "confirmed" if t == "confirm_memo" else "rejected"
    meta["actions"] = acts
    db.execute("UPDATE agent_chats SET meta=? WHERE id=?", (db.dumps(meta), msg_id))
    return {"message": shape(db.query_one("SELECT * FROM agent_chats WHERE id=?", (msg_id,))), "result": result}


def mark_lead_undone(lead_id: int):
    """在审核台 / 线索库撤销后，对话里对应的 [撤销] 按钮也变成“已撤销”。"""
    for r in db.query("SELECT id, meta FROM agent_chats WHERE meta LIKE ?", ('%"undo_review"%',)):
        meta = db.loads(r["meta"], {})
        changed = False
        for a in meta.get("actions") or []:
            if a.get("type") == "undo_review" and int(a.get("lead_id") or 0) == int(lead_id) and not a.get("done"):
                a["done"] = True
                a["result"] = "undone"
                changed = True
        if changed:
            db.execute("UPDATE agent_chats SET meta=? WHERE id=?", (db.dumps(meta), r["id"]))


def _clear_history(agents):
    seen = settings.get("chat_seen", {}) or {}
    for agent in agents:
        db.execute("DELETE FROM agent_chats WHERE agent=?", (agent,))
        seen.pop(agent, None)
    settings.put("chat_seen", seen)


@_locked
def clear(agent: str):
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    with db.transaction():
        _clear_history([agent])
    _generations[agent] += 1


@_locked
def reset_memory(agent: str):
    """Forget conversation and standing memos only; retain business records.

    Shared memos belong to all agents and require an explicit all-agent reset.
    """
    if agent != "all" and agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    agents = list(AGENT_KEYS) if agent == "all" else [agent]
    with db.transaction():
        _clear_history(agents)
        if agent == "all":
            db.execute("DELETE FROM memos")
        else:
            db.execute("DELETE FROM memos WHERE agent=?", (agent,))
    for key in agents:
        _generations[key] += 1
    return {"ok": True, "agents": agents, "shared_memos_retained": agent != "all"}

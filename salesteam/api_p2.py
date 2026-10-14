"""第 2 步 HTTP 接口：TG 账号池 / 群 / 监控、审核台、推送与提醒、AI 记忆与学习、单点对话、快速开始、中转站一键配置、
AI 启用开关、防休眠状态。只监听本机（由 server.py 的中间件把关）。"""
import json
import time

from fastapi import Depends, APIRouter, File, Form, Request, UploadFile

from . import alerts, db, leads, power, push, review, settings, tasks
from .agents import chat as chat_mod
from .agents import learning, memory, monitor, onboarding
from .agents.registry import AGENT_KEYS
from .errors import UserError
from .learn import embed, evolve
from .llm import model_eval, relay
from .tg import joining, pool

router = APIRouter(prefix="/api")
MAX_UPLOAD = 30 * 1024 * 1024


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise UserError("bad_request")
    if not isinstance(data, dict):
        raise UserError("bad_request")
    return data


# ------------------------------------------------------------------ AI 启用开关（PRD 2.1.2）

@router.get("/agents/enabled")
def agents_enabled():
    return settings.get("agents_enabled")


@router.put("/agents/{agent}/enabled")
def set_agent_enabled(agent: str, data: dict = Depends(body)):
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    if agent in ("sales", "followup"):
        raise UserError("agent_not_available", step=3)
    on = bool(data.get("on"))
    e = settings.get("agents_enabled")
    e[agent] = on
    settings.put("agents_enabled", e)
    return e


# ------------------------------------------------------------------ TG 账号池 / 群

@router.get("/source-pool")
def source_pool_state():
    from . import source_pool
    return source_pool.state()


@router.put("/source-pool/settings")
def source_pool_settings(data: dict = Depends(body)):
    from . import source_pool
    return source_pool.set_settings(data)


@router.post("/source-pool")
def source_pool_add(data: dict = Depends(body)):
    from . import source_pool
    return source_pool.add(data)


@router.put("/source-pool/{cid}")
def source_pool_update(cid: int, data: dict = Depends(body)):
    from . import source_pool
    return source_pool.update(cid, data)


@router.get("/social/{platform}/status")
def social_status(platform: str):
    from . import social
    return social.status(platform)


@router.post("/social/{platform}/login")
def social_login(platform: str):
    from . import social
    return social.open_login(platform)


@router.post("/social/{platform}/login/done")
def social_login_done(platform: str):
    from . import social
    return social.finish_login(platform)


@router.delete("/social/{platform}/session")
def social_logout(platform: str):
    from . import social
    return social.clear_session(platform)

@router.get("/tg/config")
def tg_config():
    return {**pool.tg_config(), "warnings": pool.warnings()}


@router.put("/tg/config")
def tg_config_put(data: dict = Depends(body)):
    pool.set_tg_config(data)
    return tg_config()


@router.get("/tg/accounts")
def tg_accounts():
    return {"accounts": pool.list_accounts(), "warnings": pool.warnings(), "backend": settings.get("tg")["backend"]}


@router.post("/tg/accounts")
def tg_add(data: dict = Depends(body)):
    return pool.add_account(data)


@router.put("/tg/accounts/{acc_id}")
def tg_update(acc_id: int, data: dict = Depends(body)):
    return pool.update_account(acc_id, data)


@router.delete("/tg/accounts/{acc_id}")
def tg_delete(acc_id: int):
    pool.delete_account(acc_id)
    return {"ok": True}


@router.post("/tg/accounts/{acc_id}/login")
def tg_login(acc_id: int):
    return pool.start_login(acc_id)


@router.post("/tg/accounts/{acc_id}/code")
def tg_code(acc_id: int, data: dict = Depends(body)):
    d = data
    return pool.submit_code(acc_id, d.get("code") or "", d.get("password") or "")


@router.post("/tg/accounts/{acc_id}/logout")
def tg_logout(acc_id: int):
    """「退出登录」：真正在 TG 退出 + 清本机登录信息（群列表保留）。remote=False → TG 那边没确认，请客户去手机「设备」里结束。"""
    return pool.logout(acc_id)


@router.post("/tg/accounts/{acc_id}/check")
def tg_check(acc_id: int):
    return pool.check_health(acc_id)


@router.post("/tg/accounts/{acc_id}/sync")
def tg_sync(acc_id: int):
    """同步群列表：返回 {total: 一共几个群/频道, added: 这次新增几个}。"""
    return pool.sync_dialogs(acc_id)


@router.post("/tg/accounts/{acc_id}/skip-warmup")
def tg_skip_warmup(acc_id: int):
    """跳过养号（客户确认后，PRD 6.3）。"""
    return pool.skip_warmup(acc_id)


@router.get("/tg/accounts/{acc_id}/info")
def tg_account_info(acc_id: int):
    """账号「详情」：现读一次账号信息（PRD 6.4.1）。"""
    return pool.account_info(acc_id)


@router.get("/tg/chats")
def tg_chats(account_id: int | None = None, monitored: int | None = None):
    return pool.list_chats(account_id, None if monitored is None else bool(monitored))


@router.post("/tg/chats/monitor")
def tg_monitor_set(data: dict = Depends(body)):
    d = data
    return {"changed": pool.set_monitored(d.get("ids") or [], bool(d.get("on")), d.get("icp_id"))}


@router.get("/tg/join")
def tg_join_queue():
    return {"queue": joining.queue(), "assisted": settings.get("joining")["assisted_join"]}


@router.post("/tg/join")
def tg_join(data: dict = Depends(body)):
    d = data
    return {"queued": joining.enqueue(d.get("links") or "", icp_id=d.get("icp_id"))}


@router.delete("/tg/join/{qid}")
def tg_join_cancel(qid: int):
    joining.cancel(qid)
    return {"ok": True}


@router.post("/tg/discover")
def tg_discover(data: dict = Depends(body)):
    d = data
    icp_id = d.get("icp_id") or monitor.default_icp_id()
    if not icp_id:
        raise UserError("task_icp_missing")
    return joining.discover(int(icp_id))


@router.get("/tg/discoveries")
def tg_discoveries(status: str | None = None):
    return joining.discoveries(status)


@router.post("/tg/discoveries/approve")
def tg_disc_approve(data: dict = Depends(body)):
    return {"queued": joining.approve_discoveries(data.get("ids") or [])}


@router.post("/tg/discoveries/reject")
def tg_disc_reject(data: dict = Depends(body)):
    joining.reject_discoveries(data.get("ids") or [])
    return {"ok": True}


@router.get("/tg/outbox")
def tg_outbox():
    return pool.outbox(200)


@router.post("/monitor/run")
def monitor_run():
    """「立即监控一轮」：只在有任务勾选了 TG 群并在运行时可用（PRD 6.4）。"""
    if not tasks.tg_tasks():
        raise UserError("monitor_needs_task")
    if not settings.agent_enabled("monitor"):
        raise UserError("agent_disabled", agent="@agent.monitor")
    return tasks.create_task({"trigger": "manual"}, kind="monitor")


@router.get("/monitor/status")
def monitor_status():
    from . import scheduler
    run = tasks.running("monitor")
    tg = [{"id": t.id, "icp_id": t.icp_id} for t in tasks.tg_tasks()]
    from . import llm
    off = not settings.agent_enabled("monitor")
    return {"running": run.id if run else None, "settings": settings.get("monitor"), "tg_tasks": tg,
            "monitor_ready": not off and llm.configured("monitor"), "monitor_off": off,
            "due": scheduler.due_monitor_accounts() if tg else [], "recent": tasks.list_tasks(10, kind="monitor")}


# ------------------------------------------------------------------ 审核台（PRD 7）

def _f(request: Request):
    q = request.query_params
    return {k: q.get(k) for k in ("country", "grade", "source", "icp_id", "include_later") if q.get(k)}


@router.get("/review")
def review_list(request: Request):
    return review.pending(_f(request))


@router.post("/review/batch")
def review_batch(data: dict = Depends(body)):
    d = data
    return {"approved": review.batch_approve(d.get("ids"), d.get("grade"), d.get("filters") or {})}


@router.post("/review/{lead_id}")
def review_act(lead_id: int, data: dict = Depends(body)):
    d = data
    return review.act(lead_id, d.get("action"), reason=d.get("reason") or "", reasons=d.get("reasons"),
                      note=d.get("note") or "", edits=d.get("edits"), assigned_to=d.get("assigned_to") or "",
                      plan=d.get("plan") or "")


@router.post("/review/{lead_id}/undo")
def review_undo(lead_id: int):
    return review.undo(lead_id)


@router.post("/leads/{lead_id}/deal")
def lead_deal(lead_id: int, data: dict = Depends(body)):
    return review.mark_deal(lead_id, bool(data.get("deal", True)))


@router.post("/leads/{lead_id}/dnc")
def lead_dnc(lead_id: int, data: dict = Depends(body)):
    """永不联系（永久）：标记 + 身份进黑名单（PRD 5.2.2）。"""
    from . import leadpool
    return leadpool.set_do_not_contact(lead_id, bool(data.get("on", True)), (data.get("reason") or "customer")[:40])


# ------------------------------------------------------------------ 线索池管理（PRD 5.2.2）

@router.get("/pool/status")
def pool_status():
    from . import leadpool
    return {**leadpool.status(), "settings": leadpool.cfg()}


@router.post("/pool/run")
def pool_run():
    return tasks.create_task({"trigger": "manual"}, kind="pool")


@router.get("/blacklist")
def blacklist_list():
    return db.query("SELECT * FROM contact_blacklist ORDER BY created_at DESC LIMIT 500")


@router.post("/blacklist/remove")
def blacklist_remove(data: dict = Depends(body)):
    from . import leadpool
    leadpool.blacklist_remove([[data.get("kind"), data.get("value")]])
    return {"ok": True}


@router.put("/leads/{lead_id}")
def lead_update(lead_id: int, data: dict = Depends(body)):
    return review.update_lead(lead_id, data)


# ------------------------------------------------------------------ 推送（PRD 7.1）与提醒（7.2）

@router.get("/notify")
def notify_get():
    acc = pool.notify_account()
    return {"settings": settings.get("notify"), "notify_account": pool.public(acc) if acc else None,
            "configured": push.configured(), "ready": pool.notify_ready()}


@router.put("/notify")
def notify_put(data: dict = Depends(body)):
    d = data
    n = settings.get("notify")
    for k in ("lead_recipients", "alert_recipients"):
        if k in d:
            v = d[k] if isinstance(d[k], list) else str(d[k] or "").replace("，", ",").split(",")
            n[k] = [x.strip() for x in v if str(x).strip()][:20]
    for k in ("push_enabled", "push_on_task_done"):
        if k in d:
            n[k] = bool(d[k])
    if "cutoff" in d:
        import re
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(d["cutoff"])):
            raise UserError("invalid_value", field="cutoff")
        n["cutoff"] = d["cutoff"]
    settings.put("notify", n)
    return notify_get()


@router.post("/notify/push")
def notify_push(data: dict = Depends(body)):
    d = data
    return push.push("manual" if d.get("lead_ids") else "daily", lead_ids=d.get("lead_ids"),
                     recipients=d.get("recipients"))


@router.post("/notify/test")
def notify_test(data: dict = Depends(body)):
    from . import i18n
    d = data
    which = d.get("which", "lead")
    rcps = settings.get("notify")["lead_recipients" if which == "lead" else "alert_recipients"]
    if not rcps:
        raise UserError("notify_recipients_missing")
    if not pool.notify_ready():
        raise UserError("notify_account_unhealthy")
    ok = pool.notify_send(rcps, i18n.t("push.test_msg"), kind="test")
    return {"ok": ok}


@router.get("/alerts")
def alerts_list(status: str | None = None):
    return {"items": alerts.list_alerts(status), **alerts.unread_count()}


@router.get("/alerts/since")
def alerts_since(after: int = 0):
    return {"items": alerts.latest_unread_info(after), **alerts.unread_count()}


@router.post("/alerts/read")
def alerts_read(data: dict = Depends(body)):
    alerts.mark_read(data.get("ids"))
    return alerts.unread_count()


@router.post("/alerts/resolve-all")
def alerts_resolve_all():
    return {"resolved": alerts.resolve_all(), **alerts.unread_count()}


@router.post("/alerts/{aid}/resolve")
def alert_resolve(aid: int):
    r = db.query_one("SELECT key FROM alerts WHERE id=?", (aid,))
    if r:
        alerts.resolve(r["key"])
    return {"ok": True}


# ------------------------------------------------------------------ AI 记忆与学习（PRD 2.5 / 12）

# ------------------------------------------------------------------ 浏览器组件（第 5a 步：第一次打开时后台下载）

@router.get("/browser")
def browser_status():
    from . import browsers
    return browsers.status()


@router.post("/browser/install")
def browser_install():
    from . import browsers
    return browsers.ensure_background(force=True)


# ------------------------------------------------------------------ 渠道池（范围决定 #5）

@router.get("/channels")
def channels_list(request: Request):
    from . import channels
    q = request.query_params
    return {"items": channels.list_all(q.get("kind") or "", q.get("status") or ""), "kinds": channels.KINDS}


@router.put("/channels/{channel_id}")
def channels_update(channel_id: int, data: dict = Depends(body)):
    from . import channels
    return channels.set_status(channel_id, data.get("status") or "")


# ------------------------------------------------------------------ 提示词（PRD 2.4.2：属于客户，可查看和修改）

@router.get("/prompts")
def prompts_list():
    from . import prompts
    return {"version": prompts.VERSION, "items": prompts.list_all()}


@router.put("/prompts/{name}")
def prompts_save(name: str, data: dict = Depends(body)):
    from . import prompts
    return prompts.save(name, data.get("text") or "")


@router.delete("/prompts/{name}")
def prompts_reset(name: str):
    from . import prompts
    return prompts.reset(name)


@router.get("/memory/{agent}")
def memory_layers(agent: str):
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    return memory.layers(agent)


@router.get("/memos")
def memos(agent: str | None = None, status: str | None = None):
    return memory.list_memos(agent, status)


@router.delete("/memory/{agent}")
def memory_clear(agent: str):
    return chat_mod.reset_memory(agent)


@router.post("/memos")
def memo_add(data: dict = Depends(body)):
    d = data
    mid = memory.add_memo(d.get("agent") or "all", d.get("text") or "", origin="manual", status="confirmed")
    return {"id": mid}


@router.post("/memos/{mid}/decide")
def memo_decide(mid: int, data: dict = Depends(body)):
    memory.decide_memo(mid, bool(data.get("confirm")))
    return {"ok": True}


@router.put("/memos/{mid}")
def memo_edit(mid: int, data: dict = Depends(body)):
    memory.edit_memo(mid, data.get("text") or "")
    return {"ok": True}


@router.delete("/memos/{mid}")
def memo_del(mid: int):
    memory.delete_memo(mid)
    return {"ok": True}


@router.get("/learning/overview")
def learning_overview():
    from .llm import decision
    tpls = db.query("SELECT id, name FROM icp_templates ORDER BY updated_at DESC")
    pending = db.query("SELECT k.*, t.name icp_name FROM icp_knowledge k JOIN icp_templates t ON t.id=k.icp_id "
                       "WHERE k.status='pending_confirmation' ORDER BY k.id")
    for p in pending:
        p["content"] = db.loads(p["content"], None)
    from . import quality
    return {"templates": tpls, "pending": pending, "curve": evolve.approval_curve(),
            "quality": quality.curve(), "spot": quality.spot_stats(), "rates": quality.rates(),
            "decision": {**decision.public_config(), "tests": evolve.tests_history()},
            "embed": embed.status(), "memos_pending": len(memory.list_memos(status="pending"))}


@router.get("/learning/{icp_id}")
def learning_icp(icp_id: int):
    return {"knowledge": learning.knowledge_history(icp_id), "samples": learning.list_samples(icp_id),
            "effective": learning.effective(icp_id)}


@router.post("/learning/knowledge/{kid}/decide")
def knowledge_decide(kid: int, data: dict = Depends(body)):
    learning.decide_knowledge(kid, bool(data.get("accept")))
    return {"ok": True}


@router.post("/learning/{icp_id}/rollback")
def knowledge_rollback(icp_id: int, data: dict = Depends(body)):
    return {"rolled_back": learning.rollback(icp_id, int(data.get("version") or 0))}


@router.post("/learning/{icp_id}/undo")
def knowledge_undo(icp_id: int, data: dict = Depends(body)):
    """AI 记忆里「撤销」AI 最近一次自动加的词 / 排除项（U32：这里只读 + 撤销，改词在模板管理里）。"""
    kind = data.get("kind")
    if kind not in ("keywords", "exclusions"):
        raise UserError("invalid_value", field="kind")
    return {"undone": learning.undo_latest(icp_id, kind)}


@router.post("/learning/{icp_id}/evolve")
def knowledge_evolve(icp_id: int):
    return evolve.evolve(icp_id, force=True)


@router.post("/decision/retest")
def decision_retest():
    return evolve.retest(force=True)


@router.post("/embed/download")
def embed_download():
    embed.download()
    return embed.status()


@router.get("/embed/status")
def embed_status():
    return embed.status()


# ------------------------------------------------------------------ 单点对话（PRD 2.1.3）

@router.get("/workbench")
def workbench_overview():
    """工作台：目标 · 费用 · 6 个 AI 的状态（v2 设计）。"""
    from . import workbench
    return workbench.overview()


@router.get("/workbench/feed")
def workbench_feed(agent: str = "", after: float = 0, limit: int = 30):
    from . import workbench
    return workbench.feed(agent or None, after, limit)


@router.get("/chat/unread")
def chat_unread():
    """各 AI 未读回复数（主管抽屉角标）。"""
    return chat_mod.unread()


@router.post("/chat/action/{msg_id}/{index}")
def chat_action(msg_id: int, index: int):
    """执行回复里的行内动作：撤销 / 去某页面 / 确认或不要备忘录。"""
    return chat_mod.do_action(msg_id, index)


@router.get("/chat/{agent}")
def chat_history(agent: str, after: int = 0, limit: int = 60):
    """after>0：只取新消息（工作台主对话 / 抽屉轮询用）。"""
    msgs = [chat_mod.shape(m) for m in chat_mod.history(agent, limit=max(1, min(limit, 200)), after=after)]
    if after:
        return {"messages": msgs}
    return {"messages": msgs, "memos": memory.list_memos(agent)}


@router.post("/chat/{agent}")
def chat_send(agent: str, data: dict = Depends(body)):
    """page：客户当前所在页面，如 {"route": "review", "lead_id": 12}（可选）。"""
    return chat_mod.send(agent, data.get("message") or "", page=data.get("page"))


@router.post("/chat/{agent}/seen")
def chat_seen(agent: str):
    chat_mod.mark_seen(agent)
    return {"ok": True}


@router.delete("/chat/{agent}")
def chat_clear(agent: str):
    chat_mod.clear(agent)
    return {"ok": True}


@router.delete("/chat/{agent}/messages/{msg_id}")
def chat_delete_message(agent: str, msg_id: int):
    chat_mod.delete_message(agent, msg_id)
    return {"ok": True}


# ------------------------------------------------------------------ 快速开始（PRD 3.0）

@router.get("/quickstart")
def qs_state():
    from . import llm
    ob = onboarding.latest()
    rs = settings.get("relay_state", {}) or {}
    return {"first_launch": onboarding.first_launch(), "latest": ob, "relays": relay.public_relays(),
            "relay_presets": [p for p in relay.presets()["relay_presets"]],
            "relay_state": {"provisional": rs.get("provisional", False), "bootstrapped": bool(rs.get("advisor"))},
            "model_eval": _me_public(), "company": settings.get("company_profile", {}) or {},
            "versions": onboarding.versions()[:10], "agreement": settings.get("agreement", {}) or {},
            "ai_ready": any(llm.get_config(a).provider in ("relay", "mock") or llm.get_config(a).api_key
                            for a in ("manager",)),
            "skipped": list((settings.get("onboarding_skips", {}) or {}).get("steps", []))}


@router.post("/quickstart/skip")
def qs_skip(data: dict = Depends(body)):
    """快速开始：除了用户协议，每一步都可以跳过（PRD 3.0）。跳过“读资料”或“确认”= 先进软件，
    以后在“重新开始设置”或和主管对话里补；跳过“配置 AI”= 以后在 设置 → AI 模型 里配（之前 AI 功能用不了，工作台有提示）。"""
    step = data.get("step")
    if step not in ("ai", "read", "interview", "confirm"):
        raise UserError("invalid_value", field="step")
    sk = settings.get("onboarding_skips", {}) or {}
    steps = list(dict.fromkeys(list(sk.get("steps") or []) + [step]))
    settings.put("onboarding_skips", {"steps": steps})
    if step in ("read", "confirm"):
        ob = settings.get("onboarding")
        ob["done"] = True
        settings.put("onboarding", ob)
    if step == "read":
        cur = onboarding.latest()
        if cur and cur["status"] in ("reading", "queued", "failed", "robots_blocked"):
            onboarding.cancel(cur["id"])      # 正在读 / 读失败的那次不再挡在快速开始里
    return {"skipped": steps, "first_launch": onboarding.first_launch()}


@router.post("/quickstart/agree")
def qs_agree():
    import time as _t
    settings.put("agreement", {"accepted_at": _t.time(), "version": "2026-10"})
    return {"ok": True}


@router.post("/quickstart/start")
def qs_start(website: str = Form(""), intro: str = Form(""), oneliner: str = Form(""), redo: str = Form(""),
                   files: list[UploadFile] | None = File(None)):
    pdfs = []
    for f in files or []:
        data = f.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise UserError("file_too_large", max="30 MB")
        if not data.startswith(b"%PDF"):
            raise UserError("pdf_unreadable")
        pdfs.append((f.filename or "catalog.pdf", data))
    return onboarding.start({"website": website, "intro": intro, "oneliner": oneliner}, pdfs, redo=bool(redo))


@router.get("/quickstart/{ob_id}")
def qs_get(ob_id: int):
    from .agents import site_login
    d = onboarding.get(ob_id)
    d["diff"] = onboarding.diff(ob_id) if d["inputs"].get("redo") else []
    if d["login"].get("host"):
        d["login"]["session"] = bool(site_login.cookies_for(d["login"]["host"]))
    return d


@router.post("/quickstart/{ob_id}/retry")
def qs_retry(ob_id: int):
    return onboarding.retry(ob_id)


@router.post("/quickstart/{ob_id}/cancel")
def qs_cancel(ob_id: int):
    return onboarding.cancel(ob_id)


@router.post("/quickstart/{ob_id}/answer")
def qs_answer(ob_id: int, data: dict = Depends(body)):
    d = data
    return onboarding.answer(ob_id, d.get("message") or "", skip=bool(d.get("skip")))


@router.put("/quickstart/{ob_id}/profile")
def qs_profile(ob_id: int, data: dict = Depends(body)):
    return onboarding.update_profile(ob_id, data)


@router.post("/quickstart/{ob_id}/confirm")
def qs_confirm(ob_id: int, data: dict = Depends(body)):
    d = data
    out = onboarding.confirm(ob_id)
    # 模型方案与画像一起确认（PRD 3.0 并行）
    tier = d.get("tier")
    st = model_eval.state()
    if tier and st.get("status") == "done":
        out["models"] = model_eval.apply(tier)
    out["model_status"] = model_eval.state().get("status")
    from . import edition
    if edition.current()["id"] == "my_igaming":
        from .agents import preparation
        out["preparation"] = preparation.request(out["icp_id"])
    return out


@router.post("/quickstart/rollback")
def qs_rollback(data: dict = Depends(body)):
    return onboarding.rollback(int(data.get("version") or 0))


@router.post("/quickstart/{ob_id}/login")
def qs_login(ob_id: int, data: dict = Depends(body)):
    from .agents import site_login
    d = data
    if not settings.get("onboarding_flags", {}).get("site_login"):
        raise UserError("risk_confirm_required")
    return site_login.open_window(ob_id, d.get("url") or "")


@router.post("/quickstart/{ob_id}/login/done")
def qs_login_done(ob_id: int):
    from .agents import site_login
    return site_login.finish(ob_id)


@router.post("/quickstart/{ob_id}/login/clear")
def qs_login_clear(ob_id: int):
    from .agents import site_login
    return site_login.clear(ob_id)


@router.get("/company")
def company_get():
    return settings.get("company_profile", {}) or {}


@router.put("/company")
def company_put(data: dict = Depends(body)):
    d = data
    c = settings.get("company_profile", {}) or {}
    # Validate the entire patch before writing: a bad field must not save any other edits.
    fields = ("name", "summary", "oem_odm", "advantages", "certifications", "factory", "cases", "markets",
              "moq", "price_range", "payment_terms", "lead_time", "competitors")
    patch = {}
    for field in fields:
        if field not in d:
            continue
        value = d[field]
        if not isinstance(value, str) or len(value) > 2000 or (field == "summary" and not value.strip()):
            raise UserError("invalid_value", field=field)
        old = c.get(field)
        old = old if isinstance(old, dict) else {}
        sources = old.get("sources")
        sources = [v for v in sources if isinstance(v, str)] if isinstance(sources, list) else []
        patch[field] = {**old, "text": value.strip(), "sources": list(dict.fromkeys(["input:customer", *sources]))}
    if "product_lines" in d:
        values = d["product_lines"]
        if not isinstance(values, list) or len(values) > 50 or any(
                not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
            raise UserError("invalid_value", field="product_lines")
        previous = {p["name"]: p for p in (c.get("product_lines") or [])
                    if isinstance(p, dict) and isinstance(p.get("name"), str)}
        patch["product_lines"] = []
        for value in values:
            old = previous.get(value.strip(), {})
            sources = old.get("sources")
            sources = [v for v in sources if isinstance(v, str)] if isinstance(sources, list) else []
            patch["product_lines"].append({**old, "name": value.strip(),
                                           "sources": list(dict.fromkeys(["input:customer", *sources]))})
    if "signature" in d:
        patch["signature"] = str(d["signature"] or "").strip()[:1000]
    c = {**c, **patch}
    if patch:
        c["updated_at"] = time.time()
    settings.put("company_profile", c)
    return c


# ------------------------------------------------------------------ 中转站一键配置（PRD 2.4.1）

def _me_public():
    s = model_eval.state()
    return {k: s.get(k) for k in ("status", "estimate_usd", "actual_usd", "recommendation", "applied_tier", "error",
                                  "connectivity", "task_id")}


@router.get("/relay")
def relay_get():
    return {"relays": relay.public_relays(), "presets": relay.presets()["relay_presets"], "eval": _me_public(),
            "fallbacks": settings.get("model_fallbacks", {}) or {}}


@router.post("/relay")
def relay_add(data: dict = Depends(body)):
    """添加 API / 中转站（PRD 3.0 / 2.4.1）。
    - 带 role：给这个 AI 用上（快速开始：role=manager；设置里某个 AI 的「自己的 API」：role=那个 AI）
    - 不带 role（设置 → AI 模型 →「添加 API」）：**只加到列表里，不改任何 AI 的模型（包括主管）**——0.2.2 之前这里默认
      给主管换成新 API 的模型，可能悄悄换到更贵的模型。新 API 的模型只作为备用 / 切换的候选：已配置的 AI 里，
      还没有备用、或备用和主模型是同一家厂商的，重新挑一次备用（不做实测，不花钱）。"""
    from .agents.registry import AGENT_KEYS
    d = data
    role = d.get("role") or ""
    if role and role not in AGENT_KEYS and role != "decision":
        raise UserError("unknown_agent", 404, agent=role)
    known = {r["id"] for r in relay.relays()}
    info = relay.add_relay(d)
    if role == "decision":
        # 判断模型（PRD 2.3）：和其他 AI 一样配 API + 模型；没填模型 → 这个 API 上最便宜、支持结构化输出的。
        # 新加的 API 和其他 API 一样只是备用候选，不改任何 AI 的模型
        from .llm import decision
        conf = decision.configure(info["id"], (relay.clean_models(d.get("model") or d.get("models")) or [""])[0])
        return {"relay": info, "configured": {"agent": "decision", "model": conf["model"], "norm": conf["model"]},
                "backups": _refresh_backups() if info["id"] not in known else {}}
    if role:
        conf = relay.configure_agent(role, info["id"], (relay.clean_models(d.get("model") or d.get("models")) or [""])[0])
        return {"relay": info, "configured": conf}
    return {"relay": info, "configured": None, "backups": _refresh_backups()}


def _refresh_backups() -> dict:
    """新加了 API 之后：已配置的 AI 里，没有备用、或备用和主模型同一家厂商的，重新挑备用（候选包含所有 API 的模型）。"""
    from . import llm
    from .agents.registry import active_keys
    from .llm import backup
    out = {}
    for a in active_keys():
        try:
            if not llm.configured(a):
                continue
            cfg = llm.get_config(a)
            if cfg.provider == "mock":
                continue
            fb = backup.get(a)
            if fb and fb.get("model") and relay.vendor_of(fb["model"]) != relay.vendor_of(cfg.model):
                continue                                  # 已经有不同厂商的备用：不动
            new = backup.auto_assign(a, check=False)
            out[a] = (new or {}).get("model")
        except UserError:
            continue
    return out


@router.post("/agents/{agent}/same-as-manager")
def agent_same_as_manager(agent: str, data: dict = Depends(body)):
    """「和主管一样」：用主管的 API（同一个中转站 / 协议 / Key），模型按规则挑或用客户填的。"""
    from . import llm
    from .agents.registry import AGENT_KEYS
    if agent not in AGENT_KEYS and agent not in ("decision", "unconfigured"):
        raise UserError("unknown_agent", 404, agent=agent)
    m = llm.get_config("manager")
    if not llm.configured("manager"):
        raise UserError("agent_not_configured", agent="@agent.manager")
    if agent == "unconfigured":
        # U47「没配置的 AI 都用主管的 API」：一次配完（模型按规则挑；判断模型是选用的，不在里面）
        from .agents.registry import active_keys
        done = []
        for k in active_keys():
            if k == "manager" or llm.configured(k):
                continue
            if m.provider == "relay":
                r = relay.configure_agent(k, m.preset, "")
            else:
                llm.update_config(k, {"provider": m.provider, "preset": m.preset or "", "base_url": m.base_url or "",
                                      "api_key": m.api_key or "", "model": m.model})
                r = {"agent": k, "model": llm.get_config(k).model}
            done.append({"agent": k, "model": r.get("model") if isinstance(r, dict) else ""})
        return {"configured": done}
    if agent == "decision":
        # 判断模型「和主管一样」：用主管的 API；没填模型 → 这个 API 上最便宜、支持结构化输出的模型
        from .llm import decision
        if m.provider != "relay":
            raise UserError("relay_not_configured")
        conf = decision.configure(m.preset, (relay.clean_models(data.get("model")) or [""])[0])
        return {"agent": "decision", "model": conf["model"], "norm": conf["model"]}
    if m.provider == "relay":
        return relay.configure_agent(agent, m.preset, (relay.clean_models(data.get("model")) or [""])[0])
    llm.update_config(agent, {"provider": m.provider, "preset": m.preset or "", "base_url": m.base_url or "",
                              "api_key": m.api_key or "", "model": data.get("model") or m.model})
    return {"agent": agent, "model": llm.get_config(agent).model}


@router.post("/agents/{agent}/pick")
def agent_pick(agent: str, data: dict = Depends(body)):
    """「让主管帮我选模型」：主管在这个 AI 的 API 上实测（够用就好：门槛 → 价格 / 速度 / 拒答），选完自动用上主模型 + 拒答最少的备用。"""
    from . import llm
    from .agents.registry import AGENT_KEYS
    if agent not in AGENT_KEYS and agent != "decision":
        raise UserError("unknown_agent", 404, agent=agent)
    if agent == "decision":
        from .llm import decision
        dc = decision.get_config()
        rid = dc.get("relay", "") if decision.configured(dc) and dc.get("provider") == "relay" else ""
    else:
        cfg = llm.get_config(agent)
        rid = cfg.preset if cfg.provider == "relay" else ""
    if not rid:
        mg = llm.get_config("manager")
        rid = mg.preset if mg.provider == "relay" else ""
    if not rid:
        raise UserError("relay_not_configured")
    if tasks.running("model_eval"):
        raise UserError("task_already_running")
    t = tasks.create_task({"roles": [agent], "relay": rid, "confirmed": True}, kind="model_eval")
    model_eval._put(task_id=t["id"], status="shortlisting", recommendation=None, applied_tier=None)
    return {"task_id": t["id"], "relay": rid}


@router.post("/relay/models")
def relay_models(data: dict = Depends(body)):
    """给主管配置 API：先读模型列表（不保存），让客户选主管用的模型。"""
    return {"models": relay.list_remote(data)}


@router.delete("/relay/{relay_id}")
def relay_del(relay_id: str):
    relay.remove_relay(relay_id)
    return {"ok": True}


@router.post("/relay/evaluate")
def relay_evaluate(data: dict = Depends(body)):
    """重新评估模型（第 3 步由销售主管建议，客户一键同意）；或预计费用 > $1 时客户确认后继续。"""
    d = data
    if tasks.running("model_eval"):
        raise UserError("task_already_running")
    t = tasks.create_task({"confirmed": bool(d.get("confirmed"))}, kind="model_eval")
    model_eval._put(task_id=t["id"], status="shortlisting")
    return {"task_id": t["id"]}


@router.post("/relay/apply")
def relay_apply(data: dict = Depends(body)):
    return model_eval.apply(data.get("tier") or "balanced")


@router.put("/relay/prices")
def relay_prices(data: dict = Depends(body)):
    """价格未知的模型：客户可选填单价（美元/百万 token）。"""
    d = data
    p = settings.get("relay_prices", {}) or {}
    key = f"{d.get('relay')}::{d.get('model')}"
    if d.get("price_in") in (None, ""):
        p.pop(key, None)
    else:
        p[key] = [float(d["price_in"]), float(d.get("price_out") or 0)]
    settings.put("relay_prices", p)
    return p


# ------------------------------------------------------------------ 防休眠（PRD 9.1）

@router.get("/power")
def power_status():
    return power.status()


__all__ = ["router", "leads"]

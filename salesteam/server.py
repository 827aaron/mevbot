"""HTTP 接口（FastAPI）。只监听 127.0.0.1；拒绝外部网站跨域调用和 DNS 重绑定。"""
import asyncio
import os
import json
import re
import time

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import APP_NAME, APP_TAGLINE, VERSION, background, countries, db, edition, leads, llm, paths, settings, tasks, vault
from .agents import icp as icp_mod
from .agents import learning
from .agents.registry import AGENT_KEYS, active_agents, active_keys
from .errors import UserError
from .llm import decision, providers
from .sources import search as search_mod
from .sources import youtube as yt

app = FastAPI(title=APP_NAME["en"], version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
_LOCAL_HOST = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
_LOCAL_ORIGIN = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")
MAX_UPLOAD = 30 * 1024 * 1024


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if not _LOCAL_HOST.match(host):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and not _LOCAL_ORIGIN.match(origin):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/static/") and "cache-control" not in resp.headers:
        # 界面文件每次都向本机服务确认一下（没变就是 304，很快）：装了新版本后窗口不会继续用缓存里的旧界面代码
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(UserError)
async def user_error(_request: Request, exc: UserError):
    return JSONResponse(exc.to_dict(), status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected(_request: Request, exc: Exception):
    import traceback
    traceback.print_exception(exc)
    return JSONResponse({"error": "unexpected", "params": {"detail": str(exc)[:200]}}, status_code=500)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise UserError("bad_request")
    if not isinstance(data, dict):
        raise UserError("bad_request")
    return data


def ui_lang():
    return settings.get("ui_lang")


# ------------------------------------------------------------------ 基础

@app.get("/api/meta")
def meta():
    return {
        "version": VERSION,
        "app": {"name": APP_NAME, "tagline": APP_TAGLINE},
        "edition": edition.current(),
        "ui_lang": ui_lang(),
        "agents": active_agents(),          # v1：不含销售员 / 跟进员（FEATURES["outreach"]）
        "providers": providers.public_catalog(),
        "geo": countries.as_json(),
        "customer_types": icp_mod.CUSTOMER_TYPES,
        "sources": tasks.SOURCES,
        "feedback_reasons": learning.REASONS,
        "data_dir": str(paths.DATA_DIR),
        "budget": llm.budget_state(),
        "first_launch": _first_launch(),
        "search_guide": _search_guide(),
        "setup_needed": not llm.configured("manager"),     # 还没给主管配置 API（快速开始可以跳过这一步）
        "agents_enabled": settings.get("agents_enabled"),
    }


def _first_launch():
    from .agents import onboarding
    return onboarding.first_launch()


def _search_guide():
    """首次使用引导：免费搜索兜底不稳定，建议配置 Serper / Brave Key，或让调研员用 Claude 自带联网搜索。"""
    if search_mod.which_provider() != "free":
        return False
    cfg = llm.get_config("researcher")
    return not (cfg.provider == "claude" or (cfg.provider == "relay" and "claude" in (cfg.model or "")))


@app.post("/api/settings/ui")
def set_ui(data: dict = Depends(body)):
    lang = data.get("lang")
    if lang not in ("zh", "en"):
        raise UserError("invalid_value", field="lang")
    settings.put("ui_lang", lang)
    return {"ui_lang": lang}


@app.get("/api/diagnostics/export")
def export_diagnostics():
    from . import diagnostics
    filename, content = diagnostics.build()
    return Response(content, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
    })


@app.get("/api/edition")
def get_edition():
    return {**edition.current(), "presets": edition.presets()}


@app.post("/api/edition/presets")
def apply_edition_presets(data: dict = Depends(body)):
    return {"templates": edition.apply_presets(keys=data.get("keys"), confirmed=data.get("confirmed", False),
                                                product=data.get("product"))}


# ------------------------------------------------------------------ 智能体模型设置 / 用量

@app.get("/api/agents/config")
def agents_config():
    dash = {d["agent"]: d for d in llm.usage_dashboard()["agents"]}
    return [{**llm.public_config(k), "usage": dash[k]} for k in active_keys()]


@app.put("/api/agents/{agent}/config")
def put_agent(agent: str, data: dict = Depends(body)):
    before = llm.public_config(agent)
    llm.update_config(agent, data)
    after = llm.public_config(agent)
    # 客户手动换了主模型：自动选一个备用模型（拒答最少 → 能力 → 价格 → 不同厂商），PRD 2.4.1
    if any(before.get(k) != after.get(k) for k in ("provider", "preset", "model")):
        from .llm import backup
        try:
            backup.auto_assign(agent)
        except UserError:
            pass
        after = llm.public_config(agent)
    return after


@app.put("/api/agents/{agent}/fallback")
def put_agent_fallback(agent: str, data: dict = Depends(body)):
    """客户自己改备用模型；auto=true 时重新自动选。"""
    from .llm import backup
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    if data.get("auto"):
        backup.auto_assign(agent)
    else:
        if not data.get("relay") or not data.get("model"):
            raise UserError("invalid_value", field="model")
        backup.set_manual(agent, data["relay"], data["model"])
    return llm.public_config(agent)


@app.get("/api/agents/{agent}/fallback/options")
def agent_fallback_options(agent: str):
    from .llm import backup
    if agent not in AGENT_KEYS:
        raise UserError("unknown_agent", 404, agent=agent)
    cfg = llm.get_config(agent)
    return [{"relay": m["relay"], "id": m["id"], "vendor": m.get("vendor", ""), "price_in": m.get("price_in"),
             "price_out": m.get("price_out")} for m in backup.candidates(agent, cfg.model)[:40]]


@app.post("/api/agents/{agent}/test")
def test_agent(agent: str):
    if agent == "decision":                    # 判断模型这一行的「测试」：答一道选择题
        t0 = time.time()
        r = decision.test()
        return {"ok": True, **r, "ms": int((time.time() - t0) * 1000)}
    return {"ok": True, **llm.test_agent(agent)}


@app.post("/api/agents/{agent}/models")
def agent_models(agent: str, data: dict = Depends(body)):
    overrides = {k: data.get(k) for k in ("provider", "preset", "base_url", "api_key") if data.get(k)}
    return {"models": llm.list_models(agent, overrides)}


@app.post("/api/agents/apply-all")
def apply_all(data: dict = Depends(body)):
    """一个 Key 应用到全部智能体：厂商 + 接口地址 + Key（+ 可选模型）。监控员保留便宜模型以外的设置由客户自行调整。"""
    provider = data.get("provider")
    if provider not in providers.PROVIDERS:
        raise UserError("invalid_value", field="provider")
    upd = {"provider": provider, "preset": data.get("preset", ""), "base_url": data.get("base_url", "")}
    if data.get("api_key"):
        upd["api_key"] = data["api_key"]
    for k in AGENT_KEYS:
        u = dict(upd)
        if data.get("model"):
            u["model"] = data["model"]
        elif provider == "claude":
            from .agents.registry import DEFAULTS
            u["model"] = DEFAULTS[k]["model"]
        elif provider == "mock":
            u["model"] = "mock-model"
        llm.update_config(k, u)
    return agents_config()


@app.get("/api/usage")
def usage():
    return llm.usage_dashboard()


@app.get("/api/decision")
def get_decision():
    return decision.public_config()


@app.put("/api/decision")
def put_decision(data: dict = Depends(body)):
    return decision.update_config(data)


@app.post("/api/decision/test")
def test_decision(data: dict = Depends(body)):
    return {"ok": True, **decision.test(data)}


@app.put("/api/budget")
def put_budget(data: dict = Depends(body)):
    llm.set_budget(data.get("daily"), data.get("monthly"))
    return llm.usage_dashboard()


# ------------------------------------------------------------------ 数据源设置

def _sources_public():
    return {
        "search": settings.get("search"),
        "crawler": settings.get("crawler"),
        "youtube": settings.get("youtube"),
        "analyst": settings.get("analyst"),
        "keys": {n: {"set": bool(settings.get_secret(n)), "hint": vault.mask(settings.get_secret(n))}
                 for n in ("serper_api_key", "brave_api_key", "youtube_api_key")},
        "active_search": search_mod.which_provider(),
        "risk_log": settings.risk_log(),
    }


@app.get("/api/settings/sources")
def get_sources():
    return _sources_public()


def _num(v, lo, hi, field, cast=float):
    try:
        v = cast(v)
    except (TypeError, ValueError):
        raise UserError("invalid_value", field=field)
    if not lo <= v <= hi:
        raise UserError("value_range", field=field, min=lo, max=hi)
    return v


@app.put("/api/settings/sources")
def put_sources(data: dict = Depends(body)):
    if "search" in data:
        s = settings.get("search")
        d = data["search"]
        if "provider" in d:
            if d["provider"] not in ("auto", "claude", "serper", "brave", "free"):
                raise UserError("invalid_value", field="search.provider")
            s["provider"] = d["provider"]
        if "results_per_query" in d:
            s["results_per_query"] = _num(d["results_per_query"], 1, 20, "results_per_query", int)
        if "queries_per_source" in d:
            s["queries_per_source"] = _num(d["queries_per_source"], 1, 30, "queries_per_source", int)
        settings.put("search", s)
    if "crawler" in data:
        c = settings.get("crawler")
        d = data["crawler"]
        if "delay_seconds" in d:
            c["delay_seconds"] = _num(d["delay_seconds"], 1, 60, "delay_seconds")
        if "max_pages_per_task" in d:
            c["max_pages_per_task"] = _num(d["max_pages_per_task"], 1, 1000, "max_pages_per_task", int)
        if "max_pages_per_site" in d:
            c["max_pages_per_site"] = _num(d["max_pages_per_site"], 1, 50, "max_pages_per_site", int)
        if "daily_page_limit" in d:
            c["daily_page_limit"] = _num(d["daily_page_limit"], 1, 20000, "daily_page_limit", int)
        if "stealth_mode" in d and bool(d["stealth_mode"]) != bool(c.get("stealth_mode", True)):
            c["stealth_mode"] = bool(d["stealth_mode"])          # 普通开关，只在本机记录开关时间
            settings.log_risk("crawler.stealth_mode", c["stealth_mode"], "settings")
        settings.put("crawler", c)
    if "youtube" in data:
        y = settings.get("youtube")
        d = data["youtube"]
        if "max_videos_per_query" in d:
            y["max_videos_per_query"] = _num(d["max_videos_per_query"], 1, 25, "max_videos_per_query", int)
        if "max_comments_per_video" in d:
            y["max_comments_per_video"] = _num(d["max_comments_per_video"], 0, 100, "max_comments_per_video", int)
        settings.put("youtube", y)
    if "analyst" in data:
        a = settings.get("analyst")
        d = data["analyst"]
        if "enrich_website" in d:
            a["enrich_website"] = bool(d["enrich_website"])
        if "concurrency" in d:
            a["concurrency"] = _num(d["concurrency"], 1, 4, "concurrency", int)
        settings.put("analyst", a)
    for name in ("serper_api_key", "brave_api_key", "youtube_api_key"):
        if name in data.get("keys", {}):
            settings.set_secret(name, vault.clean_secret(data["keys"][name]))
    return _sources_public()


@app.post("/api/settings/sources/test")
def test_source(data: dict = Depends(body)):
    which = data.get("which")
    key = (data.get("key") or "").strip() or None
    if which == "youtube":
        return {"ok": True, **yt.test(key)}
    return {"ok": True, **search_mod.test(which, key)}


RISK_FEATURES = [("assisted_join", "joining.assisted_join"), ("site_login", "onboarding.site_login"),
                 ("own_site_ignore_robots", "onboarding.ignore_robots"), ("ignore_robots", "crawler.respect_robots_off")]
# 隐身模式（0.2.2）：测试阶段默认开启、按需使用；是普通开关（设置 → 数据来源），开 / 关都不需要风险确认


@app.get("/api/settings/risk")
def risk_state():
    """所有高风险开关的当前状态 + 最近一次开启时间（设置 → 高级 → 安全与风险）。"""
    c, j, f = settings.get("crawler"), settings.get("joining"), settings.get("onboarding_flags", {}) or {}
    on = {"assisted_join": bool(j.get("assisted_join")), "site_login": bool(f.get("site_login")),
          "own_site_ignore_robots": bool(f.get("ignore_robots")), "ignore_robots": not c.get("respect_robots", True),
          "stealth_mode": bool(c.get("stealth_mode"))}
    log = settings.risk_log()
    out = []
    for key, logkey in RISK_FEATURES:
        last = next((r["ts"] for r in log if r["feature"] == logkey and r["enabled"]), None)
        out.append({"key": key, "on": on[key], "at": last})
    return {"features": out}


@app.post("/api/settings/risk")
def set_risk(data: dict = Depends(body)):
    """高风险开关：开启必须带 confirm=true（界面上用户勾选“我已了解风险，自行承担”），并在本地记录。"""
    feature = data.get("feature")
    enable = bool(data.get("enable"))
    c = settings.get("crawler")
    if feature == "stealth_mode":
        c["stealth_mode"] = enable
        settings.log_risk("crawler.stealth_mode", enable, data.get("note", ""))
    elif feature == "ignore_robots":
        if enable and not data.get("confirm"):
            raise UserError("risk_confirm_required")
        c["respect_robots"] = not enable
        settings.log_risk("crawler.respect_robots_off", enable, data.get("note", ""))
    elif feature == "assisted_join":
        if enable and not data.get("confirm"):
            raise UserError("risk_confirm_required")
        j = settings.get("joining")
        j["assisted_join"] = enable
        settings.put("joining", j)
        settings.log_risk("joining.assisted_join", enable, data.get("note", ""))
    elif feature in ("site_login", "own_site_ignore_robots"):
        if enable and not data.get("confirm"):
            raise UserError("risk_confirm_required")
        f = settings.get("onboarding_flags", {}) or {}
        f["site_login" if feature == "site_login" else "ignore_robots"] = enable
        settings.put("onboarding_flags", f)
        settings.log_risk("onboarding.site_login" if feature == "site_login" else "onboarding.ignore_robots", enable,
                          data.get("note", ""))
    else:
        raise UserError("invalid_value", field="feature")
    settings.put("crawler", c)
    return _sources_public()


# ------------------------------------------------------------------ 理想客户模板

@app.get("/api/icps")
def icps():
    return icp_mod.list_all()


@app.get("/api/icps/{icp_id}")
def icp_get(icp_id: int):
    return icp_mod.get(icp_id)


@app.post("/api/icps")
def icp_create(data: dict = Depends(body)):
    return icp_mod.save(data.get("data") or {}, reasons=data.get("reasons"), chat_log=data.get("chat"),
                        origin=data.get("origin") or "manual")


@app.put("/api/icps/{icp_id}")
def icp_update(icp_id: int, data: dict = Depends(body)):
    return icp_mod.save(data.get("data") or {}, icp_id=icp_id, reasons=data.get("reasons"), chat_log=data.get("chat"))


@app.post("/api/icps/{icp_id}/copy")
def icp_copy(icp_id: int):
    return icp_mod.copy(icp_id, " (副本)" if ui_lang() == "zh" else " (copy)")


@app.delete("/api/icps/{icp_id}")
def icp_delete(icp_id: int):
    icp_mod.delete(icp_id)
    return {"ok": True}


@app.post("/api/icps/generate")
def icp_generate(mode: str = Form(...), text: str = Form(""), url: str = Form(""),
                 file: UploadFile | None = File(None)):
    pdf = None
    if mode == "pdf":
        if file is None:
            raise UserError("icp_input_missing")
        pdf = file.file.read(MAX_UPLOAD + 1)
        if len(pdf) > MAX_UPLOAD:
            raise UserError("file_too_large", max="30 MB")
        if not pdf.startswith(b"%PDF"):
            raise UserError("pdf_unreadable")
    if mode not in ("website", "text", "pdf"):
        raise UserError("invalid_value", field="mode")
    return icp_mod.generate(mode, text=text, url=url, pdf=pdf, lang=ui_lang())


@app.post("/api/icps/chat")
def icp_chat(data: dict = Depends(body)):
    return icp_mod.chat(data.get("icp") or {}, data.get("message") or "", data.get("history") or [], lang=ui_lang())


# ---- AI 学习：样本库（PRD 第 12 节）

@app.get("/api/icps/{icp_id}/samples")
def samples(icp_id: int):
    icp_mod.get(icp_id)
    return learning.list_samples(icp_id)


@app.post("/api/icps/{icp_id}/samples")
def add_sample(icp_id: int, data: dict = Depends(body)):
    pol = 1 if data.get("polarity") in (1, "1", "positive") else -1
    sid = learning.add_sample(icp_id, pol, "manual_paste", data.get("text") or "", reason=data.get("reason") or "",
                              note=data.get("note") or "")
    return {"id": sid}


@app.delete("/api/samples/{sample_id}")
def del_sample(sample_id: int):
    learning.delete_sample(sample_id)
    return {"ok": True}


@app.get("/api/icps/{icp_id}/knowledge")
def knowledge(icp_id: int):
    return learning.knowledge_history(icp_id)


# ------------------------------------------------------------------ 任务

@app.post("/api/tasks")
def task_create(data: dict = Depends(body)):
    """新建任务。when.mode：now 立即开始（默认）/ once 定时（when.at = "YYYY-MM-DD HH:MM"）/ daily 每天（when.time =
    "HH:MM"），本机时间（PRD 5.1.1）。定时 / 每天返回 {"schedule": …}，到点由调度器开一轮。"""
    when = data.get("when") or {}
    mode = when.get("mode") or "now"
    if mode == "now":
        return tasks.create_task(data)
    from . import schedules
    return {"schedule": schedules.create(data, mode, when)}


@app.get("/api/schedules")
def schedule_list():
    from . import schedules
    return schedules.list_schedules()


@app.put("/api/schedules/{sid}")
def schedule_update(sid: int, data: dict = Depends(body)):
    """「修改」（params / at / time）和「暂停」/「恢复」（paused）。"""
    from . import schedules
    return schedules.update(sid, data)


@app.delete("/api/schedules/{sid}")
def schedule_delete(sid: int):
    from . import schedules
    schedules.delete(sid)
    return {"ok": True}


@app.get("/api/tasks")
def task_list(kind: str | None = None, limit: int = 30):
    return tasks.list_tasks(max(1, min(int(limit), 200)), kind=kind)


@app.get("/api/tasks/{task_id}")
def task_get(task_id: str):
    return tasks.get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
def task_cancel(task_id: str):
    tasks.cancel(task_id)
    return {"ok": True}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = 0):
    tasks.get_task(task_id)

    async def gen():
        last = after
        idle = 0.0
        while True:
            if await request.is_disconnected():
                return
            evs = tasks.events_since(task_id, last)
            for ev in evs:
                last = ev["seq"]
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
            run = tasks.get_run(task_id)
            if evs:
                idle = 0
                stats = run.snapshot() if run else tasks.get_task(task_id)
                if run:
                    yield f"event: stats\ndata: {json.dumps(stats, ensure_ascii=False)}\n\n"
            if not run or (run.done and not tasks.events_since(task_id, last)):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.4)
            idle += 0.4
            if idle >= 15:
                idle = 0
                yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/tasks/{task_id}/events-tail")
def task_events_tail(task_id: str, n: int = 30):
    """已经结束 / 中断的任务：最后几条记录（快速开始页面显示，不再等实时进度）。"""
    tasks.get_task(task_id)
    rows = db.query("SELECT * FROM task_events WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task_id, max(1, min(n, 200))))
    return [{"seq": r["seq"], "ts": r["ts"], "agent": r["agent"], "level": r["level"], "key": r["key"],
             "params": db.loads(r["params"], {})} for r in reversed(rows)]


@app.get("/api/tasks/{task_id}/pool")
def task_pool(task_id: str):
    rows = db.query("SELECT id, source, source_url, company_name, country, status, reason, lead_id FROM raw_leads "
                    "WHERE task_id=? ORDER BY id DESC LIMIT 500", (task_id,))
    return rows


# ------------------------------------------------------------------ 线索库

def _filters(request: Request):
    q = request.query_params
    return {k: q.get(k) for k in ("grade", "country", "source", "status", "q", "qualified", "icp_id", "task_id",
                                  "feedback") if q.get(k)}


@app.get("/api/leads")
def leads_list(request: Request, page: int = 1, size: int = 50):
    return leads.list_leads(_filters(request), page, min(max(size, 1), 200))


@app.get("/api/leads/facets")
def leads_facets():
    return leads.facets()


@app.get("/api/leads/export.csv")
def leads_export(request: Request):
    content = leads.export_csv(_filters(request), ui_lang())
    name = time.strftime("leads-%Y%m%d-%H%M.csv")
    return Response(content.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/leads/export.xlsx")
def leads_export_xlsx(request: Request):
    """线索库导出：与 TG 推送相同的本地化 Excel（当前筛选，或勾选的线索）。"""
    from . import push
    ids = [i for i in (request.query_params.get("ids") or "").split(",") if i.strip().isdigit()]
    path = push.make_excel(leads.export_rows(_filters(request), ids), ui_lang())
    name = time.strftime("leads-%Y%m%d-%H%M.xlsx")
    return FileResponse(path, filename=name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/leads/{lead_id}")
def lead_get(lead_id: int):
    return leads.get(lead_id)


@app.post("/api/leads/delete")
def leads_delete(data: dict = Depends(body)):
    return {"deleted": leads.delete(data.get("ids") or [])}


@app.post("/api/leads/{lead_id}/feedback")
def lead_feedback(lead_id: int, data: dict = Depends(body)):
    pol = data.get("polarity")
    if pol not in (1, -1, 0):
        raise UserError("invalid_value", field="polarity")
    if pol == -1:
        # 「这条不好」= 拒绝 + 负样本 + 调整（可撤销），与审核台一致（PRD 14）
        from . import review
        return review.act(lead_id, "reject", reasons=data.get("reasons") or ([data["reason"]] if data.get("reason") else []),
                          note=data.get("note") or "")
    return learning.mark_lead(lead_id, pol, data.get("reason") or "", data.get("note") or "")


# ------------------------------------------------------------------ 数据管理

@app.post("/api/data/delete-all")
def delete_all(data: dict = Depends(body)):
    if data.get("confirm") != "DELETE":
        raise UserError("delete_confirm_required")
    scope = data.get("scope", "business")
    if not isinstance(scope, str) or scope not in ("business", "everything"):
        raise UserError("invalid_value", field="scope")
    tables = ["lead_identities", "leads", "raw_leads", "channels", "task_events", "tasks", "icp_samples", "icp_knowledge",
              "tg_join_queue", "tg_discoveries", "outbox", "alerts", "memos", "agent_chats", "decision_tests",
              "embeddings", "onboarding", "site_pages", "profile_versions", "agent_actions",
              "task_schedules", "contact_blacklist", "contact_history", "quality_checks", "pool_events",
              "manager_plans", "manager_commands", "manager_reports", "scout_runs", "icp_templates"]
    if scope == "everything":
        tables += ["usage", "risk_log", "secrets", "agent_configs", "settings", "tg_chats", "tg_counters", "tg_accounts",
                   "prompt_overrides"]
    from . import schedules
    # Lock order: schedule -> task registration -> database. In-flight ticks
    # finish first; newly created tasks then make reset fail without deleting.
    with schedules.execution_lock(), tasks.registry_lock(), background.reset_guard() as reset:
        if any(not r.done for r in tasks._RUNS.values()):
            raise UserError("task_already_running")
        with db.transaction():
            for t in tables:
                db.execute(f"DELETE FROM {t}")
            for key in ("manager_goal", "scout_settings", "scout_collection_state", "channel_overrides",
                        "source_pool_settings", "source_pool_entries", "source_pool_report", "source_pool_checkpoints",
                        "source_pool_bootstrap", "manager_preparation", "manager_connection_checks", "manager_remediations"):
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            if scope == "everything":
                llm.init_defaults()
        reset.commit()
        tasks._RUNS.clear()
        # Pending callbacks from the old generation will be skipped, so their
        # finally blocks cannot be relied on to clear scheduling sentinels.
        from . import scheduler
        from .agents import scout
        from .agents import preparation, remediation
        preparation._busy = False
        remediation._active.clear()
        from .tg import upkeep
        scheduler._last.clear()
        scheduler._tg_info_busy[0] = False
        upkeep._next_quick.clear()
        upkeep._busy[0] = False
        scout._active_days.clear()
        if scope == "everything":
            from . import social
            social._sessions.clear()
        try:
            db.execute("VACUUM")
        except Exception:  # Compaction is optional; the deletion already committed.
            import traceback
            traceback.print_exc()
    return {"ok": True, "scope": scope}


# ------------------------------------------------------------------ 前端

@app.get("/")
def index():
    # 界面文件的网址带上版本号（/static/js/core.js?v=0.2.3）：装了新版本后窗口一定加载新的界面代码，
    # 不会用缓存里旧版本的（Mac 的窗口缓存按软件共用，旧版本存下的文件没有缓存规则，可能被当成还新鲜）
    html = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'((?:src|href)="/static/[^"?]+)"', rf'\1?v={VERSION}"', html)
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


from .api_p2 import router as _p2_router  # noqa: E402
from .desktop_api import router as _desktop_router  # noqa: E402

app.include_router(_p2_router)
app.include_router(_desktop_router)
from .api_manager import router as _manager_router  # noqa: E402
app.include_router(_manager_router)
app.mount("/static", StaticFiles(directory=paths.WEB_DIR), name="static")


def _apply_default_changes():
    """新版本改了默认值时，对已经装好的软件生效一次（之后以客户自己的设置为准）。"""
    try:
        decision.migrate()                     # 判断模型改成和其他 AI 一样配置（PRD 2.3）：旧设置迁移一次
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    done = list(settings.get("applied_defaults", []) or [])
    if "stealth_on_0.2.2" not in done:          # 隐身模式：测试阶段默认开启、按需使用（0.2.1 存的是 false）
        c = settings.get("crawler")
        c["stealth_mode"] = True
        settings.put("crawler", c)
        done.append("stealth_on_0.2.2")
        settings.put("applied_defaults", done)
    if "lead_words_v1" not in done:             # 模板改成线索词（PRD §3）：旧的关键词 / 求购说法 / 排除关键词搬过去
        from .agents import icp as icp_mod
        for r in db.query("SELECT id, data FROM icp_templates"):
            db.execute("UPDATE icp_templates SET data=? WHERE id=?",
                       (db.dumps(icp_mod.normalize(db.loads(r["data"], {}))), r["id"]))
        done.append("lead_words_v1")
        settings.put("applied_defaults", done)
    if "tg_default_monitor_v1" not in done:     # 群默认监听（PRD 6.3）：升级时所有还在的群改为监听
        from .tg import pool as tg_pool
        n = tg_pool.migrate_default_monitored()
        print(f"[upgrade] groups switched to monitored by default: {n}", flush=True)
        done.append("tg_default_monitor_v1")
        settings.put("applied_defaults", done)


def startup():
    from . import scheduler
    paths.ensure_dirs()
    from . import qa_fixture
    qa_fixture.maybe_install()          # 只在设置了 SALESTEAM_QA_FIXTURES 时生效（测试钩子）；平时什么都不做
    db.connect()
    llm.init_defaults()
    tasks.mark_interrupted()
    leads.settle_review_statuses()
    _apply_default_changes()
    try:
        from .agents import onboarding as _ob
        _ob.recover()               # 上次停在“读取中”的快速开始：接着读一次，或标记失败（界面给出重试 / 跳过 / 重新填写）
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
    if not os.environ.get("SALESTEAM_NO_SCHEDULER"):
        scheduler.start()

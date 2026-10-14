"""桌面版菜单栏 / 托盘用的接口（PRD 9.0.1）：状态一行字、紧急提醒红点、暂停 / 继续所有 AI、再次打开软件时显示窗口。

只给本机的窗口进程用（服务只监听 127.0.0.1）。"""
from fastapi import APIRouter, Depends

from . import db, settings, tasks
from .api_p2 import body

router = APIRouter(prefix="/api")
_show = {"seq": 0}


@router.get("/desktop/state")
def state():
    from . import workbench
    t0 = workbench._today0()
    working = sum(1 for a in workbench.agents() if a.get("status") == "working")
    q = db.query_one("SELECT COUNT(*) n FROM leads WHERE qualified=1 AND created_at>=?", (t0,))["n"]
    urgent = db.query_one("SELECT COUNT(*) n FROM alerts WHERE level='urgent' AND status='open'")["n"]
    return {"working": working, "qualified_today": q, "urgent": urgent, "paused": settings.ai_paused(),
            "show_seq": _show["seq"], "lang": settings.get("ui_lang") or "zh"}


@router.post("/desktop/show")
def show():
    """软件已经在后台运行时再次打开：让已有的窗口显示出来（窗口进程轮询 show_seq）。"""
    _show["seq"] += 1
    return {"ok": True}


@router.post("/ai/pause")
def pause(data: dict = Depends(body)):
    """暂停 / 继续所有 AI。暂停时停下正在跑的找客户 / 监控 / 线索池任务（已有结果保留）。"""
    on = bool(data.get("on"))
    tasks.set_paused(on)
    return {"paused": on}

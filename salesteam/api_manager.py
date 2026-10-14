"""Local API for confirmed goals and explicitly enabled TG discovery."""
from fastapi import APIRouter, Depends, Request
from .agents import manager, scout
from .errors import UserError

router = APIRouter(prefix="/api")


async def body(request: Request):
    try:
        value = await request.json()
    except ValueError:
        raise UserError("bad_request")
    if not isinstance(value, dict):
        raise UserError("bad_request")
    return value


@router.get("/manager/state")
def state():
    return manager.state()


@router.put("/manager/goal")
def goal(data: dict = Depends(body)):
    return manager.set_goal(data)


@router.post("/manager/command")
def command(data: dict = Depends(body)):
    return manager.command(data.get("message"))


@router.post("/manager/command/{cid}/confirm")
def confirm(cid: int):
    return manager.confirm_command(cid)


@router.post("/manager/command/{cid}/reject")
def reject(cid: int):
    return manager.reject_command(cid)


@router.get("/manager/plan")
def plan(day: str = ""):
    return manager.plans(day or None)


@router.get("/manager/report")
def report(day: str = ""):
    return manager.report(day or None)


@router.get("/scout/settings")
def scout_settings():
    return scout.get_settings()


@router.put("/scout/settings")
def scout_put(data: dict = Depends(body)):
    return scout.set_settings(data)


@router.get("/scout/status")
def scout_status():
    return scout.status()

"""TG 账号池（PRD 6.2）+ 群列表（6.3 A）+ 通知号发送（7.1 / 7.2）。

- 账号数量不限；只给提醒（不阻止）：超过 5 个账号且有账号没设代理 → 封号风险；多个账号共用同一代理 IP → 提醒
- 健康状态：active 正常 / need_relogin 掉线需重登 / restricted 被限制（自动暂停 + 紧急提醒）/ flood_wait（到期自动恢复）
- 每日上限按账号计数（进群、发送）；新号“养号”期内每天最多进 1 个群
- 一个号最多约 500 个群/频道（默认 480 留余量），满了自动换号
"""
import random
import threading
import time

from .. import alerts, db, settings, vault
from ..errors import UserError
from . import base

_backend = None
_blk = threading.Lock()


def backend() -> base.Backend:
    global _backend
    want = settings.get("tg")["backend"]
    with _blk:
        if _backend is None or _backend.name != want:
            if want == "mock":
                from .mock import MockBackend
                _backend = MockBackend()
            else:
                from .telethon_backend import TelethonBackend
                _backend = TelethonBackend()
        return _backend


def tg_config() -> dict:
    import sys
    t = settings.get("tg")
    return {"backend": t["backend"], "api_id": t.get("api_id") or "",
            "api_hash_set": bool(settings.get_secret("tg_api_hash")),
            # 安装包里不给客户勾“测试模式：假的 TG”（同判断模型的 mock）；开发 / 测试环境，或已经在用时才显示
            "dev": not getattr(sys, "frozen", False)}


def set_tg_config(data: dict):
    t = settings.get("tg")
    if "backend" in data:
        if data["backend"] not in ("telethon", "mock"):
            raise UserError("invalid_value", field="backend")
        t["backend"] = data["backend"]
    if "api_id" in data:
        v = str(data["api_id"] or "").strip()
        if v and not v.isdigit():
            raise UserError("tg_api_id_invalid")
        t["api_id"] = v
    settings.put("tg", t)
    if data.get("api_hash"):
        settings.set_secret("tg_api_hash", vault.clean_secret(data["api_hash"]))


# ------------------------------------------------------------------ 账号

def _row(acc_id):
    r = db.query_one("SELECT * FROM tg_accounts WHERE id=?", (acc_id,))
    if not r:
        raise UserError("tg_account_not_found", 404)
    return r


def _acc(r: dict) -> dict:
    """给后端用的账号信息（解密）。"""
    proxy = db.loads(r["proxy"], {})
    if proxy.get("password_enc"):
        proxy["password"] = vault.decrypt(proxy.pop("password_enc"))
    t = settings.get("tg")
    return {"id": r["id"], "phone": r["phone"], "session": vault.decrypt(r["session_enc"]),
            "api_id": t.get("api_id") or "", "api_hash": settings.get_secret("tg_api_hash"),
            "proxy": proxy, "device": db.loads(r["device"], {})}


def public(r: dict) -> dict:
    proxy = db.loads(r["proxy"], {})
    proxy.pop("password_enc", None)
    day = time.strftime("%Y-%m-%d")
    cnt = {x["kind"]: x["n"] for x in db.query("SELECT kind, n FROM tg_counters WHERE account_id=? AND day=?",
                                                 (r["id"], day))}
    chats = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(monitored),0) m FROM tg_chats WHERE account_id=? "
                         "AND left_at IS NULL", (r["id"],))
    from ..lease import roles_of
    return {"id": r["id"], "phone": r["phone"], "label": r["label"], "role": r["role"], "roles": sorted(roles_of(r)),
            "status": r["status"], "logged_in": bool(r["session_enc"]),
            "status_detail": r["status_detail"], "flood_until": r["flood_until"], "proxy": proxy,
            "device": db.loads(r["device"], {}), "tg_user": db.loads(r["tg_user"], {}),
            "daily_join_limit": r["daily_join_limit"], "daily_send_limit": r["daily_send_limit"],
            "max_chats": r["max_chats"], "warmup_days": r["warmup_days"], "added_at": r["added_at"],
            "last_ok_at": r["last_ok_at"], "in_warmup": in_warmup(r), "warmup_left_days": warmup_left_days(r),
            "today": cnt,
            "chats": chats["n"], "monitored": chats["m"], **_sched_info(r["id"])}


def _sched_info(acc_id) -> dict:
    """账号调度：今天各动作的上限、现在被哪个 AI 借着（PRD 6.2.1）。"""
    from .. import lease
    c = lease.cfg()["caps"]
    h = lease.holding().get(f"tg:{acc_id}")
    return {"caps": {k: c[k] for k in ("search", "preview", "read")}, "holding": (h or {}).get("agent") or ""}


def list_accounts():
    return [public(r) for r in db.query("SELECT * FROM tg_accounts ORDER BY role DESC, id")]


def _clean_proxy(p: dict | None, old: dict | None = None) -> dict:
    p = dict(p or {})
    if not p.get("host"):
        return {}
    out = {"type": p.get("type") if p.get("type") in ("socks5", "socks4", "http") else "socks5",
           "host": str(p["host"]).strip(), "port": int(p.get("port") or 1080),
           "username": str(p.get("username") or "").strip()}
    if p.get("password"):
        out["password_enc"] = vault.encrypt(str(p["password"]))
    elif old and old.get("password_enc") and old.get("host") == out["host"]:
        out["password_enc"] = old["password_enc"]
    return out


def add_account(data: dict) -> dict:
    phone = "".join(ch for ch in str(data.get("phone") or "") if ch.isdigit() or ch == "+")
    if len(phone.lstrip("+")) < 7:
        raise UserError("tg_phone_invalid")
    if not phone.startswith("+"):
        phone = "+" + phone
    if db.query_one("SELECT id FROM tg_accounts WHERE phone=?", (phone,)):
        raise UserError("tg_phone_exists")
    role = data.get("role") if data.get("role") in ("monitor", "notify") else "monitor"
    aid = db.execute("INSERT INTO tg_accounts(phone, label, role, roles, proxy, device, added_at) VALUES(?,?,?,?,?,?,?)",
                     (phone, str(data.get("label") or "")[:60], role, _clean_roles(data.get("roles"), role),
                      db.dumps(_clean_proxy(data.get("proxy"))), db.dumps(_clean_device(data.get("device"))), db.now()))
    if data.get("skip_warmup"):                 # 「老号，跳过养号」（客户决定，PRD 6.3）
        db.execute("UPDATE tg_accounts SET warmup_days=0 WHERE id=?", (aid,))
    if role == "notify":
        _single_notify(aid)
    return public(_row(aid))


def _clean_device(d):
    d = d or {}
    return {k: str(d.get(k) or "")[:60] for k in ("device_model", "system_version", "app_version", "lang_code")
            if d.get(k)}


def _clean_roles(roles, role: str) -> str:
    """账号用途（PRD 6.2.1，可以重叠）：monitor 读群并加入要监控的群；scout 只给渠道拓展员搜索 / 预览；notify 推送 / 提醒。
    没给时按主用途：监控号 = monitor + scout，通知号 = notify。"""
    if isinstance(roles, str):
        roles = [x for x in roles.split(",")]
    rs = [x.strip() for x in (roles or []) if str(x).strip() in ("monitor", "scout", "notify")]
    if not rs:
        rs = ["notify"] if role == "notify" else ["monitor", "scout"]
    if role == "notify" and "notify" not in rs:
        rs.append("notify")
    return ",".join(sorted(set(rs)))


def _single_notify(aid):
    """通知号只能有一个；新设的通知号把旧的改回监控号。"""
    for r in db.query("SELECT id, roles FROM tg_accounts WHERE id<>? AND (role='notify' OR roles LIKE '%notify%')", (aid,)):
        rs = [x for x in (r["roles"] or "").split(",") if x and x != "notify"] or ["monitor", "scout"]
        db.execute("UPDATE tg_accounts SET role='monitor', roles=? WHERE id=?", (",".join(sorted(rs)), r["id"]))


def update_account(acc_id: int, data: dict) -> dict:
    r = _row(acc_id)
    sets, args = [], []
    if "label" in data:
        sets.append("label=?"); args.append(str(data["label"] or "")[:60])
    if "role" in data and data["role"] in ("monitor", "notify"):
        sets.append("role=?"); args.append(data["role"])
    if "roles" in data or "role" in data:
        sets.append("roles=?"); args.append(_clean_roles(data.get("roles"), data.get("role") or r["role"]))
    if "proxy" in data:
        sets.append("proxy=?"); args.append(db.dumps(_clean_proxy(data["proxy"], db.loads(r["proxy"], {}))))
    if "device" in data:
        sets.append("device=?"); args.append(db.dumps(_clean_device(data["device"])))
    for k, lo, hi in (("daily_join_limit", 0, 50), ("daily_send_limit", 0, 500), ("max_chats", 10, 500),
                      ("warmup_days", 0, 30)):
        if k in data:
            try:
                v = int(data[k])
            except (TypeError, ValueError):
                raise UserError("invalid_value", field=k)
            if not lo <= v <= hi:
                raise UserError("value_range", field=k, min=lo, max=hi)
            sets.append(f"{k}=?"); args.append(v)
    if data.get("disabled") and r["status"] != "disabled":
        # 「暂停使用」（PRD 6.4.1）：登录信息保留；调度不再借出这个号（不读群、不加群、不发通知）
        sets += ["status=?", "status_detail=?"]; args += ["disabled", ""]
    if sets:
        db.execute(f"UPDATE tg_accounts SET {', '.join(sets)} WHERE id=?", (*args, acc_id))
    if data.get("role") == "notify":
        _single_notify(acc_id)
    if "proxy" in data or "device" in data:
        backend().logout(_acc(_row(acc_id)))   # 下次用新代理/设备信息重新连接
    if "disabled" in data and not data["disabled"]:
        return resume(acc_id)
    return public(_row(acc_id))


def resume(acc_id: int) -> dict:
    """「恢复使用」：先现读一次 get_me 确认账号还能用，再改回正常。没有登录信息 → 「未登录」。
    读失败：掉线 / 被限制等按平常的规则改状态；网络问题 → 仍是「已暂停」，把原因告诉客户。"""
    r = _row(acc_id)
    if r["status"] != "disabled":
        return public(r)
    if not r["session_enc"]:
        _set_status(acc_id, "new")
        return public(_row(acc_id))
    try:
        user = backend().check(_acc(r))
    except base.TGError as e:
        handle_error(acc_id, e)
        raise _user_error(e)
    _set_status(acc_id, "active", tg_user=db.dumps(user), last_ok_at=db.now())
    alerts.resolve(f"tg_account:{acc_id}")
    return public(_row(acc_id))


def delete_account(acc_id: int):
    r = _row(acc_id)
    try:
        backend().logout(_acc(r))
    except Exception:  # noqa: BLE001
        pass
    with db.transaction():
        db.execute("DELETE FROM tg_chats WHERE account_id=?", (acc_id,))
        db.execute("DELETE FROM tg_counters WHERE account_id=?", (acc_id,))
        db.execute("UPDATE tg_join_queue SET account_id=NULL, status='pending' WHERE account_id=? AND status='pending'",
                   (acc_id,))
        db.execute("DELETE FROM tg_accounts WHERE id=?", (acc_id,))
    alerts.resolve(f"tg_account:{acc_id}")


def warnings() -> list[dict]:
    rows = db.query("SELECT * FROM tg_accounts WHERE status<>'disabled'")
    out = []
    no_proxy = [r for r in rows if not db.loads(r["proxy"], {}).get("host")]
    if len(rows) > 5 and no_proxy:
        out.append({"key": "tg_warn_no_proxy", "params": {"n": len(rows), "m": len(no_proxy)}})
    seen = {}
    for r in rows:
        h = db.loads(r["proxy"], {}).get("host")
        if h:
            seen.setdefault(h, []).append(r["phone"])
    for h, phones in seen.items():
        if len(phones) > 1:
            out.append({"key": "tg_warn_shared_proxy", "params": {"host": h, "phones": ", ".join(phones)}})
    t = settings.get("tg")
    if t["backend"] != "mock" and (not t.get("api_id") or not settings.get_secret("tg_api_hash")):
        out.append({"key": "tg_warn_no_api", "params": {}})
    return out


# ------------------------------------------------------------------ 登录

def _set_status(acc_id, status, detail="", **extra):
    sets, args = ["status=?", "status_detail=?"], [status, detail[:300]]
    for k, v in extra.items():
        sets.append(f"{k}=?")
        args.append(v)
    db.execute(f"UPDATE tg_accounts SET {', '.join(sets)} WHERE id=?", (*args, acc_id))


def start_login(acc_id: int) -> dict:
    r = _row(acc_id)
    try:
        st = backend().send_code(_acc(r))
    except base.TGError as e:
        raise _user_error(e)
    db.execute("UPDATE tg_accounts SET login_state=?, status='code_sent', status_detail='' WHERE id=?",
               (vault.encrypt(db.dumps(st)), acc_id))
    return public(_row(acc_id))


def submit_code(acc_id: int, code: str = "", password: str = "") -> dict:
    r = _row(acc_id)
    st = db.loads(vault.decrypt(r["login_state"]) if r["login_state"] not in ("", "{}") else "{}", {}) or {}
    try:
        res = backend().sign_in(_acc(r), st, code=code, password=password)
    except base.NeedPassword as e:
        st["need_password"] = True
        if e.params.get("tmp_session"):
            st["tmp_session"] = e.params["tmp_session"]
        db.execute("UPDATE tg_accounts SET login_state=?, status='need_password' WHERE id=?",
                   (vault.encrypt(db.dumps(st)), acc_id))
        return public(_row(acc_id))
    except base.TGError as e:
        raise _user_error(e)
    # 只保存登录状态（session），不保存验证码和两步验证密码
    db.execute("UPDATE tg_accounts SET session_enc=?, login_state='{}', status='active', status_detail='', "
               "tg_user=?, last_ok_at=? WHERE id=?",
               (vault.encrypt(res["session"]), db.dumps(res.get("user") or {}), db.now(), acc_id))
    alerts.resolve(f"tg_account:{acc_id}")
    try:
        sync_dialogs(acc_id)
    except Exception as e:  # noqa: BLE001
        sync_failed(acc_id, e)
    return public(_row(acc_id))


def sync_failed(acc_id: int, e: Exception):
    """群列表同步失败：写进日志（app.log）、账号行显示“群列表同步失败：…”、发一般提醒；下次同步成功时清掉。"""
    import traceback
    print(f"[tg] account {acc_id}: dialogs sync failed: {type(e).__name__}: {e}", flush=True)
    traceback.print_exc()
    detail = (e.params.get("detail") if isinstance(e, UserError) else getattr(e, "detail", "")) or ""
    key = getattr(e, "key", "") or type(e).__name__
    reason = detail if (detail and key == "tg_error") else key + (f": {detail}" if detail else
                                                               (f": {e}" if not isinstance(e, UserError) else ""))
    r = _row(acc_id)
    if r["status"] == "active":                  # 掉线 / 被限制等由 handle_error 设好了状态，不覆盖
        db.execute("UPDATE tg_accounts SET status_detail=? WHERE id=?", (("sync_failed: " + reason)[:300], acc_id))
    alerts.raise_alert(f"tg_sync:{acc_id}", "general", "tg_sync_failed", link="#/accounts",
                       account=r["label"] or r["phone"], reason=reason[:200])


def logout(acc_id: int) -> dict:
    """「退出登录」（PRD 6.4.1）：真正在 TG 退出（手机「设置 → 设备」里随之消失），清掉本机登录信息，状态「未登录」。
    群列表（tg_chats）保留，重新登录后全部恢复。TG 那边没退成（网络等）也照样清本机，返回 remote=False，
    由界面提醒客户去手机上手动结束这个会话。"""
    from .. import lease
    r = _row(acc_id)
    acc = _acc(r)
    ls = None
    if usable(r):          # 正在被 AI 用着：等它这一步做完（最多 20 秒），免得读到一半被切断
        ls = lease.acquire("tg", next(iter(lease.roles_of(r))), purpose="maintenance", agent="system",
                           account_id=acc_id, timeout=20)
    try:
        db.execute("UPDATE tg_accounts SET session_enc='', login_state='{}', status='new', status_detail='' WHERE id=?",
                   (acc_id,))
    finally:
        if ls:
            ls.release()
    remote = False
    if acc.get("session"):
        try:
            remote = bool(backend().sign_out(acc))
        except Exception as e:  # noqa: BLE001  网络等：本机已经清掉，界面提醒客户去手机上结束会话
            print(f"[tg] account {acc_id}: log out on Telegram failed: {type(e).__name__}: {e}", flush=True)
    alerts.resolve(f"tg_account:{acc_id}")
    alerts.resolve(f"tg_sync:{acc_id}")
    return {"ok": True, "remote": remote, "account": public(_row(acc_id))}


def _user_error(e: base.TGError) -> UserError:
    if isinstance(e, base.FloodWait):
        return UserError("tg_flood_wait", minutes=max(1, e.seconds // 60))
    return UserError(e.key, detail=e.detail)


# ------------------------------------------------------------------ 健康状态 / 错误处理

def in_warmup(r) -> bool:
    return (time.time() - r["added_at"]) < r["warmup_days"] * 86400


def warmup_left_days(r) -> int:
    left = r["added_at"] + r["warmup_days"] * 86400 - time.time()
    return max(0, int(-(-left // 86400))) if left > 0 else 0


def skip_warmup(acc_id: int) -> dict:
    """客户确认后立刻结束养号（PRD 6.3）：之后按客户设的每日上限。"""
    _row(acc_id)
    db.execute("UPDATE tg_accounts SET warmup_days=0 WHERE id=?", (acc_id,))
    return public(_row(acc_id))


def usable(r) -> bool:
    if r["status"] == "flood_wait" and (r["flood_until"] or 0) <= time.time():
        _set_status(r["id"], "active")
        alerts.resolve(f"tg_flood:{r['id']}")
        return True
    return r["status"] == "active"


def handle_error(acc_id: int, e: Exception) -> str:
    """把 TG 错误转成账号状态 + 提醒。返回 i18n 事件 key。"""
    r = _row(acc_id)
    label = r["label"] or r["phone"]
    if isinstance(e, base.FloodWait):
        until = time.time() + e.seconds + 5
        _set_status(acc_id, "flood_wait", f"{e.seconds}s", flood_until=until)
        alerts.raise_alert(f"tg_flood:{acc_id}", "general", "tg_flood", link="#/accounts", account=label,
                           minutes=max(1, e.seconds // 60))
        return "ev.tg_flood"
    if isinstance(e, base.AuthRequired):
        _set_status(acc_id, "need_relogin", e.detail)
        alerts.raise_alert(f"tg_account:{acc_id}", "urgent", "tg_relogin", link="#/accounts", account=label)
        _account_down(acc_id, "need_relogin")
        return "ev.tg_relogin"
    if isinstance(e, base.Restricted):
        _set_status(acc_id, "restricted", e.detail)
        alerts.raise_alert(f"tg_account:{acc_id}", "urgent", "tg_restricted", link="#/accounts", account=label)
        _account_down(acc_id, "restricted")
        return "ev.tg_restricted"
    if isinstance(e, base.NotConfigured):
        alerts.raise_alert("tg_not_configured", "urgent", "tg_not_configured", link="#/accounts")
        return "ev.tg_not_configured"
    return "ev.tg_error"


def _account_down(acc_id, reason):
    """账号调度（PRD 6.2.1）：立刻不再借给任何 AI，转移它监控的群，发紧急提醒。"""
    try:
        from .. import lease
        lease.account_down(acc_id, reason)
    except Exception:  # noqa: BLE001  转移失败不影响状态记录
        import traceback
        traceback.print_exc()


def mark_ok(acc_id: int):
    db.execute("UPDATE tg_accounts SET last_ok_at=? WHERE id=?", (db.now(), acc_id))


def check_health(acc_id: int) -> dict:
    r = _row(acc_id)
    if not r["session_enc"]:
        return public(r)
    try:
        user = backend().check(_acc(r))
        _set_status(acc_id, "active", tg_user=db.dumps(user), last_ok_at=db.now())
        alerts.resolve(f"tg_account:{acc_id}")
    except Exception as e:  # noqa: BLE001
        handle_error(acc_id, e)
    return public(_row(acc_id))


# ------------------------------------------------------------------ 计数（每日上限）

def count(acc_id, kind) -> int:
    r = db.query_one("SELECT n FROM tg_counters WHERE account_id=? AND day=? AND kind=?",
                     (acc_id, time.strftime("%Y-%m-%d"), kind))
    return r["n"] if r else 0


def incr(acc_id, kind, n=1):
    db.execute("INSERT INTO tg_counters(account_id, day, kind, n) VALUES(?,?,?,?) ON CONFLICT(account_id, day, kind) "
               "DO UPDATE SET n=n+excluded.n", (acc_id, time.strftime("%Y-%m-%d"), kind, n))


# ------------------------------------------------------------------ 群（A：识别账号已有的群）

def sync_dialogs(acc_id: int) -> dict:
    """读账号加入的群和频道。返回 {"total": 这个账号现在一共有几个, "added": 这次新增几个}。
    人数 / 简介 / 讨论区不在这里一次性请求（refresh_chat_info 慢慢补）。"""
    r = _row(acc_id)
    from .. import lease
    ls = lease.acquire("tg", next(iter(lease.roles_of(r))), purpose="maintenance", agent="system", account_id=acc_id,
                       timeout=120)
    if not ls:
        raise UserError("tg_account_busy")
    try:
        chats = backend().dialogs(_acc(r))
    except base.TGError as e:
        handle_error(acc_id, e)
        raise _user_error(e)
    finally:
        ls.release()
    mark_ok(acc_id)
    n = 0
    for c in chats:
        n += upsert_chat(acc_id, c, "A")
    # 频道的讨论区：记录 parent（真实后端读群列表时不知道讨论区，refresh_chat_info 补上后再关联）
    for c in chats:
        if c.get("linked_chat_id"):
            _link_discussion(acc_id, str(c["chat_id"]), str(c["linked_chat_id"]))
    # 群列表里已经没有的群 → 「已退出」（在手机上退了 / 被移出），不再监控。群列表整个是空的时候不标（防止读取异常时误标）
    gone = 0
    if chats:
        now_ids = {str(c["chat_id"]) for c in chats}
        for x in db.query("SELECT id, chat_id FROM tg_chats WHERE account_id=? AND left_at IS NULL", (acc_id,)):
            if x["chat_id"] not in now_ids:
                gone += mark_left(x["id"], "not in dialogs")
    db.execute("UPDATE tg_accounts SET dialogs_synced_at=? WHERE id=?", (db.now(), acc_id))
    if (r["status_detail"] or "").startswith("sync_failed"):
        db.execute("UPDATE tg_accounts SET status_detail='' WHERE id=?", (acc_id,))
    alerts.resolve(f"tg_sync:{acc_id}")
    total = db.query_one("SELECT COUNT(*) n FROM tg_chats WHERE account_id=? AND left_at IS NULL", (acc_id,))["n"]
    return {"total": total, "added": n, "left": gone}


def mark_left(chat_db_id: int, why: str = "") -> int:
    """「已退出」：这个账号已经不在这个群了（完整同步时不在群列表里，或读消息时 TG 说没有权限 / 不是成员）。
    关掉监控；群的记录保留（读取位置、命中数都在），重新加入后自动恢复为正常（监控要客户重新勾选）。"""
    c = db.query_one("SELECT id, account_id, title, left_at FROM tg_chats WHERE id=?", (chat_db_id,))
    if not c or c["left_at"]:
        return 0
    db.execute("UPDATE tg_chats SET left_at=?, monitored=0 WHERE id=?", (db.now(), chat_db_id))
    print(f"[tg] account {c['account_id']}: chat {c['id']} marked left ({why})", flush=True)
    return 1


# ------------------------------------------------------------------ 群列表自动更新（PRD 6.3）：快速检查最新的对话

QUICK_LIMIT = 30
_dialog_totals: dict[int, int] = {}       # 账号 → 上次快速检查时的对话总数（只在内存里；重启后第一次只记下来）


def check_new_chats(acc_id: int) -> dict:
    """快速检查（调度器每 5 分钟、账号之间错开）：一次 GetDialogs 读最新 30 个对话，没见过的群 / 频道直接加进列表
    （加入方式 A，监控默认值不变）；不请求 GetFullChannel（人数 / 讨论区由 refresh_chat_info 慢慢补）。
    只在账号正常、空闲时借（维护优先级），每次算 1 次 dialogs（每个账号每天单独的小上限，不占读消息的上限）。
    对话总数和上次不一样（加了 / 退了群，频道不一定排在前面）→ 做一次完整同步，每个账号每小时最多一次。"""
    from .. import lease
    r = _row(acc_id)
    if not r["session_enc"] or not usable(r):
        return {"skipped": "not_usable"}
    if lease.remaining("tg", acc_id, "dialogs") <= 0:
        return {"skipped": "cap"}
    ls = lease.acquire("tg", next(iter(lease.roles_of(r))), purpose="maintenance", agent="system", account_id=acc_id,
                       timeout=0, idle_only=True)
    if not ls:
        return {"skipped": "busy"}
    try:
        ls.use("dialogs")
        res = backend().recent_dialogs(_acc(r), QUICK_LIMIT)
    except lease.CapReached:
        return {"skipped": "cap"}
    except base.TGError as e:
        handle_error(acc_id, e)
        return {"skipped": "error", "error": getattr(e, "key", "tg_error")}
    finally:
        ls.release()
    mark_ok(acc_id)
    added = 0
    for c in res.get("chats") or []:
        added += upsert_chat(acc_id, c, "A")
    out = {"added": added}
    total, prev = res.get("total"), _dialog_totals.get(acc_id)
    if total is not None:
        _dialog_totals[acc_id] = total
    if prev is not None and total is not None and total != prev and \
            time.time() - (r["dialogs_synced_at"] or 0) > 3600:
        from .upkeep import background_sync
        out["full"] = background_sync(acc_id)
    return out


def _link_discussion(acc_id, channel_chat_id: str, discussion_chat_id: str):
    p = db.query_one("SELECT id, monitored FROM tg_chats WHERE account_id=? AND chat_id=?", (acc_id, channel_chat_id))
    db.execute("UPDATE tg_chats SET parent_id=?, kind='discussion' WHERE account_id=? AND chat_id=?",
               (p["id"] if p else None, acc_id, discussion_chat_id))
    if p and p["monitored"]:                   # 频道在监控 → 讨论区也监控（PRD 6.4）
        d = db.query_one("SELECT id, monitored FROM tg_chats WHERE account_id=? AND chat_id=?", (acc_id, discussion_chat_id))
        if d and not d["monitored"]:
            set_monitored([d["id"]], True, follow_discussion=False)


# ------------------------------------------------------------------ 群的人数 / 简介 / 讨论区：慢慢补（不在同步时一次性请求）

def refresh_chat_info(max_chats: int = 4, gap=(3.0, 8.0), only_chat_id: int | None = None) -> int:
    """逐个请求 GetFullChannel 补全真实人数、简介、频道的讨论区。只在账号空闲时借（不和监控抢），每个之间随机停顿，
    每个账号每天有上限（lease caps["info"]）。先补正在监控的、还没补过的，再补很久没更新的（7 天）。"""
    import random
    from .. import lease
    if only_chat_id is not None:
        rows = db.query("SELECT * FROM tg_chats WHERE id=?", (only_chat_id,))
    else:
        rows = db.query("SELECT c.* FROM tg_chats c JOIN tg_accounts a ON a.id=c.account_id WHERE a.status='active' "
                        "AND c.left_at IS NULL AND (c.info_at IS NULL OR c.info_at < ?) ORDER BY c.info_at IS NOT NULL, c.monitored DESC, "
                        "c.kind='discussion', c.id LIMIT ?", (time.time() - 7 * 86400, max_chats * 3))
    done = 0
    for c in rows:
        if done >= max_chats:
            break
        r = _row(c["account_id"])
        if lease.remaining("tg", r["id"], "info") <= 0:
            continue
        ls = lease.acquire("tg", next(iter(lease.roles_of(r))), purpose="maintenance", agent="system",
                           account_id=r["id"], timeout=10 if only_chat_id else 0, idle_only=only_chat_id is None)
        if not ls:
            continue
        try:
            ls.use("info")
            info = backend().full_info(_acc(r), dict(c))
        except lease.CapReached:
            continue
        except base.TGError as e:
            if isinstance(e, base.NotMember):          # 账号已经不在这个群 → 「已退出」
                mark_left(c["id"], e.detail)
            elif isinstance(e, base.InvalidLink):      # 不存在的群：标记一下，过 7 天再试
                db.execute("UPDATE tg_chats SET info_at=? WHERE id=?", (time.time(), c["id"]))
            else:
                handle_error(r["id"], e)
            continue
        finally:
            ls.release()
        db.execute("UPDATE tg_chats SET members=?, about=COALESCE(NULLIF(?,''), about), linked_chat_id=?, info_at=? "
                   "WHERE id=?", (int(info.get("members") or 0), info.get("about") or "",
                                  str(info.get("linked_chat_id") or c["linked_chat_id"] or ""), time.time(), c["id"]))
        if info.get("linked_chat_id"):
            _link_discussion(r["id"], str(c["chat_id"]), str(info["linked_chat_id"]))
        done += 1
        if done < max_chats and only_chat_id is None:
            time.sleep(random.uniform(*gap))
    return done


# ------------------------------------------------------------------ 账号详情（PRD 6.4.1）：现读一次账号信息（get_me）

def _view_lease(r):
    from .. import lease
    ls = lease.acquire("tg", next(iter(lease.roles_of(r))), purpose="maintenance", agent="system", account_id=r["id"],
                       timeout=15)
    if not ls:
        if r["status"] != "active":
            raise UserError("tg_account_not_ready", state=f"@tgs.{r['status']}")
        raise UserError("tg_account_busy")
    return ls


def account_info(acc_id: int) -> dict:
    """「详情」：通过账号调度借这个账号，现读一次 get_me（和「检查状态」一样），返回 TG 名字、@用户名、号码、用户 ID、状态、上次正常。"""
    r = _row(acc_id)
    if not r["session_enc"]:
        raise UserError("tg_auth_required")
    ls = _view_lease(r)
    try:
        user = backend().check(_acc(r))
    except base.TGError as e:
        handle_error(r["id"], e)
        raise _user_error(e)
    finally:
        ls.release()
    _set_status(acc_id, "active", r["status_detail"] if (r["status_detail"] or "").startswith("sync_failed") else "",
                tg_user=db.dumps(user), last_ok_at=db.now())
    alerts.resolve(f"tg_account:{acc_id}")
    r = _row(acc_id)
    return {"id": r["id"], "phone": r["phone"], "status": r["status"], "last_ok_at": r["last_ok_at"],
            "user": {"id": user.get("id"), "name": user.get("name") or "", "username": user.get("username") or ""}}


def upsert_chat(acc_id, c, method="A") -> int:
    """群列表里看到（或刚加入）的群：新的插入、已有的更新；之前标了「已退出」的恢复为正常。返回 1 = 新增或重新加入。"""
    username = (c.get("username") or "").lstrip("@").lower()
    overrides = settings.get("channel_overrides", {}) or {}
    retired = overrides.get(f"tg:@{username}") == "retired" if username else False
    retired = retired or overrides.get(f"tg:#{c['chat_id']}") == "retired"
    ex = db.query_one("SELECT id, left_at FROM tg_chats WHERE account_id=? AND chat_id=?", (acc_id, str(c["chat_id"])))
    if not ex:
        # 默认监听（PRD 6.3）：同步进来的、新加入的群都默认开（只在有勾了 TG 群的任务运行时才真正读取）；
        # 同一个群只由一个账号监听；通知号不监听
        mon = int(not retired and _default_monitor(acc_id, str(c["chat_id"])))
        # INSERT OR IGNORE：快速检查和完整同步碰巧同时插入同一个群时不报错
        db.execute("INSERT OR IGNORE INTO tg_chats(account_id, chat_id, title, username, kind, about, members, "
                   "linked_chat_id, join_method, monitored, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (acc_id, str(c["chat_id"]), c.get("title", ""), c.get("username", ""), c.get("kind", "group"),
                    c.get("about", ""), c.get("members", 0), str(c.get("linked_chat_id") or ""), method, mon, db.now()))
        return 1
    # 读群列表时人数常常拿不到（0）、也不知道讨论区：不能把补全好的人数 / 讨论区覆盖掉
    m = int(c.get("members") or 0)
    db.execute("UPDATE tg_chats SET title=?, username=?, members=CASE WHEN ?>0 THEN ? ELSE members END, "
               "about=COALESCE(NULLIF(?,''), about), linked_chat_id=COALESCE(NULLIF(?,''), linked_chat_id), "
               "left_at=NULL WHERE id=?", (c.get("title", ""), c.get("username", ""), m, m, c.get("about", ""),
                                           str(c.get("linked_chat_id") or ""), ex["id"]))
    if retired or ex["left_at"]:      # Manual retirement also applies on another account.
        db.execute("UPDATE tg_chats SET monitored=? WHERE id=?", (int(not retired and _default_monitor(acc_id, str(c["chat_id"]))), ex["id"]))
    return 1 if ex["left_at"] else 0


def _monitor_account(acc_id) -> bool:
    from ..lease import roles_of
    r = db.query_one("SELECT * FROM tg_accounts WHERE id=?", (acc_id,))
    return bool(r and "monitor" in roles_of(r))


def _default_monitor(acc_id, chat_id: str) -> bool:
    """新群默认监听：这个号是监控号，且别的号还没在监听同一个群。"""
    if not _monitor_account(acc_id):
        return False
    return not db.query_one("SELECT 1 FROM tg_chats WHERE chat_id=? AND monitored=1 AND account_id<>?", (chat_id, acc_id))


def migrate_default_monitored() -> int:
    """升级（PRD 6.3「默认监听」）：分不清客户有没有动过开关，所以所有还在的群都改为监听（同一个群只留一个号监听，
    已经在监听的那个号优先；通知号的群不监听）。只做一次（applied_defaults）。返回改了几个。"""
    changed = 0
    rows = db.query("SELECT c.id, c.chat_id, c.monitored, c.account_id FROM tg_chats c WHERE c.left_at IS NULL "
                    "ORDER BY c.monitored DESC, c.id")
    taken = set()
    for r in rows:
        if r["chat_id"] in taken or not _monitor_account(r["account_id"]):
            if r["monitored"]:
                db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (r["id"],))
            continue
        taken.add(r["chat_id"])
        if not r["monitored"]:
            db.execute("UPDATE tg_chats SET monitored=1 WHERE id=?", (r["id"],))
            changed += 1
    return changed


def list_chats(acc_id=None, monitored=None):
    sql = ("SELECT c.*, a.phone, a.label account_label, a.status account_status FROM tg_chats c "
           "JOIN tg_accounts a ON a.id=c.account_id")
    # 通知号的群不进监控列表（U28：通知号不加群、不监控）
    cond, args = ["(a.roles LIKE '%monitor%' OR (a.roles='' AND a.role<>'notify'))"], []
    if acc_id:
        cond.append("c.account_id=?"); args.append(acc_id)
    if monitored is not None:
        cond.append("c.monitored=?"); args.append(int(bool(monitored)))
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    return db.query(sql + " ORDER BY c.left_at IS NOT NULL, c.monitored DESC, c.members DESC", args)


def set_monitored(chat_ids: list[int], on: bool, icp_id=None, follow_discussion=True):
    """勾选监控。同一个群被多个账号加入时，只由一个账号监控（避免重复读取）。频道会连同其讨论区一起监控。"""
    changed = 0
    for cid in chat_ids:
        c = db.query_one("SELECT * FROM tg_chats WHERE id=?", (int(cid),))
        if not c or (on and c["left_at"]):          # 「已退出」的群不能勾选监控（重新加入后会自动恢复）
            continue
        ids = [c["id"]]
        if follow_discussion and c["linked_chat_id"]:
            d = db.query_one("SELECT id FROM tg_chats WHERE account_id=? AND chat_id=? AND left_at IS NULL",
                             (c["account_id"], c["linked_chat_id"]))
            if d:
                ids.append(d["id"])
        for i in ids:
            row = db.query_one("SELECT * FROM tg_chats WHERE id=?", (i,))
            if on:
                db.execute("UPDATE tg_chats SET monitored=0 WHERE chat_id=? AND id<>?", (row["chat_id"], i))
                cursor = row["cursor"]
                db.execute("UPDATE tg_chats SET monitored=1, icp_id=COALESCE(?, icp_id), cursor=? WHERE id=?",
                           (icp_id, cursor, i))
            else:
                db.execute("UPDATE tg_chats SET monitored=0 WHERE id=?", (i,))
            changed += 1
        if on and c["kind"] == "channel" and c["info_at"] is None:
            # 频道还不知道讨论区（同步时不再一次性请求）：马上在后台补这一个，找到讨论区就一起监控
            import threading
            threading.Thread(target=_refresh_one, args=(c["id"],), daemon=True, name="tg-info-one").start()
    return changed


def _refresh_one(chat_db_id: int):
    try:
        refresh_chat_info(1, only_chat_id=chat_db_id)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()


def pick_account_for_join() -> dict | None:
    """进群时挑账号：健康、未到每日进群上限、未满群数；养号期每天最多 1 个；群最少的优先（均匀分配）。"""
    best = None
    from ..lease import roles_of
    for r in db.query("SELECT * FROM tg_accounts ORDER BY id"):
        if "monitor" not in roles_of(r) or not usable(r):
            continue
        limit = min(1, r["daily_join_limit"]) if in_warmup(r) else r["daily_join_limit"]
        if count(r["id"], "join") >= limit:
            continue
        n = db.query_one("SELECT COUNT(*) n FROM tg_chats WHERE account_id=? AND left_at IS NULL", (r["id"],))["n"]
        if n >= r["max_chats"]:     # 新群分给群最少的监控账号，由它加入（PRD 6.2.1）
            continue
        if best is None or n < best[1]:
            best = (r, n)
    return best[0] if best else None


def monitor_accounts() -> list[dict]:
    from ..lease import roles_of
    return [r for r in db.query("SELECT * FROM tg_accounts ORDER BY id") if "monitor" in roles_of(r) and usable(r)]


def scout_accounts() -> list[dict]:
    from ..lease import roles_of
    return [r for r in db.query("SELECT * FROM tg_accounts ORDER BY id") if "scout" in roles_of(r) and usable(r)]


# ------------------------------------------------------------------ 通知号（PRD 7.1 / 7.2）

def notify_account():
    from ..lease import roles_of
    return next((r for r in db.query("SELECT * FROM tg_accounts ORDER BY role='notify' DESC, id")
                 if "notify" in roles_of(r)), None)


def notify_ready() -> bool:
    r = notify_account()
    return bool(r and usable(r))


def split_text(text: str, limit=3800) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def notify_send(recipients: list[str], text: str, kind: str, file_path: str | None = None,
                file_name: str = "") -> bool:
    """用通知号发送给接收人。只发给客户自己配置的接收人；遵守每日上限与随机间隔。返回是否全部成功。"""
    r = notify_account()
    if not r or not usable(r):
        return False
    from .. import lease
    ls = lease.acquire("tg", "notify", purpose="notify", agent="manager", account_id=r["id"], timeout=300)
    if not ls:
        return False
    try:
        return _notify_send(ls, r, recipients, text, kind, file_path, file_name)
    finally:
        ls.release()


def _notify_send(ls, r, recipients, text, kind, file_path, file_name) -> bool:
    from .. import lease
    be = backend()
    gap = settings.get("notify").get("send_gap_s") or [3, 8]
    chunks = split_text(text) if text else []
    ok_all = True
    first = True
    for rcp in recipients:
        msgs = [(c, None) for c in chunks] + ([(file_name or "", file_path)] if file_path else [])
        for msg, fp in msgs:
            if lease.remaining("tg", r["id"], "send") < 1:
                _outbox(r["id"], rcp, kind, msg, fp, "failed", "daily_limit")
                alerts.raise_alert(f"tg_send_limit:{r['id']}", "general", "tg_send_limit", link="#/accounts",
                                   account=r["label"] or r["phone"])
                return False
            if not first and not be.is_mock:
                time.sleep(random.uniform(float(gap[0]), float(gap[1])))
            first = False
            try:
                be.send(_acc(r), rcp, msg, file_path=fp)
                ls.use("send")
                _outbox(r["id"], rcp, kind, msg, fp, "mock" if be.is_mock else "sent")
            except base.TGError as e:
                ok_all = False
                _outbox(r["id"], rcp, kind, msg, fp, "failed", e.key)
                if isinstance(e, (base.FloodWait, base.AuthRequired, base.Restricted)):
                    handle_error(r["id"], e)
                    return False
                break
            except Exception as e:  # noqa: BLE001
                ok_all = False
                _outbox(r["id"], rcp, kind, msg, fp, "failed", str(e)[:200])
                break
    return ok_all


def _outbox(acc_id, rcp, kind, text, fp, status, err=""):
    import os
    db.execute("INSERT INTO outbox(ts, account_id, recipient, kind, text, file_name, status, error) "
               "VALUES(?,?,?,?,?,?,?,?)", (db.now(), acc_id, rcp, kind, text or "",
                                           os.path.basename(fp) if fp else "", status, err))


def outbox(limit=100):
    return db.query("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))

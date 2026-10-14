"""真实 TG 后端：Telethon（MIT，官方用户 API）。

- api_id / api_hash 由客户自己在 my.telegram.org 申请（不内置我方的，避免连坐封禁）
- 登录状态用 StringSession 保存，由上层加密后存数据库（不落地明文 session 文件）
- 每个账号可设独立代理（socks5 / http，需要 python-socks）和设备信息
- FloodWait / 掉线 / 被限制 统一转换成 base 里的错误类型，由账号池处理（自动暂停、提醒）
- 所有调用在一个专用 asyncio 线程里执行；客户端按账号缓存

注意：本文件在开发环境中没有用真实账号测试过（按要求不登录真实账号），只做了导入与构造检查。
"""
import asyncio
import concurrent.futures
import re
import threading

from .base import (AuthRequired, Backend, BadCode, BadPassword, BadPhone, FloodWait, InvalidLink, NeedPassword,
                   NeedsApproval, NotConfigured, NotMember, Restricted, TGError)

TIMEOUT = 90
DIALOGS_TIMEOUT = 240          # 群很多的账号读群列表会慢一些


class _Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="telethon-loop").start()

    def run(self, coro, timeout=TIMEOUT):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()                    # 超时了就别让它在后台继续跑（以前会一直占着连接）
            raise TimeoutError(f"no answer from Telegram within {timeout}s")


def _proxy(p: dict):
    if not p or not p.get("host"):
        return None
    out = {"proxy_type": p.get("type") or "socks5", "addr": p["host"], "port": int(p.get("port") or 1080), "rdns": True}
    if p.get("username"):
        out["username"] = p["username"]
        out["password"] = p.get("password") or ""
    return out


def _link_key(link: str) -> tuple[str, str]:
    """返回 (类型, 值)：('invite', hash) 或 ('public', username)。"""
    s = str(link or "").strip()
    m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)([\w-]+)", s)
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", s)
    if m:
        return "public", m.group(1)
    if s.startswith("@"):
        return "public", s[1:]
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", s):
        return "public", s
    raise InvalidLink(s)


class TelethonBackend(Backend):
    name = "telethon"

    def __init__(self):
        self._loop = None
        self._clients: dict[int, object] = {}
        self._lock = threading.Lock()

    @property
    def L(self):
        if self._loop is None:
            self._loop = _Loop()
        return self._loop

    # -------------------------------------------------- 客户端
    def _make(self, acc, session_str: str = ""):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        if not acc.get("api_id") or not acc.get("api_hash"):
            raise NotConfigured("api_id/api_hash missing")
        dev = acc.get("device") or {}
        return TelegramClient(StringSession(session_str or None), int(acc["api_id"]), acc["api_hash"],
                              proxy=_proxy(acc.get("proxy") or {}), device_model=dev.get("device_model") or None,
                              system_version=dev.get("system_version") or None,
                              app_version=dev.get("app_version") or None, lang_code=dev.get("lang_code") or "en",
                              flood_sleep_threshold=0, receive_updates=False, loop=self.L.loop)

    async def _client(self, acc):
        key = acc["id"]
        c = self._clients.get(key)
        if c is None:
            if not acc.get("session"):
                raise AuthRequired("no session")
            c = self._make(acc, acc["session"])
            self._clients[key] = c
        if not c.is_connected():
            await c.connect()
        if not await c.is_user_authorized():
            raise AuthRequired("session expired")
        return c

    def _call(self, fn, timeout=TIMEOUT):
        try:
            return self.L.run(fn(), timeout)
        except TGError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._translate(e) from e

    @staticmethod
    def _translate(e):
        from telethon import errors as te
        if isinstance(e, te.FloodWaitError):
            return FloodWait(e.seconds)
        if isinstance(e, (te.AuthKeyUnregisteredError, te.SessionRevokedError)):
            return AuthRequired(type(e).__name__)
        if isinstance(e, (te.UserDeactivatedError, te.UserDeactivatedBanError, te.PhoneNumberBannedError,
                          te.PeerFloodError, te.UserRestrictedError)):
            return Restricted(type(e).__name__)
        if isinstance(e, (te.ChannelPrivateError, te.UserNotParticipantError, te.ChannelInvalidError)):
            return NotMember(type(e).__name__)            # 被移出 / 已退出 / 私有群没有权限（也是一种 InvalidLink）
        if isinstance(e, (te.InviteHashExpiredError, te.InviteHashInvalidError, te.UsernameNotOccupiedError,
                          te.UsernameInvalidError)):
            return InvalidLink(type(e).__name__)
        if isinstance(e, te.InviteRequestSentError):
            return NeedsApproval(type(e).__name__)
        if isinstance(e, te.ChannelsTooMuchError):
            return Restricted("too many channels")
        return TGError(f"{type(e).__name__}: {e}"[:300])

    # -------------------------------------------------- 登录
    def send_code(self, acc):
        from telethon import errors as te

        async def go():
            c = self._make(acc)
            await c.connect()
            try:
                r = await c.send_code_request(acc["phone"])
            except te.PhoneNumberInvalidError:
                raise BadPhone("invalid phone")
            # 登录过程中的临时 session（还未授权）也要保留，否则验证码对不上
            return {"phone_code_hash": r.phone_code_hash, "tmp_session": c.session.save()}
        return self._call(go)

    def sign_in(self, acc, login_state, code="", password=""):
        from telethon import errors as te

        async def go():
            c = self._make(acc, login_state.get("tmp_session") or "")
            await c.connect()
            try:
                if login_state.get("need_password"):
                    await c.sign_in(password=password)
                else:
                    try:
                        await c.sign_in(phone=acc["phone"], code=code.strip(),
                                        phone_code_hash=login_state.get("phone_code_hash"))
                    except te.SessionPasswordNeededError:
                        if not password:
                            raise NeedPassword(tmp_session=c.session.save())
                        await c.sign_in(password=password)
            except (te.PhoneCodeInvalidError, te.PhoneCodeExpiredError):
                raise BadCode("bad code")
            except te.PasswordHashInvalidError:
                raise BadPassword("bad password")
            me = await c.get_me()
            session = c.session.save()
            await c.disconnect()
            return {"session": session, "user": {"id": me.id, "username": me.username or "",
                                                 "name": " ".join(x for x in (me.first_name, me.last_name) if x)}}
        return self._call(go)

    def check(self, acc):
        async def go():
            c = await self._client(acc)
            me = await c.get_me()
            return {"id": me.id, "username": me.username or "",
                    "name": " ".join(x for x in (me.first_name, me.last_name) if x)}
        return self._call(go)

    def logout(self, acc):
        """只断开本机连接（换代理 / 设备信息、删除账号时），TG 那边的会话还在。"""
        c = self._clients.pop(acc["id"], None)
        if c is not None:
            try:
                self.L.run(c.disconnect(), timeout=15)
            except Exception:  # noqa: BLE001
                pass

    def sign_out(self, acc):
        """真正退出登录（auth.logOut）：TG 删掉这个会话，手机「设置 → 设备」里随之消失。
        返回 True = TG 确认已退出；False = TG 没确认（例如会话早就失效）；网络等问题抛 TGError。"""
        async def go():
            c = self._clients.pop(acc["id"], None)
            if c is None:
                if not acc.get("session"):
                    return False
                c = self._make(acc, acc["session"])
            try:
                if not c.is_connected():
                    await c.connect()
                return bool(await c.log_out())       # 成功时 Telethon 自己断开并清空 session
            finally:
                if c.is_connected():
                    await c.disconnect()
        return self._call(go, 30)

    # -------------------------------------------------- 群
    @staticmethod
    def _chat_from_entity(ent, about="", linked=""):
        from telethon.tl.types import Channel, Chat
        kind = "group"
        if isinstance(ent, Channel):
            kind = "channel" if ent.broadcast else "group"
            cid = f"-100{ent.id}"
        elif isinstance(ent, Chat):
            cid = f"-{ent.id}"
        else:
            cid = str(ent.id)
        return {"chat_id": cid, "title": getattr(ent, "title", "") or "", "username": getattr(ent, "username", "") or "",
                "kind": kind, "members": getattr(ent, "participants_count", 0) or 0, "about": about,
                "linked_chat_id": linked}

    @staticmethod
    def _still_in(ent) -> bool:
        """对话列表里也会留着已经退出 / 被移出 / 已升级的群（ChatForbidden、ChannelForbidden、left、deactivated）。"""
        return not (type(ent).__name__.endswith("Forbidden") or getattr(ent, "left", False)
                    or getattr(ent, "deactivated", False))

    def dialogs(self, acc):
        """账号加入的群和频道（只读群列表本身）。人数、简介、频道的讨论区要逐个请求 GetFullChannel，不在这里一次性做
        （群多的账号一次几十上百个请求，既慢又容易被限流）—— 由 pool.refresh_chat_info 慢慢补。
        注意：iter_dialogs 返回的频道 / 超级群 participants_count 通常是 None（显示成 0），真实人数以补全为准。"""
        async def go():
            c = await self._client(acc)
            out = []
            async for d in c.iter_dialogs():
                if (d.is_group or d.is_channel) and self._still_in(d.entity):
                    out.append(self._chat_from_entity(d.entity))
            return out
        return self._call(go, DIALOGS_TIMEOUT)

    def recent_dialogs(self, acc, limit=30):
        """群列表自动更新的快速检查：一次 messages.GetDialogs，只取最新的 limit 个对话（刚加入的群因为有“加入”
        的系统消息通常排在前面）。total = 账号的对话总数（变了说明加了 / 退了什么，由上层决定要不要完整同步）。"""
        async def go():
            c = await self._client(acc)
            ds = await c.get_dialogs(limit=limit)
            return {"chats": [self._chat_from_entity(d.entity) for d in ds
                              if (d.is_group or d.is_channel) and self._still_in(d.entity)],
                    "total": getattr(ds, "total", None)}
        return self._call(go)

    def full_info(self, acc, chat):
        """一个群 / 频道的真实人数、简介、讨论区（GetFullChannel；普通小群用 GetFullChat）。"""
        from telethon import functions
        from telethon.tl.types import Channel

        async def go():
            c = await self._client(acc)
            ent = await self._entity(c, chat)
            if isinstance(ent, Channel):
                full = (await c(functions.channels.GetFullChannelRequest(ent))).full_chat
                linked = f"-100{full.linked_chat_id}" if getattr(full, "linked_chat_id", None) else ""
                return {"members": full.participants_count or 0, "about": full.about or "", "linked_chat_id": linked}
            full = (await c(functions.messages.GetFullChatRequest(ent.id))).full_chat
            parts = getattr(getattr(full, "participants", None), "participants", None) or []
            return {"members": len(parts) or getattr(ent, "participants_count", 0) or 0, "about": full.about or "",
                    "linked_chat_id": ""}
        return self._call(go)

    async def _entity(self, c, chat):
        from telethon.tl.types import PeerChannel, PeerChat
        cid = str(chat["chat_id"])
        if chat.get("username"):
            return await c.get_entity(chat["username"])
        if cid.startswith("-100"):
            return await c.get_entity(PeerChannel(int(cid[4:])))
        return await c.get_entity(PeerChat(int(cid.lstrip("-"))))

    @staticmethod
    def _msg(m, chat):
        s = m.sender
        username = getattr(s, "username", "") or ""
        name = " ".join(x for x in (getattr(s, "first_name", ""), getattr(s, "last_name", "")) if x) or \
            getattr(s, "title", "") or ""
        cid = str(chat["chat_id"])
        link = f"https://t.me/{chat['username']}/{m.id}" if chat.get("username") else \
            (f"https://t.me/c/{cid[4:]}/{m.id}" if cid.startswith("-100") else "")
        return {"id": m.id, "date": m.date.timestamp() if m.date else 0, "text": m.message or "",
                "sender_id": getattr(s, "id", None) or m.sender_id, "sender_username": username,
                "sender_name": name, "link": link, "reply_to": getattr(m.reply_to, "reply_to_msg_id", None)}

    def read_new(self, acc, chat, after_id, limit):
        async def go():
            c = await self._client(acc)
            ent = await self._entity(c, chat)
            out = []
            async for m in c.iter_messages(ent, min_id=int(after_id), reverse=True, limit=limit):
                if m.message:
                    out.append(self._msg(m, chat))
                elif out is not None:
                    out.append({"id": m.id, "date": 0, "text": "", "sender_id": None, "sender_username": "",
                                "sender_name": "", "link": "", "reply_to": None})
            return out
        return self._call(go)

    def latest_id(self, acc, chat):
        async def go():
            c = await self._client(acc)
            ent = await self._entity(c, chat)
            msgs = await c.get_messages(ent, limit=1)
            return msgs[0].id if msgs else 0
        return self._call(go)

    def id_before(self, acc, chat, ts):
        import datetime

        async def go():
            c = await self._client(acc)
            ent = await self._entity(c, chat)
            # offset_date：返回该时间点之前的最新一条消息
            msgs = await c.get_messages(ent, limit=1, offset_date=datetime.datetime.fromtimestamp(ts, datetime.UTC))
            return msgs[0].id if msgs else 0
        return self._call(go)

    def user_info(self, acc, user_ref):
        from telethon import functions

        async def go():
            c = await self._client(acc)
            ent = await c.get_entity(int(user_ref) if str(user_ref).lstrip("-").isdigit() else user_ref)
            full = await c(functions.users.GetFullUserRequest(ent))
            return {"username": getattr(ent, "username", "") or "", "bio": getattr(full.full_user, "about", "") or "",
                    "name": " ".join(x for x in (getattr(ent, "first_name", ""), getattr(ent, "last_name", "")) if x)}
        return self._call(go)

    def preview(self, acc, link, sample=30):
        from telethon import functions
        kind, val = _link_key(link)

        async def go():
            c = await self._client(acc)
            if kind == "invite":
                r = await c(functions.messages.CheckChatInviteRequest(val))
                title = getattr(r, "title", "") or getattr(getattr(r, "chat", None), "title", "")
                return {"chat_id": "", "title": title, "username": "", "kind": "group",
                        "members": getattr(r, "participants_count", 0) or 0, "about": getattr(r, "about", "") or "",
                        "linked_chat_id": "", "msgs_per_day": 0, "samples": []}
            ent = await c.get_entity(val)
            about = ""
            try:
                full = await c(functions.channels.GetFullChannelRequest(ent))
                about = full.full_chat.about or ""
                members = full.full_chat.participants_count or 0
            except Exception:  # noqa: BLE001
                members = getattr(ent, "participants_count", 0) or 0
            msgs = await c.get_messages(ent, limit=sample)
            texts = [m.message for m in msgs if m.message]
            per_day = 0
            if len(msgs) >= 2 and msgs[0].date and msgs[-1].date:
                span = max(1.0, (msgs[0].date - msgs[-1].date).total_seconds() / 86400)
                per_day = round(len(msgs) / span)
            d = self._chat_from_entity(ent, about=about)
            d["members"] = members
            return {**d, "msgs_per_day": per_day, "samples": texts}
        return self._call(go)

    def join(self, acc, link):
        from telethon import functions
        kind, val = _link_key(link)

        async def go():
            c = await self._client(acc)
            if kind == "invite":
                r = await c(functions.messages.ImportChatInviteRequest(val))
                ent = r.chats[0]
            else:
                ent = await c.get_entity(val)
                await c(functions.channels.JoinChannelRequest(ent))
            return self._chat_from_entity(ent)
        return self._call(go)

    def search(self, acc, query, limit=20):
        from telethon import functions
        from telethon.tl.types import Channel

        async def go():
            c = await self._client(acc)
            r = await c(functions.contacts.SearchRequest(q=query, limit=limit))
            return [self._chat_from_entity(ch) for ch in r.chats if isinstance(ch, Channel) and ch.username]
        return self._call(go)

    def send(self, acc, peer, text, file_path=None):
        async def go():
            c = await self._client(acc)
            target = peer
            try:
                kind, val = _link_key(peer)
                target = val if kind == "public" else peer
            except InvalidLink:
                target = int(peer) if str(peer).lstrip("-").isdigit() else peer
            if file_path:
                await c.send_file(target, file_path, caption=text[:1000] if text else None)
            else:
                await c.send_message(target, text, link_preview=False)
        return self._call(go)

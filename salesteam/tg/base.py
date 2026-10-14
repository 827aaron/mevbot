"""TG 后端接口与错误类型。所有方法都是同步的（真实后端内部用独立的 asyncio 线程）。

acc = {"id", "phone", "session", "api_id", "api_hash", "proxy": {...}, "device": {...}}
chat = {"chat_id", "title", "username", "kind": group|channel|discussion, "members", "about", "linked_chat_id"}
msg  = {"id", "date", "text", "sender_id", "sender_username", "sender_name", "link", "reply_to"}
"""


class TGError(Exception):
    key = "tg_error"

    def __init__(self, detail: str = "", **params):
        super().__init__(detail or self.key)
        self.detail = detail
        self.params = params


class FloodWait(TGError):
    key = "tg_flood_wait"

    def __init__(self, seconds: int):
        super().__init__(f"flood wait {seconds}s", seconds=seconds)
        self.seconds = int(seconds)


class AuthRequired(TGError):      # 掉线 / session 失效 → 需要重新登录
    key = "tg_auth_required"


class Restricted(TGError):        # 账号被限制 / 封禁
    key = "tg_restricted"


class NeedPassword(TGError):      # 需要两步验证密码
    key = "tg_need_password"


class BadCode(TGError):
    key = "tg_bad_code"


class BadPassword(TGError):
    key = "tg_bad_password"


class BadPhone(TGError):
    key = "tg_bad_phone"


class InvalidLink(TGError):
    key = "tg_invalid_link"


class NotMember(InvalidLink):      # 账号已不在这个群（被移出 / 已退出 / 私有群没有权限）→ 标记「已退出」
    pass


class NeedsApproval(TGError):     # 需审核的群：已提交申请
    key = "tg_needs_approval"


class NotConfigured(TGError):     # 没有 api_id / api_hash
    key = "tg_not_configured"


class Backend:
    name = "base"
    is_mock = False

    def send_code(self, acc) -> dict: raise NotImplementedError
    def sign_in(self, acc, login_state: dict, code: str = "", password: str = "") -> dict: raise NotImplementedError
    def check(self, acc) -> dict: raise NotImplementedError
    def dialogs(self, acc) -> list: raise NotImplementedError
    def read_new(self, acc, chat: dict, after_id: int, limit: int) -> list: raise NotImplementedError
    def latest_id(self, acc, chat: dict) -> int: raise NotImplementedError
    def id_before(self, acc, chat: dict, ts: float) -> int: raise NotImplementedError   # 时间点之前最后一条消息的 ID
    def user_info(self, acc, user_ref) -> dict: raise NotImplementedError
    def recent_dialogs(self, acc, limit: int = 30) -> dict: raise NotImplementedError  # {"chats": 最新的群/频道, "total": 对话总数}
    def full_info(self, acc, chat: dict) -> dict: raise NotImplementedError     # {"members", "about", "linked_chat_id"}
    def preview(self, acc, link: str, sample: int = 30) -> dict: raise NotImplementedError
    def join(self, acc, link: str) -> dict: raise NotImplementedError
    def search(self, acc, query: str, limit: int = 20) -> list: raise NotImplementedError
    def send(self, acc, peer: str, text: str, file_path: str | None = None) -> None: raise NotImplementedError
    def logout(self, acc) -> None: raise NotImplementedError    # 只断开本机连接（换代理 / 删除账号时）
    def sign_out(self, acc) -> bool: raise NotImplementedError  # 真正退出登录：TG 删掉这个会话（手机「设备」里消失）

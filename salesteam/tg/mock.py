"""【仅供开发测试】假 TG 世界：不联网、不登录任何真实账号、不发任何真实消息。

- 登录：验证码固定 12345；手机号以 0 结尾的账号需要两步验证密码 test
- 特殊号码：含 666 → 登录后被限制；含 777 → 进群时遇到 FloodWait
- 每个群按时间“产生”新消息（每 6–20 分钟一条），内容有求购 / 广告 / 闲聊，多语言，带发言人用户名和简介
- 发送的消息只记在本地 outbox 表（status=mock）
"""
import hashlib
import random
import time

from .base import (AuthRequired, Backend, BadCode, BadPassword, BadPhone, FloodWait, InvalidLink, NeedPassword,
                   NeedsApproval, NotMember, Restricted)

BASE = 1_790_000_000  # 固定起点，消息 ID 由时间推算
CODE = "12345"
PASSWORD = "test"

CHATS = [
    # chat_id, title, username, kind, members, about, period_min, mix(buy, ad), linked
    # v1 场景（PRD §1）：马来西亚 / 新加坡的群，中文 / 英文 / 马来语；另有一个波兰语群用来测试“其他语言跳过”
    ("-1001001", "Solar Traders Malaysia 🌞", "solar_traders_my", "group", 4200,
     "B2B group for PV distributors, installers and wholesalers in Malaysia (KL, Selangor, Johor, Penang)", 8,
     (0.25, 0.25), ""),
    ("-1001002", "Kumpulan Pemborong Solar Malaysia", "pemborong_solar_my", "group", 1800,
     "Kumpulan untuk pemborong dan pemasang solar di Malaysia", 12, (0.2, 0.2), ""),
    ("-1001003", "SG Electrical Wholesale", "sg_electrical_ws", "group", 9600,
     "Singapore electrical & solar wholesale buyers and sellers", 6, (0.15, 0.45), ""),
    ("-1001004", "Crypto Signals VIP 🚀", "crypto_vip_signals", "group", 25000,
     "Daily signals. DM admin for VIP.", 5, (0.0, 0.8), ""),
    ("-1001005", "Solar News Asia", "solar_news_asia", "channel", 12000,
     "Daily PV industry news for Southeast Asia", 20, (0.0, 0.1), "-1001006"),
    ("-1001006", "Solar News Asia — Chat", "solar_news_asia_chat", "discussion", 3100,
     "Discussion group of Solar News Asia", 10, (0.25, 0.15), ""),
    ("-1001007", "新马太阳能批发交流群", "xinma_solar_pifa", "group", 5400,
     "新加坡、马来西亚太阳能 / 储能 批发商、安装商交流", 9, (0.25, 0.25), ""),
    ("-1001008", "PV Instalatorzy Polska", "pv_instalatorzy_pl", "group", 1500,
     "Grupa dla instalatorów fotowoltaiki w Polsce", 12, (0.3, 0.1), ""),
]
PUBLIC_EXTRA = [  # AI 找群（C）时能搜到、但账号还没加入的公开群
    ("-1002001", "Johor Solar Installers", "johor_solar_installers", "group", 2600,
     "Installers and contractors in Johor Bahru and Iskandar", 10, (0.25, 0.2), ""),
    ("-1002002", "Battery Storage Buyers SG/MY", "battery_buyers_sgmy", "group", 1900,
     "Buyers of home and C&I battery storage in Singapore and Malaysia", 12, (0.3, 0.2), ""),
    ("-1002003", "Cheap Phones Wholesale 💰", "cheap_phones_ws", "group", 30000,
     "Best prices!!! DM for list", 4, (0.05, 0.85), ""),
    ("-1002004", "Private Solar Club KL", "", "group", 800, "Invite only — requires admin approval", 15, (0.3, 0.2), ""),
]

BUY = [
    ("en", "Hi all, we are a distributor in {country} looking for {qty} units of {product}. Please DM with price and MOQ."),
    ("en", "Need a reliable supplier of {product} for a {qty}-unit project in {country}. Who can quote CIF Port Klang?"),
    ("ms", "Saya nak cari pembekal {product}, perlukan {qty} unit sebulan, hantar ke {country}. Minta sebut harga."),
    ("ms", "Ada supplier {product}? Harga borong untuk {qty} unit, projek di {country}. PM saya."),
    ("zh", "我们在{country}做工程，需要采购 {qty} 台{product}，请有货源的朋友报价"),
    ("en", "Our current supplier keeps missing delivery dates. Looking for an alternative manufacturer of {product}, ~{qty} pcs/quarter, {country}."),
    ("pl", "Szukam dostawcy {product}, potrzebujemy ok. {qty} sztuk miesięcznie, Polska. Proszę o ofertę."),
]
AD = [
    "🔥 We sell {product} at factory price!!! MOQ 1 pc. DM now 👉",
    "Promo: {product} cheapest in market, contact @{seller} for price list",
    "Join our VIP channel for 100x signals 🚀🚀🚀",
    "Jual stok {product}, diskaun 30% minggu ini sahaja. WhatsApp +60 12-000 0000",
    "工厂直供{product}，全马最低价，私聊 @{seller}",
]
CHAT = [
    "Good morning everyone ☀️", "Anyone know the new NEM 3.0 quota for this year?", "Thanks for the info!",
    "Which inverter brand do you prefer for residential?", "Terima kasih bos, info yang berguna",
    "Has anyone had problems with customs clearance at Port Klang recently?", "👍", "大家早安，今天雨好大",
]
PRODUCTS = ["5kW hybrid solar inverters", "LiFePO4 battery packs", "10kW three-phase inverters",
            "solar panels 550W", "micro inverters"]
COUNTRIES = ["Kuala Lumpur", "Selangor", "Johor Bahru", "Penang", "Singapore", "Malaysia"]
FIRST = ["Ahmad", "Siti", "Wei Ming", "Mei Ling", "Raj", "Nurul", "Jason", "Aisyah", "Kelvin", "Farid"]
COMPANY = ["Volt Distribution", "SunGrid Supply", "EcoPower Trade", "Helion Wholesale", "GreenWatt", "Solaris Group"]


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def _all_chats():
    return {c[0]: c for c in CHATS + PUBLIC_EXTRA}


def _chat_dict(c):
    return {"chat_id": c[0], "title": c[1], "username": c[2], "kind": c[3], "members": c[4], "about": c[5],
            "linked_chat_id": c[8]}


def _period_s(c):
    return c[6] * 60


def _latest(c, now=None):
    return int(((now or time.time()) - BASE) // _period_s(c))


def _message(c, mid):
    r = random.Random(_h(c[0], mid))
    buy_p, ad_p = c[7]
    x = r.random()
    uid = 5000 + r.randrange(60)
    first = FIRST[uid % len(FIRST)]
    company = COMPANY[uid % len(COMPANY)]
    username = f"{first.lower().replace(' ', '')}_{company.split()[0].lower()}{uid % 7}"
    product = r.choice(PRODUCTS)
    if x < buy_p:
        lg, tpl = r.choice(BUY)
        text = tpl.format(product=product, qty=r.choice([50, 200, 500, 1000, 2000]), country=r.choice(COUNTRIES))
        kind = "buy"
    elif x < buy_p + ad_p:
        text = r.choice(AD).format(product=product, seller=username)
        kind = "ad"
    else:
        text = r.choice(CHAT)
        kind = "chat"
    link = f"https://t.me/{c[2]}/{mid}" if c[2] else f"https://t.me/c/{c[0].removeprefix('-100')}/{mid}"
    return {"id": mid, "date": BASE + mid * _period_s(c), "text": text, "sender_id": uid,
            "sender_username": username, "sender_name": f"{first} ({company})", "link": link, "reply_to": None,
            "_kind": kind}


class MockBackend(Backend):
    name = "mock"
    is_mock = True

    def __init__(self):
        self.joined: dict[str, set] = {}   # phone -> 额外加入的群（按加入顺序排在对话列表最前面）
        self.left: dict[str, set] = {}     # phone -> 已经退出 / 被移出的群（测试用：模拟在手机上退群）
        self.dialog_calls = 0              # 测试用：读最新对话（快速检查）的次数
        self.ops: dict[str, int] = {}
        self.signed_out: list[str] = []    # 测试用：真正退出登录过的号码
        self.sign_out_error = None         # 测试用：设成一个异常 → 模拟退出时网络出错

    # -------------------------------------------------- 登录
    def send_code(self, acc):
        phone = acc["phone"]
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 7:
            raise BadPhone("invalid phone")
        return {"phone_code_hash": "mock-" + digits[-4:], "sent_at": time.time()}

    def sign_in(self, acc, login_state, code="", password=""):
        phone = acc["phone"]
        if login_state.get("need_password") or (code and phone.endswith("0") and not password):
            if code and code.strip() != CODE and not login_state.get("need_password"):
                raise BadCode("bad code")
            if not password:
                raise NeedPassword()
            if password != PASSWORD:
                raise BadPassword()
        elif code.strip() != CODE:
            raise BadCode("bad code")
        digits = "".join(ch for ch in phone if ch.isdigit())
        return {"session": f"mock-session:{digits}",
                "user": {"id": _h(phone) % 10**9, "username": f"user{digits[-4:]}", "name": f"Test {digits[-4:]}"}}

    def _guard(self, acc):
        if not str(acc.get("session") or "").startswith("mock-session:"):
            raise AuthRequired("no session")
        if "666" in acc["phone"]:
            self.ops[acc["phone"]] = self.ops.get(acc["phone"], 0) + 1
            if self.ops[acc["phone"]] > 2:
                raise Restricted("account limited (mock)")

    def check(self, acc):
        self._guard(acc)
        digits = "".join(ch for ch in acc["phone"] if ch.isdigit())
        return {"id": _h(acc["phone"]) % 10**9, "username": f"user{digits[-4:]}", "name": f"Test {digits[-4:]}"}

    def logout(self, acc):
        return None

    def sign_out(self, acc):
        self.signed_out.append(acc["phone"])
        if self.sign_out_error:
            raise self.sign_out_error
        return True

    # -------------------------------------------------- 群
    def _my_chat_ids(self, acc):
        r = random.Random(_h("dialogs", acc["phone"]))
        base = [c[0] for c in CHATS]
        mine = [cid for cid in base if r.random() < 0.85] or base[:3]
        if "-1001005" in mine and "-1001006" not in mine:
            mine.append("-1001006")
        left = self.left.get(acc["phone"], set())
        return [cid for cid in mine + sorted(self.joined.get(acc["phone"], set())) if cid not in left]

    def recent_dialogs(self, acc, limit=30):
        """最新的 limit 个对话：刚加入的群在最前面（真实 TG 里因为有“加入”的系统消息）。total 另含 5 个私聊。"""
        self._guard(acc)
        self.dialog_calls += 1
        allc = _all_chats()
        ids = self._my_chat_ids(acc)
        newest = [cid for cid in reversed(ids)][:limit]
        out = []
        for cid in newest:
            if cid in allc:
                d = _chat_dict(allc[cid])
                if d["members"] > 1000 or d["kind"] == "channel":
                    d["members"] = 0
                d["linked_chat_id"] = ""
                d["about"] = ""
                out.append(d)
        return {"chats": out, "total": len(ids) + 5}

    def dialogs(self, acc):
        """和真实 TG 一样：读群列表时大群 / 频道的人数拿不到（0），频道的讨论区也不知道，要 full_info 逐个补。"""
        self._guard(acc)
        allc = _all_chats()
        out = []
        for cid in self._my_chat_ids(acc):
            if cid in allc:
                d = _chat_dict(allc[cid])
                if d["members"] > 1000 or d["kind"] == "channel":
                    d["members"] = 0
                d["linked_chat_id"] = ""
                d["about"] = ""
                out.append(d)
        return out

    def full_info(self, acc, chat):
        self._guard(acc)
        self.full_calls = getattr(self, "full_calls", 0) + 1
        c = _all_chats().get(str(chat["chat_id"]))
        if not c:
            raise InvalidLink(str(chat["chat_id"]))
        return {"members": c[4], "about": c[5], "linked_chat_id": c[8]}

    def latest_id(self, acc, chat):
        c = _all_chats().get(str(chat["chat_id"]))
        return _latest(c) if c else 0

    def id_before(self, acc, chat, ts):
        c = _all_chats().get(str(chat["chat_id"]))
        return max(0, int((ts - BASE) // _period_s(c))) if c else 0

    def read_new(self, acc, chat, after_id, limit):
        self._guard(acc)
        if str(chat["chat_id"]) in self.left.get(acc["phone"], set()):
            raise NotMember("ChannelPrivateError (mock)")
        c = _all_chats().get(str(chat["chat_id"]))
        if not c:
            return []
        last = _latest(c)
        start = max(int(after_id) + 1, last - 20000)
        out = []
        for mid in range(start, last + 1):
            m = _message(c, mid)
            m.pop("_kind", None)
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def user_info(self, acc, user_ref):
        uid = int(user_ref) if str(user_ref).isdigit() else _h(user_ref) % 60 + 5000
        first = FIRST[uid % len(FIRST)]
        company = COMPANY[uid % len(COMPANY)]
        return {"username": f"{first.lower().replace(' ', '')}_{company.split()[0].lower()}{uid % 7}", "name": f"{first}",
                "bio": (f"Purchasing @ {company} (TEST DATA)" + (f" · WhatsApp +6012{uid:07d}" if uid % 2 else ""))
                if uid % 3 else ""}

    def _find(self, link):
        key = str(link).strip().lower()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/", "@"):
            if key.startswith(prefix):
                key = key[len(prefix):]
        key = key.strip("/")
        for c in CHATS + PUBLIC_EXTRA:
            if c[2] and c[2].lower() == key:
                return c
        if key.startswith("+") or key.startswith("joinchat"):
            return PUBLIC_EXTRA[3]
        return None

    def preview(self, acc, link, sample=30):
        c = self._find(link)
        if not c:
            raise InvalidLink(link)
        last = _latest(c)
        msgs = [_message(c, mid) for mid in range(max(0, last - sample + 1), last + 1)] if c[2] else []
        per_day = round(24 * 60 / c[6])
        return {**_chat_dict(c), "msgs_per_day": per_day,
                "samples": [m["text"] for m in msgs][-sample:]}

    def join(self, acc, link):
        self._guard(acc)
        if "777" in acc["phone"]:
            raise FloodWait(120)
        c = self._find(link)
        if not c:
            raise InvalidLink(link)
        if not c[2]:
            raise NeedsApproval("join request sent")
        self.joined.setdefault(acc["phone"], set()).add(c[0])
        self.left.get(acc["phone"], set()).discard(c[0])
        return _chat_dict(c)

    def search(self, acc, query, limit=20):
        self._guard(acc)
        q = (query or "").lower()
        words = [w for w in q.split() if len(w) > 2]
        out = []
        for c in CHATS + PUBLIC_EXTRA:
            if not c[2]:
                continue
            hay = (c[1] + " " + c[5]).lower()
            if not words or any(w in hay for w in words) or "solar" in q:
                out.append(_chat_dict(c))
        return out[:limit]

    def send(self, acc, peer, text, file_path=None):
        self._guard(acc)
        return None   # 调用方负责记录到 outbox（status=mock）


def message_kind(chat_id: str, mid: int) -> str:
    """测试用：返回假消息的真实类别（buy/ad/chat），用于核对监控员的判断。"""
    c = _all_chats().get(str(chat_id))
    return _message(c, mid)["_kind"] if c else ""

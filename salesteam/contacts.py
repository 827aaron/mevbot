"""联系方式识别：TG 用户名、WhatsApp（PRD 5.1 第 4 条 / 7.1）。只收集，第一版不通过 WhatsApp 发送。

- 明确的 WhatsApp：wa.me / api.whatsapp.com 链接、标注“WhatsApp”的号码或按钮、本人说明是 WhatsApp → 可计入合格
- 普通电话号码：只标“可能是 WhatsApp”（未核实）→ 不计入合格
- TG：t.me/用户名 链接、@用户名（来自 TG 消息发言人等）
"""
import re

WA_LINK = re.compile(r"(?:https?://)?(?:wa\.me/|api\.whatsapp\.com/send/?\?(?:[^\"'\s]*&)?phone=)\+?(\d{7,15})", re.I)
WA_LABEL = re.compile(r"whats\s*app[^0-9+\n]{0,20}(\+?\d[\d\s\-().]{6,20}\d)", re.I)
PHONE = re.compile(r"(?<![\w/])(\+\d[\d\s\-().]{7,18}\d)")
TG_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?![\w/])", re.I)
TG_RESERVED = {"joinchat", "addstickers", "share", "iv", "proxy", "socks", "s", "c"}


def norm_phone(s: str) -> str:
    """统一成国际格式 +国家码号码，便于去重 / 黑名单比对（v1：马来西亚 +60、新加坡 +65 的本地写法也能识别）。
    +60 12-345 6789 / 60123456789 / 0060123456789 / 012-345 6789 → +60123456789；9123 4567 → +6591234567"""
    raw = (s or "").strip()
    d = re.sub(r"\D", "", raw)
    if not d:
        return ""
    if raw.startswith("+"):
        pass
    elif d.startswith("00"):
        d = d[2:]
    elif d.startswith("0") and 9 <= len(d) <= 11:
        d = "60" + d[1:]                 # 马来西亚本地写法（中继前缀 0）
    elif len(d) == 8 and d[0] in "3689":
        d = "65" + d                     # 新加坡本地 8 位号码
    return ("+" + d) if 7 <= len(d) <= 15 else ""


def extract(text: str, links: list[str] | None = None) -> dict:
    blob = (text or "") + " " + " ".join(links or [])
    wa = []
    for m in WA_LINK.finditer(blob):
        n = norm_phone(m.group(1))
        if n and n not in wa:
            wa.append(n)
    for m in WA_LABEL.finditer(text or ""):
        n = norm_phone(m.group(1))
        if n and n not in wa:
            wa.append(n)
    maybe = []
    for m in PHONE.finditer(text or ""):
        n = norm_phone(m.group(1))
        if n and n not in wa and n not in maybe:
            maybe.append(n)
    tg = []
    for m in TG_LINK.finditer(blob):
        u = m.group(1)
        if u.lower() not in TG_RESERVED and ("@" + u) not in tg:
            tg.append("@" + u)
    return {"whatsapp": wa, "phones": maybe[:5], "telegram": tg[:5]}


def qualifying(contacts: list[dict]) -> bool:
    """合格线索的联系方式条件：已核实的 TG 用户名，或明确的 WhatsApp。只有邮箱 / 普通电话不算。"""
    return any((c.get("telegram") and c.get("verified")) or (c.get("whatsapp") and c.get("wa_explicit"))
               for c in contacts)

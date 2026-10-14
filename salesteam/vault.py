"""本地加密：API Key、搜索 Key 等用 Fernet（AES-128-CBC + HMAC）加密后存进 SQLite。

主密钥保存在 data/.secret.key（权限 600），只存在客户电脑上。
说明：主密钥与数据库在同一目录，能防止“数据库文件被单独拷走 / 误发”时泄露 Key，
但不能防御已控制这台电脑的人。第 5 步打包时可改为存入系统钥匙串（见 docs/DECISIONS.md）。
"""
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from . import paths

_lock = threading.Lock()
_fernet: Fernet | None = None


def _get() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is None:
            paths.ensure_dirs()
            if paths.KEY_PATH.exists():
                key = paths.KEY_PATH.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                fd = os.open(paths.KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(key)
            _fernet = Fernet(key)
        return _fernet


def reset():
    global _fernet
    with _lock:
        _fernet = None


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _get().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _get().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask(plain: str) -> str:
    """只给界面显示前后几位。"""
    if not plain:
        return ""
    if len(plain) <= 10:
        return "•" * 6
    return plain[:5] + "…" + plain[-4:]


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00a0"), None)


def clean_secret(s) -> str:
    """粘贴进来的 Key：去掉前后空白 / 换行、零宽字符、两边的引号（"…"、“…”、'…'、`…`）。Key 本身不含空白。"""
    v = str(s or "").translate(_ZERO_WIDTH).strip()
    while len(v) >= 2 and v[0] in "\"'“”‘’`" and v[-1] in "\"'“”‘’`":
        v = v[1:-1].strip()
    return "".join(v.split())


_KEYLIKE = None


def provider_said(text, limit: int = 220) -> str:
    """服务商返回的原始错误文字：取 JSON 里的 message，把 Key 之类的长串打码，截短。给客户看，方便判断原因。"""
    import json
    import re
    global _KEYLIKE
    t = str(text or "").strip()
    if not t:
        return ""
    try:
        d = json.loads(t)
        if isinstance(d, dict):
            e = d.get("error", d)
            t = (e.get("message") if isinstance(e, dict) else str(e)) or d.get("message") or t
    except (ValueError, TypeError):
        pass
    if _KEYLIKE is None:
        _KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|[A-Za-z0-9_\-]{24,})")
    t = _KEYLIKE.sub(lambda m: "****" + m.group(0)[-4:], " ".join(str(t).split()))
    return t[:limit] + ("…" if len(t) > limit else "")

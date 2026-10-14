"""后端翻译（用于发到 TG 的线索卡片、异常提醒、桌面通知等不经过网页的文字）。与前端共用 web/i18n/*.json。"""
import json
import re
from functools import lru_cache

from . import paths, settings


@lru_cache(maxsize=4)
def _dict(lang: str) -> dict:
    try:
        return json.loads((paths.WEB_DIR / "i18n" / f"{lang}.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def reload():
    _dict.cache_clear()


def lang() -> str:
    return settings.get("ui_lang") or "zh"


def t(key: str, params: dict | None = None, lg: str | None = None) -> str:
    lg = lg or lang()
    s = _dict(lg).get(key)
    if s is None:
        s = _dict("en").get(key, key)
    params = params or {}

    def rep(m):
        v = params.get(m.group(1), "")
        if isinstance(v, str) and v.startswith("@"):
            return ("、" if lg == "zh" else ", ").join(t(x.lstrip("@"), None, lg) for x in v.split(","))
        if m.group(1) == "error" and isinstance(v, str):
            return t("errors." + v, params, lg)
        if m.group(1) == "country" and isinstance(v, str) and len(v) == 2:
            from .countries import name
            return name(v, "zh" if lg == "zh" else "en")
        return str(v)
    out = re.sub(r"\{(\w+)\}", rep, s)
    return re.sub(r"<[^>]+>", "", out)   # 去掉文案里的 HTML（如 <b>）

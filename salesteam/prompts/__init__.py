"""提示词库（PRD 2.4.2）：所有给 AI 的提示词文字都在这里，和代码分开；提示词属于客户，客户可以查看和修改。

- 默认提示词：本目录 default/<名称>.md，随软件提供。只写“怎么干活”（岗位职责、怎么用工具、输出格式），
  **不含任何业务内容**（产品、目标客户、行业知识、关键词、什么算好线索、销售话术）——业务内容全部在运行时
  来自客户的输入（快速开始读到的官网 / 资料、补充访谈、和主管的对话）→ 客户画像 / 理想客户模板 / 备忘录。
- 客户修改：保存在本机数据库 prompt_overrides 表，优先于默认；“恢复默认”即删除修改。
- 以后可以不随软件提供默认提示词：缺少时 get() 报 prompt_missing（首次使用时如何生成提示词待老板决定）。
- 不属于提示词、留在程序里的硬规则：输出格式（JSON schema）校验、预算与每个账号的上限、高风险开关、判重、
  合格硬性检查。
- 占位符写法 {{名称}}，由程序填入（例如 {{lang}}、{{icp}}）。客户删掉占位符不会出错，只是那部分内容不再带上。
- 选择题（判断模型用）：第一行 "? 问题"，之后每行 "选项键: 说明"。选项键由程序使用，客户只能改说明文字。
"""
import re
import time
from functools import lru_cache
from pathlib import Path

from .. import db
from ..errors import UserError

VERSION = "2026.10-p4"          # 默认提示词版本（改了默认文字就改这里）
DIR = Path(__file__).parent / "default"
_VAR = re.compile(r"{{\s*([a-z_]+)\s*}}")

# 名称 → 属于哪个 AI（界面分组用）。名称前缀即文件名前缀。
AGENT_OF = {"common": "all", "layer": "all", "chat": "all", "manager": "manager", "icp": "manager",
            "quickstart": "manager", "quality": "manager", "researcher": "researcher", "monitor": "monitor",
            "analyst": "analyst", "scout": "scout", "learning": "analyst", "decision": "decider"}


def _agent(name: str) -> str:
    if name.startswith("role."):
        return name.split(".", 1)[1]
    return AGENT_OF.get(name.split(".", 1)[0], "all")


@lru_cache(maxsize=None)
def default_text(name: str) -> str | None:
    p = DIR / f"{name}.md"
    if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", name) or not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip("\n")


def names() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.md"))


def _override(name: str) -> str | None:
    try:
        r = db.query_one("SELECT text FROM prompt_overrides WHERE name=?", (name,))
    except Exception:  # noqa: BLE001  数据库还没初始化（例如导入时）
        return None
    return r["text"] if r else None


def get(name: str) -> str:
    t = _override(name)
    if t is None:
        t = default_text(name)
    if t is None:
        raise UserError("prompt_missing", name=name)
    return t


def render(name: str, **kw) -> str:
    """取提示词并填入占位符；没提供的占位符换成空字符串。"""
    return _VAR.sub(lambda m: str(kw.get(m.group(1), "")), get(name))


def options(name: str) -> tuple[str, dict]:
    """选择题：返回 (问题, {选项键: 说明})。客户改过的版本缺少某个选项或多了选项时，选项键以默认为准。"""
    def parse(text):
        q, opts = "", {}
        for line in (text or "").splitlines():
            line = line.strip()
            if line.startswith("?"):
                q = line[1:].strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                if re.fullmatch(r"[a-z_]+", k.strip()):
                    opts[k.strip()] = v.strip()
        return q, opts
    dq, dopts = parse(default_text(name) or "")
    q, opts = parse(get(name))
    if dopts:
        opts = {k: (opts.get(k) or dopts[k]) for k in dopts}
    return q or dq, opts


def variables(name: str) -> list[str]:
    return list(dict.fromkeys(_VAR.findall(default_text(name) or "")))


# ------------------------------------------------------------------ 查看 / 修改（客户）

def list_all() -> list[dict]:
    rows = {r["name"]: r for r in db.query("SELECT name, text, updated_at FROM prompt_overrides")}
    out = []
    for n in names():
        o = rows.get(n)
        out.append({"name": n, "agent": _agent(n), "text": o["text"] if o else default_text(n),
                    "default": default_text(n), "edited": bool(o), "updated_at": o["updated_at"] if o else None,
                    "vars": variables(n), "choice": n.startswith("decision.")})
    return out


def save(name: str, text: str) -> dict:
    if default_text(name) is None:
        raise UserError("prompt_missing", 404, name=name)
    text = (text or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        raise UserError("prompt_empty")
    if len(text) > 20000:
        raise UserError("prompt_too_long", n=20000)
    if text == default_text(name):
        return reset(name)
    db.execute("INSERT INTO prompt_overrides(name, text, updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
               "text=excluded.text, updated_at=excluded.updated_at", (name, text, time.time()))
    return {"name": name, "edited": True}


def reset(name: str) -> dict:
    db.execute("DELETE FROM prompt_overrides WHERE name=?", (name,))
    return {"name": name, "edited": False}

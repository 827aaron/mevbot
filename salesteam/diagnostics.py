"""Bounded, local diagnostic export. Business files and databases stay on disk."""
from datetime import datetime, timezone
from io import BytesIO
import importlib.metadata
import json
import platform
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit
import zipfile

from . import VERSION, db, edition, paths, settings, vault

MAX_TASKS = 20
MAX_EVENTS_PER_TASK = 100
MAX_EVENTS = 500
MAX_LOG_BYTES = 256 * 1024
_REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(r"(?i)(?:password|passwd|secret|credential|authorization|cookie|signature|"
                          r"api[_-]?key|api[_-]?hash|access[_-]?token|refresh[_-]?token|phone_code|session|"
                          r"(?:^|[-_])token(?:$|[-_])|^(?:key|token|sig|otp|auth|authentication|jwt)$|_enc$)")
_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_COOKIE = re.compile(r"([\"']?(?:cookie|set-cookie)[\"']?\s*[:=]\s*)"
                     r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]+)", re.I)
_LABELED = re.compile(r"([\"']?(?:api[_ -]?key|key|authorization|auth(?:entication)?|token|"
                      r"(?:access|refresh|id)[_-]?token|x[-_]auth[-_]token|"
                      r"password|passwd|secret|session(?:_string)?(?:_enc)?|api_hash|phone_code_hash|"
                      r"cookie|set-cookie|x-api-key)[\"']?\s*[:=]\s*)"
                      r"(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}]+)", re.I)
_KEYLIKE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{6,}|github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+|"
                     r"AIza[A-Za-z0-9_-]+|[A-Za-z0-9_-]{43}=|gAAAAA[A-Za-z0-9_=-]{30,})")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\w)(?:\+\d[\d ()-]{7,}\d|\d{10,15})(?!\w)")
_HOME = re.compile(r"(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:[\\/]Users[\\/][^\\/\s]+)", re.I)
_EVENT_PARAMS = {"q", "query", "keyword", "n", "count", "reason", "detail", "error", "provider", "source",
                 "url", "code", "model", "ms", "qualified", "target", "backup", "discarded", "filtered",
                 "errors", "pages", "countries", "lang", "action", "steps", "cost", "result", "stage",
                 "grade", "processed", "pool", "queries", "messages", "chats", "keyword_hits", "prescreened"}
_TASK_PARAMS = {"sources", "target", "countries", "languages", "keywords", "query", "keyword", "icp_id",
                "manager_query_offset", "schedule_id", "created_by", "goal_date"}
_STATS = {"pool", "processed", "qualified", "backup", "discarded", "duplicate", "filtered", "errors", "pages",
          "queries", "messages", "chats", "keyword_hits", "prescreened", "source_notes", "search_results",
          "source_states", "source_status", "search_diagnostics", "discovered",
          "source_new_items", "source_unchanged_items", "source_changed_items"}


def _known_values():
    values = []
    def decrypted(token):
        # Exporting diagnostics must not recreate a missing encryption key.
        return vault.decrypt(token) if paths.KEY_PATH.is_file() else ""

    for row in db.query("SELECT value_enc FROM secrets") + db.query("SELECT api_key_enc AS value_enc FROM agent_configs"):
        values.extend((row["value_enc"], decrypted(row["value_enc"])))
    for row in db.query("SELECT phone, session_enc, login_state, proxy FROM tg_accounts"):
        values.extend((row["phone"], row["session_enc"], decrypted(row["session_enc"])))
        for key in ("login_state", "proxy"):
            parsed = db.loads(row[key], {})
            for name, value in (parsed.items() if isinstance(parsed, dict) else []):
                if _SECRET_NAME.search(name) and isinstance(value, str):
                    values.append(value)
                    if name.endswith("_enc"):
                        values.append(decrypted(value))
    variants = {variant for value in values if value for variant in (value, quote(value, safe=""), quote_plus(value))}
    return sorted(variants, key=len, reverse=True)


class Redactor:
    def __init__(self, values):
        self.values = values

    def text(self, value):
        text = str(value)
        for secret in self.values:
            text = text.replace(secret, _REDACTED)
        text = text.replace(str(paths.DATA_DIR), "[DATA_DIR]")

        def url(match):
            try:
                parsed = urlsplit(match.group())
                query = [(k, _REDACTED if _SECRET_NAME.search(k) else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)]
                return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, urlencode(query), ""))
            except ValueError:
                return "[URL]"

        text = _URL.sub(url, text)
        text = _COOKIE.sub(lambda m: m[1] + _REDACTED, text)
        text = _AUTH.sub(lambda m: m[1] + " " + _REDACTED, text)
        text = _LABELED.sub(lambda m: m[1] + _REDACTED, text)
        text = _KEYLIKE.sub(_REDACTED, text)
        text = _EMAIL.sub("[EMAIL]", text)
        text = _PHONE.sub("[PHONE]", text)
        return _HOME.sub("[HOME]", text)

    def data(self, value, depth=0):
        if depth > 6:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            return {self.text(k): self.data(v, depth + 1) for k, v in list(value.items())[:80]
                    if not _SECRET_NAME.search(str(k)) or isinstance(v, bool)}
        if isinstance(value, list):
            return [self.data(v, depth + 1) for v in value[:MAX_EVENTS_PER_TASK]]
        if isinstance(value, str):
            return self.text(value)[:2000]
        return value


def _only(value, keys):
    return {key: value[key] for key in keys if isinstance(value, dict) and key in value}


def _snapshot():
    # Only database reads under the short database lock; no file I/O or network.
    with db._lock:
        redactor = Redactor(_known_values())
        configs = {
            "agents": db.query("SELECT agent,provider,preset,model,effort,base_url,api_key_enc!='' AS key_present FROM agent_configs"),
            "relays": [{**_only(r, {"id", "kind", "preset", "base_url", "listed", "manual_models"}),
                        "key_present": bool(db.query_one("SELECT name FROM secrets WHERE name=?", ("relay_key:" + r["id"],)))}
                       for r in settings.get("relays", []) or []],
            "decision": _only(settings.get("decision", {}) or {}, {"provider", "relay", "model", "fallback_active", "fallback_reason"}),
            "paused": settings.ai_paused(),
            "search": _only(settings.get("search"), set(settings.DEFAULTS["search"])),
            "crawler": _only(settings.get("crawler"), set(settings.DEFAULTS["crawler"])),
            "analyst": _only(settings.get("analyst"), set(settings.DEFAULTS["analyst"])),
            "agents_enabled": settings.get("agents_enabled"),
            "source_keys_present": {name: bool(db.query_one("SELECT name FROM secrets WHERE name=?", (name,)))
                                    for name in ("serper_api_key", "brave_api_key", "youtube_api_key")},
        }
        for agent in configs["agents"]:
            agent["key_present"] = bool(agent["key_present"])
        recent = db.query("SELECT id,kind,icp_id,status,target,stats,result,error,created_by,created_at,started_at,finished_at,"
                          "substr(params,1,16384) AS params FROM tasks ORDER BY created_at DESC,id DESC LIMIT ?", (MAX_TASKS,))
        remaining = MAX_EVENTS
        for task in recent:
            task["params"] = _only(db.loads(task["params"], {}), _TASK_PARAMS)
            task["stats"] = _only(db.loads(task["stats"], {}), _STATS)
            events = db.query("SELECT seq,ts,agent,level,key AS event_key,substr(params,1,16384) AS params FROM task_events "
                              "WHERE task_id=? ORDER BY seq DESC LIMIT ?", (task["id"], min(remaining, MAX_EVENTS_PER_TASK)))
            remaining -= len(events)
            for event in events:
                event["params"] = _only(db.loads(event["params"], {}), _EVENT_PARAMS)
            task["events"] = list(reversed(events))
            task["raw_counts"] = db.query("SELECT source,status,reason,COUNT(*) AS count FROM raw_leads WHERE task_id=? "
                                          "GROUP BY source,status,reason ORDER BY count DESC LIMIT 100", (task["id"],))
        summary = {
            "task_status_counts": db.query("SELECT status,COUNT(*) AS count FROM tasks GROUP BY status"),
            "raw_status_counts": db.query("SELECT source,status,COUNT(*) AS count FROM raw_leads GROUP BY source,status"),
            "lead_status_counts": db.query("SELECT status,qualified,COUNT(*) AS count FROM leads GROUP BY status,qualified"),
            "failure_reasons": db.query("SELECT status,reason,COUNT(*) AS count FROM raw_leads WHERE reason!='' "
                                         "GROUP BY status,reason ORDER BY count DESC LIMIT 100"),
        }
    return redactor, configs, recent, summary


def _log_tail(name, redactor):
    path = paths.DATA_DIR / name
    if path.is_symlink():
        return "[symbolic link skipped]\n", "skipped"
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            start = max(0, size - MAX_LOG_BYTES)
            file.seek(start)
            raw = file.read(MAX_LOG_BYTES)
        # Do not export a partial credential at the beginning of a truncated line.
        if start:
            raw = raw.partition(b"\n")[2]
        text = redactor.text(raw.decode("utf-8", errors="replace"))
        text = text.encode("utf-8")[-MAX_LOG_BYTES:].decode("utf-8", errors="ignore")
        return text, "tail" if start else "complete"
    except FileNotFoundError:
        return "[log not present]\n", "missing"
    except OSError:
        return "[log unavailable]\n", "unavailable"


def build():
    redactor, config, recent, summary = _snapshot()
    generated = datetime.now(timezone.utc)
    manifest = {"format": 1, "version": VERSION, "edition": edition.current()["id"], "generated_at": generated.isoformat(),
                "runtime": {"os": platform.system(), "os_version": platform.release(),
                            "architecture": platform.machine(), "python": platform.python_version()},
                "limits": {"tasks": MAX_TASKS, "events_per_task": MAX_EVENTS_PER_TASK,
                           "events_total": MAX_EVENTS, "bytes_per_log": MAX_LOG_BYTES}, "logs": {}}
    manifest["packages"] = {}
    for package in ("certifi", "truststore", "openai", "anthropic", "pywebview"):
        try:
            manifest["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            manifest["packages"][package] = "not available"
    import certifi
    from pathlib import Path
    manifest["tls"] = {"bundled_ca_present": Path(certifi.where()).is_file()}
    files = {}
    for name in ("app.log", "app.prev.log"):
        files["logs/" + name], manifest["logs"][name] = _log_tail(name, redactor)
    files["README.txt"] = (
        "JamesAgent local diagnostic bundle / 本机诊断包\n"
        "Contains version/configuration summaries, recent task statistics and events, and redacted log tails.\n"
        "包含版本/配置摘要、近期任务统计/错误和脱敏日志，可能含查询关键词和网站地址。\n"
        "No database, API keys, account sessions, customer attachments or full customer records are copied.\n"
        "不打包数据库、API Key、账号会话、客户附件或完整客户记录。自动脱敏不能保证所有自由文本完全匿名。\n"
        "Saved locally only; review the files before choosing to share them. No upload is performed.\n"
        "仅保存本机；发送前可查看文件内容。导出操作不会自动上传。\n"
    )
    for name, value in (("manifest.json", manifest), ("config.json", config), ("tasks.json", recent), ("summary.json", summary)):
        files[name] = json.dumps(redactor.data(value), ensure_ascii=False, indent=2)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    filename = f"JamesAgent-diagnostics-{VERSION}-{generated.strftime('%Y%m%d-%H%M%S')}.zip"
    return filename, buffer.getvalue()

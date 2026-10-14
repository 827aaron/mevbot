"""Local, opt-in daily source discovery/reading, independent of lead quotas."""
import copy
import hashlib
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit, urljoin

from . import background, channels, db, edition, llm, settings, tasks
from .errors import UserError

DEFAULT = {"enabled": False, "icp_id": None, "time_of_day": "09:00", "keywords": [], "discovered_keywords": []}
_lock = threading.RLock()
KINDS = {"forum", "directory", "news_source", "fb_group", "instagram_account", "youtube_channel", "x_account", "reddit_community"}


def config():
    return {**copy.deepcopy(DEFAULT), **(settings.get("source_pool_settings", {}) or {})}


def keywords_for(icp_id):
    cfg = config()
    if cfg["icp_id"] != icp_id:
        return []
    return list(dict.fromkeys(cfg["keywords"] + [v["word"] for v in cfg["discovered_keywords"]]))[:100]


@background.tracked("source_pool:settings")
def set_settings(data):
    from .agents import icp, manager
    if not isinstance(data, dict) or set(data) - {"enabled", "icp_id", "time_of_day", "keywords"}:
        raise UserError("invalid_value", field="source_pool")
    with _lock:
        cfg = {**config(), **data}
        manager.boolean(cfg["enabled"], "enabled")
        manager.parse_time(cfg["time_of_day"])
        if cfg["icp_id"] is not None:
            manager.integer(cfg["icp_id"], "icp_id", 1, 2**31 - 1)
            icp.get(cfg["icp_id"])
        elif cfg["enabled"]:
            raise UserError("task_icp_missing")
        words = cfg["keywords"]
        if not isinstance(words, list) or len(words) > 100 or any(
                not isinstance(w, str) or not w.strip() or len(w) > 120 for w in words):
            raise UserError("invalid_value", field="keywords")
        cfg["keywords"] = list(dict.fromkeys(w.strip() for w in words))
        previous = config()
        if cfg["icp_id"] != previous["icp_id"]:
            cfg["discovered_keywords"] = []
        settings.put("source_pool_settings", cfg)
        if not cfg["enabled"] or cfg["icp_id"] != previous["icp_id"]:
            with tasks.registry_lock():
                for row in db.query("SELECT id FROM tasks WHERE created_by='source_pool' AND status='running'"):
                    run = tasks.get_run(row["id"])
                    if run and not run.done:
                        tasks.cancel(run.id)
        return cfg


def normalize_url(value):
    try:
        p = urlsplit(str(value or "").strip())
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise ValueError()
        if p.port not in (None, 443):
            raise ValueError()
        host = p.hostname.lower()
        # Local service/credentials are never a crawler seed.
        import ipaddress
        if host == "localhost" or host.endswith((".local", ".localhost")):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError()
        from . import social
        return social.canonical_url(urlunsplit(("https", host, p.path or "/", p.query, "")))
    except (ValueError, TypeError):
        raise UserError("relay_bad_url")


def _metadata():
    return settings.get("source_pool_entries", {}) or {}


@background.tracked("source_pool:add")
def add(data):
    from . import social
    url = normalize_url(data.get("url"))
    platform = social.platform_for(url)
    kind = {"facebook": "fb_group", "instagram": "instagram_account", "youtube": "youtube_channel", "x": "x_account", "reddit": "reddit_community"}.get(
        platform, data.get("kind") or "forum")
    if kind not in KINDS:
        raise UserError("invalid_value", field="kind")
    access = data.get("access") or "public"
    if access not in ("public", "login") or (access == "login" and not platform):
        raise UserError("invalid_value", field="access")
    name = str(data.get("name") or url)[:200]
    with _lock, db.transaction():
        cid = channels.register(kind, name, url, source="manual", status="monitored")
        entries = _metadata()
        entries[str(cid)] = {**entries.get(str(cid), {}), "access": access, "platform": platform or ""}
        settings.put("source_pool_entries", entries)
    return next(c for c in items() if c["id"] == cid)


@background.tracked("source_pool:update")
def update(cid, data):
    with _lock, db.transaction():
        entries = _metadata()
        row = db.query_one("SELECT * FROM channels WHERE id=?", (cid,))
        if not row or str(cid) not in entries:
            raise UserError("channel_not_found", 404)
        if set(data) - {"status", "access"}:
            raise UserError("invalid_value", field="source_pool")
        if "access" in data:
            access = data["access"]
            if access not in ("public", "login") or (access == "login" and not entries[str(cid)].get("platform")):
                raise UserError("invalid_value", field="access")
            entries[str(cid)]["access"] = access
            settings.put("source_pool_entries", entries)
        if "status" in data:
            channels.set_status(cid, data["status"])
    return next(c for c in items() if c["id"] == cid)


def items():
    entries = _metadata()
    return [{**c, **entries[str(c["id"])]} for c in db.query("SELECT * FROM channels ORDER BY id DESC")
            if str(c["id"]) in entries]


def urls(platform=None):
    # Oldest/unread sources get the next bounded round, so a growing pool
    # cannot permanently starve sources outside the first page budget.
    rows = sorted(items(), key=lambda c: (c.get("last_read_at", 0), c["id"]))
    return [c["url"] for c in rows if c["status"] == "monitored"
            and (platform is None or c.get("platform") == platform)]


def entry_for(url):
    try:
        url = normalize_url(url)
    except UserError:
        return None
    for c in items():
        try:
            if normalize_url(c["url"]).rstrip("/") == url.rstrip("/"):
                return c
        except UserError:
            continue
    return None


def _retired_url(url):
    if (settings.get("channel_overrides", {}) or {}).get(url.lower()) == "retired":
        return True
    for row in db.query("SELECT url FROM channels WHERE status='retired' AND url<>''"):
        try:
            if normalize_url(row["url"]).rstrip("/") == url.rstrip("/"):
                return True
        except UserError:
            continue
    return False


def _supported_link(url, label):
    from . import social
    if social.platform_for(url):
        return True
    host = urlsplit(url).hostname or ""
    unsupported = ("linkedin.com", "tiktok.com", "discord.com", "discord.gg", "pinterest.com", "snapchat.com", "whatsapp.com")
    if any(host == h or host.endswith("." + h) for h in unsupported):
        return False
    return bool(re.search(r"forum|thread|communit(?:y|ies)|discussion|message.?board|论坛|討論|讨论|社群", url + " " + label, re.I))


def _industry_evidence(url, text):
    industry = re.compile(r"\bigaming\b|\bcasinos?\b|\bsportsbook\b|\bgambling\b|\bbetting\b|博彩|赌博|\bkasino\b|\bpertaruhan\b", re.I)
    return [{"url": url, "quote": line[:1200]} for line in edition._sentences(text) if industry.search(line)][:5]


def _registration_link(url, label=""):
    path = urlsplit(url).path.lower()
    query = urlsplit(url).query.lower()
    from . import social
    platform = social.platform_for(url)
    return bool(re.search(r"(?:^|/)(?:register|registration|signup|sign-up|create-account|join|login|signin)(?:[./]|$)", path)
                or (not platform and re.search(r"(?:^|&)(?:ref|referral|affiliate|aff_id|agent|invite|promo|code)=", query))
                or re.search(r"注册|报名|邀请码|推广码|\b(?:sign.?up|registration|referral code)\b", label, re.I))


@background.tracked("source_pool:discover")
def discover_page(icp_id, page, context=None):
    """Read evidence may discover bounded next-round sources; never transfers auth."""
    from .agents import icp
    from . import social
    profile = icp.get(icp_id)["data"]
    if not edition.signal_mode(profile) or config()["icp_id"] != icp_id:
        return None
    url = str(page.get("source_pool_parent") or page.get("url") or "")
    text = str(page.get("markdown") or "")
    if _retired_url(url):
        return None
    result = edition.validate_b2b(profile, [{"url": url, "text": text}])
    evidence = result.get("evidence") or _industry_evidence(url, text)
    if not evidence:
        return None
    learn_keywords(icp_id, [{"url": url, "text": text}])
    parent = entry_for(url)
    added = None
    if not parent and not _registration_link(url):
        added = add({"url": url, "name": page.get("title") or url, "kind": "news_source",
                     "access": page.get("access", "public")})
        parent = added
        with _lock:
            entries = _metadata()
            entries[str(added["id"])].update(origin="discovery", evidence=evidence[:3], depth=0)
            settings.put("source_pool_entries", entries)
    ctx = context if context is not None else {}
    historical_depth = max(0, int((parent or {}).get("depth", 0)))
    lineage_depth = max(0, int((parent or {}).get("lineage_depth", historical_depth)))
    # Every previously discovered monitored source may be a fresh round root.
    # Historical provenance must not permanently stop the next day's growth.
    depth = max(0, int(ctx.get("depth", 0))) if ctx.get("round_roots") is True else max(0, int(ctx.get("depth", 0)), historical_depth)
    cap = min(10, max(0, int(ctx.get("max_expanded", 10))))
    max_depth = min(2, max(0, int(ctx.get("max_depth", 2))))
    ctx.setdefault("expanded", 0)
    children = []
    if depth < max_depth:
        for link in page.get("links") or []:
            href, label = (link.get("url"), link.get("text", "")) if isinstance(link, dict) else (link[0], link[1] if len(link) > 1 else "")
            if ctx["expanded"] >= cap:
                break
            try:
                target = normalize_url(urljoin(url, str(href or "")))
            except UserError:
                continue
            host = urlsplit(target).hostname or ""
            # Public TG invite/group links are recommendations only. No join or
            # member crawler is called, even when the referring page was private.
            if host in ("t.me", "telegram.me"):
                if not re.fullmatch(r"/(?:[A-Za-z0-9_]{4,}|\+[A-Za-z0-9_-]{4,}|joinchat/[A-Za-z0-9_-]{4,})/?", urlsplit(target).path):
                    continue
                username = urlsplit(target).path.strip("/").lstrip("+").lower()
                if (settings.get("channel_overrides", {}) or {}).get("tg:@" + username) == "retired":
                    continue
                if (db.query_one("SELECT id FROM tg_discoveries WHERE link=?", (target,))
                        or db.query_one("SELECT id FROM tg_join_queue WHERE link=? AND status IN ('pending','joined','needs_approval')", (target,))
                        or db.query_one("SELECT id FROM tg_chats WHERE LOWER(username)=? AND left_at IS NULL", (username,))):
                    continue
                reason = {"parent_url": url, "link_text": str(label)[:300], "evidence": evidence[:3], "depth": depth + 1, "lineage_depth": lineage_depth + 1,
                          "source_signal": {"status": "unverified", "kind": "linked_source", "evidence": []}}
                did = db.execute("INSERT INTO tg_discoveries(icp_id,link,title,about,reason,source,created_at) VALUES(?,?,?,?,?,?,?)",
                                 (icp_id, target, str(label or target)[:200], "", db.dumps(reason), "linked_source", db.now()))
                children.append({"discovery_id": did, "url": target, "kind": "tg_group", "status": "candidate"})
                ctx["expanded"] += 1
                continue
            if (_registration_link(target, str(label)) or target.rstrip("/") == url.rstrip("/") or entry_for(target)
                    or _retired_url(target) or not _supported_link(target, str(label))):
                continue
            child = add({"url": target, "name": str(label or target)[:200], "kind": "forum", "access": "public"})
            if child["status"] == "retired":
                continue
            with _lock:
                entries = _metadata()
                entries[str(child["id"])].update(origin="discovery", parent_url=url, link_text=str(label)[:300],
                                                 evidence=evidence[:3], depth=depth + 1, lineage_depth=lineage_depth + 1, access="public")
                settings.put("source_pool_entries", entries)
            children.append(child)
            ctx["expanded"] += 1
    if added:
        return {**added, "discovered_sources": children}
    return {"parent_url": url, "discovered_sources": children} if children else None


@background.tracked("source_pool:read_status")
def record_read(url, status, error=""):
    with _lock:
        row = entry_for(url)
        if row:
            entries = _metadata()
            entries[str(row["id"])].update(last_status=status, last_error=error, last_read_at=db.now())
            settings.put("source_pool_entries", entries)


@background.tracked("source_pool:learn")
def learn_keywords(icp_id, documents):
    """Learn business-agent expressions only from real read evidence, never the LLM."""
    from .agents import icp
    profile = icp.get(icp_id)["data"]
    if not edition.signal_mode(profile):
        return []
    evidence = edition.validate_b2b(profile, documents).get("evidence") or []
    words = re.compile(r"\b(?:iGaming|casino|betting|sportsbook)\s+(?:affiliate(?:\s+agency)?|agents?)\b|"
                       r"\bmaster agent(?:\s+network)?\b|\baffiliate (?:agency|network|team)\b|"
                       r"博彩代理|代理团队|总代理|ejen iGaming|agen kasino", re.I)
    with _lock:
        cfg = config()
        if cfg["icp_id"] != icp_id:
            return []
        known = {w.lower() for w in keywords_for(icp_id)}
        added = []
        for e in evidence:
            for match in words.finditer(e["quote"]):
                word = match.group(0)
                if word.lower() not in known and len(cfg["keywords"]) + len(cfg["discovered_keywords"]) < 100:
                    item = {"word": word, "url": e["url"], "quote": e["quote"][:600], "at": db.now()}
                    cfg["discovered_keywords"].append(item)
                    known.add(word.lower())
                    added.append(item)
        if added:
            settings.put("source_pool_settings", cfg)
        return added


def report():
    value = settings.get("source_pool_report", {}) or {}
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (value.get("task_id", ""),))
    if row:
        value = {**value, "status": row["status"], "error": row["error"], "stats": db.loads(row["stats"], {})}
    day = value.get("day")
    if day:
        try:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
            end = time.mktime(time.localtime(start + 36 * 3600)[:3] + (0, 0, 0, 0, 0, -1))
            value["added_sources"] = sum(c.get("origin") == "discovery" and start <= c["created_at"] < end for c in items())
        except ValueError:
            pass
    return value


def summary():
    rows = items()
    return {"total": len(rows), "monitored": sum(c["status"] == "monitored" for c in rows),
            "login_required": sum(c.get("last_error") == "social_login_required" for c in rows),
            "report": report()}


def state():
    from . import social
    return {"settings": config(), "items": items(), "report": report(), "platforms": social.status_all()}


@background.tracked("source_pool:daily")
def tick(ts=None):
    """One durable collection task per local day; no dependency on today's lead goal."""
    from .agents import manager
    ts = time.time() if ts is None else ts
    cfg = config()
    if not cfg["enabled"] or settings.ai_paused():
        return None
    today = manager.day(ts)
    if time.strftime("%H:%M", time.localtime(ts)) < cfg["time_of_day"]:
        return None
    with _lock, tasks.registry_lock():
        parent = "source_pool:" + today
        row = db.query_one("SELECT id FROM tasks WHERE parent_id=? AND created_by='source_pool'", (parent,))
        if row:
            return None
        seeds = urls()
        if not seeds and edition.current()["id"] != "my_igaming":
            return None
        # With no customer seeds the industry edition still attempts web
        # discovery. Failures stay failures, never a successful zero result.
        params = {"icp_id": cfg["icp_id"], "target": 500, "sources": ["google", "web"], "urls": seeds[:50],
                  "follow_links": True}
        try:
            for agent in ("researcher", "analyst"):
                if settings.agent_enabled(agent):
                    llm.check_budget(agent, llm.get_config(agent))
            task = tasks.create_task(params, created_by="source_pool", parent_id=parent, goal_date=today)
        except UserError as exc:
            settings.put("source_pool_report", {"day": today, "status": "blocked", "error": exc.key})
            return None
        settings.put("source_pool_report", {"day": today, "status": task["status"], "task_id": task["id"],
                                            "sources": len(seeds), "error": ""})
        return task["id"]


def _unit_token(icp_id, unit):
    from . import social
    identity = str(unit.get("id") or social.canonical_url(str(unit.get("url") or "")))
    location = social.canonical_url(str(unit.get("url") or ""))
    platform = str(unit.get("platform") or social.platform_for(location) or "page")
    from .agents import icp
    data = icp.normalize(icp.get(icp_id)["data"])
    manual = config()["keywords"] if config()["icp_id"] == icp_id else []
    # Store hashes only; same-id profile/manual-feature changes re-evaluate.
    namespace = hashlib.sha256(db.dumps({"profile": data, "keywords": manual}).encode()).hexdigest()
    key = hashlib.sha256(db.dumps([icp_id, namespace, platform, location, identity]).encode()).hexdigest()
    content = hashlib.sha256(db.dumps([str(unit.get("author") or ""), str(unit.get("text") or ""), unit.get("links") or []]).encode()).hexdigest()
    return {"key": key, "content_hash": content, "icp_id": icp_id}


def pending_units(icp_id, page):
    if page.get("error") or page.get("status") in ("failed", "error"):
        return []
    units = page.get("units")
    if units is None:
        units = [{"id": "page", "url": page.get("url", ""), "text": page.get("markdown", ""),
                  "links": page.get("links") or [], "platform": page.get("platform") or "page"}]
    checkpoints = settings.get("source_pool_checkpoints", {}) or {}
    pending, seen = [], set()
    for original in units:
        if not str(original.get("text") or "").strip() or not original.get("url"):
            continue
        unit = dict(original)
        token = _unit_token(icp_id, unit)
        fingerprint = (token["key"], token["content_hash"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        previous = checkpoints.get(token["key"])
        if previous and previous.get("content_hash") == token["content_hash"]:
            continue
        unit.update(checkpoint_token=token, checkpoint_changed=bool(previous))
        pending.append(unit)
    return pending


@background.tracked("source_pool:ack_unit")
def ack_unit(icp_id, unit):
    token = _unit_token(icp_id, unit)
    if unit.get("checkpoint_token") != token:
        # The caller may have evaluated an older profile while the customer
        # edited a feature. Leave this unit pending for a fresh next-round read.
        return False
    with _lock:
        checkpoints = settings.get("source_pool_checkpoints", {}) or {}
        checkpoints[token["key"]] = {"content_hash": token["content_hash"], "at": db.now()}
        settings.put("source_pool_checkpoints", checkpoints)
    return True


@background.tracked("source_pool:scan")
def record_scan(url, new, unchanged, changed):
    with _lock:
        row = entry_for(url)
        if not row:
            return
        entries = _metadata()
        entries[str(row["id"])].update(new_items=int(new), unchanged_items=int(unchanged), changed_items=int(changed),
                                      last_new_items=int(new), last_duplicate_items=int(unchanged), last_changed_items=int(changed))
        settings.put("source_pool_entries", entries)

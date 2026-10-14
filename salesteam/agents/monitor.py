"""👁️ 监控员（PRD 6.4）：读取 TG 群/频道（含讨论区）的新消息，识别购买信号，写入线索池交给数据师。

- 只在有任务勾选了「TG 群」时工作（PRD 6.4「所有渠道规矩一样：开始任务才工作」）：每一轮把已开启监控的群读一遍，
  每条消息只读一次，再分别按每个这样的任务的模板（线索词）判断；命中的线索归那个任务（进它的线索队列，由它的数据师处理）

- 每个群记录读取位置（cursor）：只读新消息，断点补读，不重不漏；新加入监控的群第一次往回读少量历史
- 过滤漏斗（PRD 2.4 / 5.2.1）：
  ① 线索词初筛（免费，PRD §3）：命中任一求购词或产品词、且没有命中排除词（模板的 + 学到的）
     + 本地语义相似（与客户教过的好例子意思相近也算命中；不花大模型费用；命中排除词的不算）
  ② 判断模型（可选，极便宜）：求购 / 广告 / 闲聊？意向高 / 中 / 低？拿不准的升级
  ③ 大模型（只处理少量，且批量：一次判断多条消息）
- 记录：发言人、公开资料（用户名、简介）、原文、群名、时间、消息链接
- 每轮小结；高意向弹桌面通知 + 提醒中心
- 只读，不发任何消息
"""
import re
import time

from .. import alerts, countries, db, langdetect, lease, leads, llm, prompts, settings
from .. import contacts as contacts_mod
from ..llm import decision
from ..llm.mock import mock_kind as _mock_kind     # 只给测试模式（mock 判断模型）提示答案
from ..tg import base, pool
from . import learning, memory

AGENT = "monitor"
BATCH = 12
DEFAULT_BACKLOG_DAYS = 7     # 第一次往回读 7 天（PRD 6.4，固定起点；由销售主管按群调整）
MAX_BACKLOG_DAYS = 30        # 程序硬上限：最多往回 30 天
MAX_BACKFILL_MSGS = 3000     # 程序硬上限：每个群每轮最多补读 3000 条

# 初筛关键词全部来自客户的模板（关键词 + 多语言关键词 + AI 根据客户资料生成的“求购说法”），程序里不写死任何
# 业务词（PRD 2.4.2）。分类提示词在提示词库 monitor.classify。
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["idx", "message_type", "intent", "need", "product", "quantity",
                                            "country", "company", "reason"],
                               "properties": {"idx": {"type": "integer"},
                                              "message_type": {"type": "string",
                                                               "enum": ["buy_request", "advertisement",
                                                                        "chit_chat", "other"]},
                                              "intent": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                                              "need": {"type": "string"}, "product": {"type": "string"},
                                              "quantity": {"type": "string"}, "country": {"type": "string"},
                                              "company": {"type": "string"}, "reason": {"type": "string"}}}}}}


def default_icp_id():
    v = settings.get("monitor").get("icp_id")
    if v and db.query_one("SELECT id FROM icp_templates WHERE id=?", (v,)):
        return int(v)
    r = db.query_one("SELECT id FROM icp_templates ORDER BY updated_at DESC LIMIT 1")
    return r["id"] if r else None


class Monitor:
    def __init__(self, run):
        from . import icp as icp_mod
        self.run = run
        self.cfg = settings.get("monitor")
        self.lang = settings.get("ui_lang")
        self.icp_mod = icp_mod
        self._icp_cache: dict = {}
        self.tasks: list = []          # 这一轮服务的任务（勾选了 TG 群、正在运行）
        self.counts: dict = {}
        self.high: list = []           # 这一轮发现的高意向买家（结束时合并成一条通知）

    def emit(self, _key, level="info", **p):
        self.run.emit(AGENT, _key, level=level, state="working", **p)

    def stop(self):
        return self.run.cancel_flag.is_set() or not self.live_tasks()

    def live_tasks(self):
        """这一轮服务的任务里还在运行的（达标 / 被停止的去掉）。"""
        self.tasks = [t for t in self.tasks if not t.done and not t.should_stop()]
        return self.tasks

    def tasks_for(self, chat):
        """一个任务 = 一个模板 + 勾的渠道（PRD §3）：每个群都按每个运行中的 TG 任务自己的模板判断（不再按群选模板）。"""
        return self.live_tasks()

    def emit_tasks(self, tasks, _key, level="info", state="working", **p):
        for t in tasks:
            t.emit(AGENT, _key, level=level, state=state, **p)

    def pause(self, rng):
        import random
        end = time.time() + random.uniform(float(rng[0]), float(rng[1]))
        while time.time() < end and not self.stop():
            time.sleep(0.2)

    # ---------------------------------------------------- 模板上下文（每个群可以用不同模板）
    def ctx(self, icp_id):
        if icp_id in self._icp_cache:
            return self._icp_cache[icp_id]
        if icp_id and self.icp_mod.needs_rules(self.icp_mod.get(icp_id)["data"]):
            self.icp_mod.ensure_rules(icp_id, task_id=self.run.id)   # 旧模板：先让主管生成“求购说法”
        data = self.icp_mod.effective_data(icp_id) if icp_id else {}
        words = self.icp_mod.lead_words(data)
        types = [k["content"] for k in learning.active_knowledge(icp_id) if k["kind"] == "message_type"] if icp_id else []
        c = {"icp_id": icp_id, "icp": data, "words": words, "types": types}
        self._icp_cache[icp_id] = c
        return c

    # ---------------------------------------------------- 主流程
    def execute(self):
        from .. import tasks as tasks_mod
        self.tasks = tasks_mod.tg_tasks()
        if not self.tasks:
            self.emit("ev.monitor_no_task", level="warn")
            return
        self.run.bump(tasks=len(self.tasks))
        self.counts = {t.id: {"chats": 0, "messages": 0, "hits": 0} for t in self.tasks}
        self.emit_tasks(self.tasks, "ev.monitor_round_start")
        accs = pool.monitor_accounts()
        if self.run.params.get("account_ids"):
            accs = [a for a in accs if a["id"] in self.run.params["account_ids"]]
        if not accs:
            self.emit("ev.monitor_no_accounts", level="warn")
            self.emit_tasks(self.tasks, "ev.monitor_no_accounts", level="warn")
            return
        if not settings.agent_enabled(AGENT):
            self.emit("ev.agent_off", level="warn", agent="@agent.monitor")
            return
        be = pool.backend()
        for acc_row in accs:
            if self.stop():
                break
            chats = db.query("SELECT * FROM tg_chats WHERE account_id=? AND monitored=1 ORDER BY id", (acc_row["id"],))
            if not chats:
                continue
            acc = pool._acc(acc_row)
            label = acc_row["label"] or acc_row["phone"]
            self.emit("ev.monitor_account", account=label, n=len(chats))
            try:
                for n, chat in enumerate(chats):
                    if self.stop():
                        break
                    if n and not be.is_mock:
                        self.pause(self.cfg.get("chat_gap_s") or [2, 8])     # 群与群之间随机停顿，像真人
                    # 账号调度（PRD 6.2.1）：只在调用 TG（读这个群）时借账号，AI 判断消息时已经还回去
                    self._ls = lease.acquire("tg", "monitor", purpose="monitor", agent=AGENT, account_id=acc_row["id"])
                    if not self._ls:
                        self.emit("ev.monitor_account_unavailable", level="warn", account=label)
                        break
                    try:
                        msgs = self.read_chat(be, acc, acc_row, chat)
                    except base.NotMember as e:
                        # 账号已经不在这个群（手机上退了 / 被移出 / 私有群没权限）→「已退出」，不再监控，接着读下一个群
                        pool.mark_left(chat["id"], e.detail)
                        self.emit("ev.monitor_chat_left", level="warn", chat=chat["title"], account=label)
                        continue
                    finally:
                        self._ls.release()
                    for t in self.tasks_for(chat):
                        self.counts[t.id]["chats"] += 1
                        self.counts[t.id]["messages"] += len(msgs)
                        t.bump(chats=1, messages=len(msgs))
                    if msgs:
                        self.process_msgs(be, acc, acc_row, chat, msgs)
                pool.mark_ok(acc_row["id"])
                if not self.stop():          # 读到一半任务停了：不算读过这一轮，下个任务开始时马上读
                    db.execute("UPDATE tg_accounts SET last_monitor_at=? WHERE id=?", (db.now(), acc_row["id"]))
            except lease.CapReached:
                self.emit("ev.monitor_read_cap", level="warn", account=label,
                          cap=lease.cap("tg", acc_row["id"], "read"))
            except base.TGError as e:
                key = pool.handle_error(acc_row["id"], e)
                self.emit(key, level="error", account=label)
                continue
        self.run.agent_state.setdefault(AGENT, {})["state"] = "done"
        if self.high:
            h = self.high[0]
            alerts.raise_alert(f"high_intent_round:{self.run.id}", "info", "high_intent_round", link="#/leads",
                               desktop=True, notice=True, n=len(self.high), name=h["name"], chat=h["chat"], need=h["need"])
        s = self.run.stats
        self.run.emit(AGENT, "ev.monitor_round_summary", level="success", state="done", chats=s["chats"],
                      messages=s["messages"], kw=s["keyword_hits"], hits=s["pool"], skipped=s.get("lang_skipped", 0))
        for t in self.tasks:
            c = self.counts[t.id]
            t.emit(AGENT, "ev.monitor_task_round", level="success", state="waiting", chats=c["chats"],
                   messages=c["messages"], hits=c["hits"])

    def read_chat(self, be, acc, acc_row, chat):
        title = chat["title"]
        cursor = int(chat["cursor"] or 0)
        backfill = bool(chat["backfill"])
        if cursor == 0:
            # 第一次监控：按时间往回读（默认 7 天，由销售主管按群调整；程序上限 30 天 / 3000 条）
            days = min(float(chat["backlog_days"] or DEFAULT_BACKLOG_DAYS), MAX_BACKLOG_DAYS)
            latest = be.latest_id(acc, chat)
            since_id = be.id_before(acc, chat, time.time() - days * 86400)
            cursor = max(since_id, latest - MAX_BACKFILL_MSGS, 0)
            backfill = True
            db.execute("UPDATE tg_chats SET cursor=?, min_read_id=?, backfill=1 WHERE id=?",
                       (cursor, cursor + 1, chat["id"]))
            self.emit("ev.monitor_backfill", chat=title, days=int(days), n=max(0, latest - cursor))
        max_n = int(self.cfg.get("backfill_round_cap", 1000) if backfill else self.cfg.get("max_msgs_per_chat", 300))
        msgs = []
        while len(msgs) < max_n and not self.stop():
            self._ls.use("read")                    # 这个账号今天读了多少页（所有 AI 共用一个计数）
            batch = be.read_new(acc, chat, cursor, min(100, max_n - len(msgs)))
            if not batch:
                break
            msgs += batch
            cursor = max(cursor, max(m["id"] for m in batch))
            if len(batch) < min(100, max_n):
                break
            if not be.is_mock:
                self.pause(self.cfg.get("page_gap_s") or [1, 3])          # 同一个群翻页之间也停一下
        caught_up = len(msgs) < max_n
        db.execute("UPDATE tg_chats SET cursor=?, last_read_at=?, msgs_read=msgs_read+?, backfill=? WHERE id=?",
                   (cursor, db.now(), len(msgs), 0 if caught_up else int(backfill), chat["id"]))
        self.run.bump(chats=1, messages=len(msgs))
        self.emit("ev.monitor_read", chat=title, n=len(msgs))
        if not msgs:
            return []
        return msgs + self.read_older(be, acc, chat)

    def read_older(self, be, acc, chat) -> list:
        """往更早补读（销售主管发起，例如线索不够时把高质量群补到 14 / 30 天）。与向前的游标分开。"""
        target = int(chat["older_target_id"] or 0)
        low = int(chat["min_read_id"] or 0)
        if not target or not low or target >= low - 1:
            return []
        start = max(target, int(chat["older_cursor"] or 0) or target)
        out = []
        cap = MAX_BACKFILL_MSGS
        while len(out) < cap and not self.stop():
            self._ls.use("read")
            batch = [m for m in be.read_new(acc, chat, start, min(100, cap - len(out))) if m["id"] < low]
            if not batch:
                start = low
                break
            out += batch
            start = max(m["id"] for m in batch)
            if not be.is_mock:
                self.pause(self.cfg.get("page_gap_s") or [1, 3])
        done = start >= low - 1
        db.execute("UPDATE tg_chats SET older_cursor=?, older_target_id=?, min_read_id=? WHERE id=?",
                   (0 if done else start, 0 if done else target, target + 1 if done else low, chat["id"]))
        if out:
            self.emit("ev.monitor_older", chat=chat["title"], n=len(out))
        return out

    def process_msgs(self, be, acc, acc_row, chat, msgs):
        """这个群新读到的消息（只读了一次）→ 分别按每个任务的模板初筛、判断；命中的线索归那个任务。"""
        if not msgs:
            return
        title = chat["title"]
        texts = self.usable_texts(msgs)
        hit_ids: set = set()
        for task in self.tasks_for(chat):
            c = self.ctx(task.icp_id)
            cands = self.prefilter(texts, c)
            self.run.bump(keyword_hits=len(cands))
            task.bump(keyword_hits=len(cands))
            if cands:
                self.emit("ev.monitor_prefilter", chat=title, n=len(cands), total=len(msgs))
            for i in range(0, len(cands), BATCH):
                if self.stop() or task.should_stop():
                    break
                hit_ids |= self.classify(be, acc, acc_row, chat, [dict(m) for m in cands[i:i + BATCH]], c, task)
        if hit_ids:
            db.execute("UPDATE tg_chats SET hits=hits+? WHERE id=?", (len(hit_ids), chat["id"]))

    # ---------------------------------------------------- ① 关键词 + 语义
    def usable_texts(self, msgs):
        """够长、语言在范围内的消息（每条消息只算一次，和有几个任务无关）。"""
        min_len = int(self.cfg.get("min_len", 12))
        texts = [(m, (m.get("text") or "").strip()) for m in msgs]
        texts = [(m, t) for m, t in texts if len(t) >= min_len]
        # v1 只做 中文 / 英文 / 马来语（PRD §1）：其他语言的消息在任何模型之前跳过（纯规则识别，不花钱）
        kept = [(m, t) for m, t in texts if langdetect.allowed(t)]
        if len(kept) < len(texts):
            self.run.bump(lang_skipped=len(texts) - len(kept))
        return kept

    def prefilter(self, texts, c):
        """线索词规则（PRD §3）：命中任一求购词或产品词、且没有命中排除词 → 候选；命中排除词的直接不要；
        都没命中的再看本地语义相似（和客户教过的好例子意思相近）。"""
        out, rest = [], []
        for m, t in texts:
            hit = self.icp_mod.match_words(t, c["words"])
            if hit == "hit":
                out.append({**m, "_why": "keyword"})
            elif hit != "excluded":
                rest.append(m)
        if rest and c["icp_id"]:
            try:
                from ..learn import embed
                sims = embed.sample_similarity(c["icp_id"], AGENT, [m["text"] for m in rest])
            except Exception:  # noqa: BLE001  语义模型不可用时只靠关键词
                sims = None
            if sims:
                for m, (pos, neg) in zip(rest, sims):
                    if pos >= embed.MATCH_THRESHOLD and pos > neg:
                        out.append({**m, "_why": "semantic"})
        return out

    # ---------------------------------------------------- ② 判断模型 + ③ 大模型
    def classify(self, be, acc, acc_row, chat, cands, c, task) -> set:
        """一批候选消息按一个任务的模板判断；求购的写成这个任务的线索。返回命中的消息 ID。"""
        resolved, need_llm = [], []
        if decision.enabled():
            for m in cands:
                q1, o1 = prompts.options("decision.message_kind")
                d = decision.decide(m["text"], o1, q1, agent=AGENT, task_id=self.run.id,
                                    context={"mock_hint": _mock_kind(m["text"])})
                if d is None or d.escalate:
                    need_llm.append(m)
                    continue
                if d.choice != "buy_request":
                    self.run.bump(prescreened=1)
                    continue
                q2, o2 = prompts.options("decision.intent")
                d2 = decision.decide(m["text"], o2, q2, agent=AGENT, task_id=self.run.id,
                                     context={"mock_hint": "high" if re.search(r"\d", m["text"]) else "medium"})
                if d2 is None or d2.escalate:
                    need_llm.append(m)
                    continue
                resolved.append((m, {"message_type": "buy_request", "intent": d2.choice, "need": m["text"][:300],
                                     "product": "", "quantity": "", "country": "", "company": "",
                                     "reason": f"decision {d.confidence:.0%}/{d2.confidence:.0%}"}))
        else:
            need_llm = list(cands)
        if need_llm:
            self.emit("ev.monitor_ai_judging", chat=chat["title"], n=len(need_llm))
            items = "\n\n".join(f"[{i}] from {m.get('sender_name') or ''} @{m.get('sender_username') or '-'}:\n"
                                f"{m['text'][:1500]}" for i, m in enumerate(need_llm))
            samples = learning.relevant_samples(c["icp_id"], " ".join(m["text"] for m in need_llm)[:4000],
                                                agent=AGENT)
            types = ("Customer-confirmed message types to recognise: " + "; ".join(
                str(t.get("name") if isinstance(t, dict) else t) for t in c["types"])) if c["types"] else ""
            from .researcher import icp_brief
            system = prompts.render("monitor.classify", role=memory.role(AGENT),
                                    lang="Simplified Chinese" if self.lang == "zh" else "English", types=types,
                                    languages="Messages are in Chinese, English or Malay." if countries.V1_SCOPE else "",
                                    icp=icp_brief(c["icp"]) if c["icp"] else "-", memos=memory.memos_block(AGENT))
            user = (learning.few_shot_block(samples).lstrip() + f"\n\nTelegram group: {chat['title']}\n"
                    f"Group description: {chat['about'][:300]}\n\nMessages:\n\n{items}")
            data, _ = llm.call_json(AGENT, "tg_classify", system, user, SCHEMA, task_id=self.run.id,
                                    context={"messages": need_llm, "lang": self.lang})
            for it in data.get("items") or []:
                i = it.get("idx")
                if isinstance(i, int) and 0 <= i < len(need_llm):
                    resolved.append((need_llm[i], it))
        hits = set()
        for m, it in resolved:
            if it.get("message_type") != "buy_request" or it.get("intent") in ("none", ""):
                continue
            hits.add(m["id"])
            self.add_lead(be, acc, acc_row, chat, m, it, task)
        return hits

    def _channel(self, chat):
        """这个群在渠道池里的 id（统计每个渠道带来的线索）。"""
        try:
            from .. import channels
            return channels.for_tg_chat(chat)
        except Exception:  # noqa: BLE001  统计失败不影响找线索
            return None

    def add_lead(self, be, acc, acc_row, chat, m, it, task):
        username = (m.get("sender_username") or "").lstrip("@")
        bio = ""
        if username or m.get("sender_id"):
            ls = lease.acquire("tg", "monitor", purpose="monitor", agent=AGENT, account_id=acc_row["id"], timeout=60)
            if ls:
                try:
                    info = be.user_info(acc, username or m["sender_id"])
                    bio = info.get("bio", "")
                except base.TGError:
                    pass
                finally:
                    ls.release()
        name = it.get("company") or m.get("sender_name") or (f"@{username}" if username else "Telegram user")
        # 消息年龄：超过 3 天的求购，意向降一级并记录原因（PRD 6.4）
        posted = float(m.get("date") or time.time())
        age_days = (time.time() - posted) / 86400
        intent_note = ""
        if age_days > 3 and it.get("intent") in ("high", "medium"):
            it = {**it, "intent": {"high": "medium", "medium": "low"}[it["intent"]]}
            intent_note = f"older than 3 days ({int(age_days)}d): intent lowered one level"
        country = countries.normalize_country(it.get("country", ""))
        if not country:
            # 群名 / 群简介 / 消息 / 发言人简介里的地名，或电话区号（+60 / +65）推断国家（v1 只要马来西亚）
            ph = contacts_mod.extract(bio + " " + m.get("text", ""))
            country, _ = countries.infer_country(" ".join([chat["title"], chat.get("about") or "", m.get("text", ""), bio]),
                                                 "", ph["whatsapp"] + ph["phones"])
        excerpt = (f"Telegram group: {chat['title']}\nGroup description: {chat['about']}\n"
                   f"Sender: {m.get('sender_name', '')} @{username}\nSender bio: {bio}\n"
                   f"Message ({time.strftime('%Y-%m-%d %H:%M', time.localtime(m.get('date') or time.time()))}):\n"
                   f"{m['text']}")
        bio_c = contacts_mod.extract(bio + " " + m["text"])
        cand = {"company_name": name, "website": "", "country": country, "customer_type": "", "industry": "",
                "emails": [], "telegram": [f"@{username}"] if username else [], "phones": bio_c["phones"],
                "whatsapp": bio_c["whatsapp"],
                "people": [{"name": m.get("sender_name", ""), "title": "", "email": ""}] if m.get("sender_name") else [],
                "description": it.get("need") or m["text"][:300], "buying_signal": it.get("need") or "",
                "evidence_quote": m["text"][:1000], "source_url": m.get("link", ""), "verified_text": True,
                "page_excerpt": excerpt, "message_type": it.get("message_type"), "intent": it.get("intent"),
                "found_by_account": acc_row["id"], "posted_at": posted, "age_days": round(age_days, 1),
                "intent_note": intent_note, "channel_id": self._channel(chat), "tg": {"chat_id": chat["chat_id"], "chat_title": chat["title"],
                                                          "msg_id": m["id"], "sender_id": m.get("sender_id"),
                                                          "username": username, "bio": bio, "why": m.get("_why")}}
        rid = db.execute(
            "INSERT INTO raw_leads(task_id, source, source_url, source_title, company_name, website, country, data, "
            "evidence, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task.id, "tg", m.get("link", ""), chat["title"], name, "", country, db.dumps(cand),
             m["text"][:1000], "new", db.now()))
        task.lead_queue.put(rid)                 # 线索归这个任务：由它的数据师按它的模板处理、算进它的目标
        snap = task.bump(pool=1)
        self.run.bump(pool=1)
        self.counts[task.id]["hits"] += 1
        self.emit("ev.monitor_hit", chat=chat["title"], name=name, intent=f"@intent.{it.get('intent')}",
                  total=self.run.stats["pool"])
        task.emit(AGENT, "ev.monitor_hit", state="working", chat=chat["title"], name=name,
                  intent=f"@intent.{it.get('intent')}", total=snap["pool"])
        if it.get("intent") == "high":
            # U58：高意向不再一条一条“待处理”：这一轮结束时合并成一条通知（默认已读，桌面通知一轮一次）
            self.high.append({"name": name, "chat": chat["title"], "need": (it.get("need") or m["text"])[:120]})


# ------------------------------------------------------------------ 给销售主管（第 3 步）调用的内部接口

def set_chat_backlog(chat_db_id: int, days: float, note: str = "", by: str = "manager") -> dict:
    """调整某个群“往回读”的天数；如果比已读到的更早，就安排往更早补读。程序硬上限 30 天 / 每轮 3000 条。"""
    from ..tg import pool
    days = max(1.0, min(float(days), MAX_BACKLOG_DAYS))
    chat = db.query_one("SELECT * FROM tg_chats WHERE id=?", (chat_db_id,))
    if not chat:
        raise ValueError("chat not found")
    acc_row = db.query_one("SELECT * FROM tg_accounts WHERE id=?", (chat["account_id"],))
    target = 0
    if acc_row and chat["min_read_id"]:
        be = pool.backend()
        target = be.id_before(pool._acc(acc_row), chat, time.time() - days * 86400)
        if target >= chat["min_read_id"] - 1:
            target = 0
    db.execute("UPDATE tg_chats SET backlog_days=?, older_target_id=?, older_cursor=0 WHERE id=?",
               (days, target, chat_db_id))
    db.execute("INSERT INTO agent_actions(ts, agent, action, params, note) VALUES(?,?,?,?,?)",
               (db.now(), by, "chat_backlog", db.dumps({"chat_id": chat_db_id, "title": chat["title"], "days": days,
                                                        "older_target_id": target}), note))
    return {"chat_id": chat_db_id, "days": days, "backfill_scheduled": bool(target)}

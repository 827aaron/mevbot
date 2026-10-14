"""AI 学习的自动部分（PRD 12.2 / 12.3 / 2.3）：

- 关键词自动进化：从客户确认的好例子里提炼新关键词与多语言表达 → 自动生效（可回退）
- 消息类型归纳：如“项目求购”“找替代供应商” → 属于大的规则变化，先给客户看，确认后生效
- 判断模型每周自检：用样本库里客户审核过的真实数据当标准测试题；准确率明显下降 → 提醒客户并自动切换到备用（大模型）
"""
import time

from .. import alerts, db, llm, settings
from ..agents import learning
from ..agents.schemas import obj
from ..errors import UserError

MIN_NEW_POSITIVES = 3
RETEST_EVERY = 7 * 86400
MIN_TEST_SAMPLES = 10

KW_SCHEMA = obj({"keywords": {"type": "array", "items": {"type": "string"}},
                 "by_lang": {"type": "array", "items": obj({"lang": {"type": "string"},
                                                            "keywords": {"type": "array", "items": {"type": "string"}}})},
                 "message_types": {"type": "array", "items": obj({"name": {"type": "string"},
                                                                  "description": {"type": "string"}})}})


def _agent():
    for a in ("analyst", "monitor", "manager"):
        if settings.agent_enabled(a):
            return a
    return None


def evolve(icp_id: int, force=False) -> dict:
    """从好例子提炼关键词（自动生效）和消息类型（待确认）。"""
    st = settings.get("learning")
    last_ids = st.get("evolve_last_sample", {}) or {}
    last = int(last_ids.get(str(icp_id), 0))
    pos = db.query("SELECT id, text FROM icp_samples WHERE icp_id=? AND polarity>0 ORDER BY id DESC LIMIT 40", (icp_id,))
    new = [p for p in pos if p["id"] > last]
    if not force and len(new) < MIN_NEW_POSITIVES:
        return {"skipped": "not_enough_new_samples", "new": len(new)}
    if not pos:
        return {"skipped": "no_samples"}
    agent = _agent()
    if not agent:
        return {"skipped": "agents_off"}
    from ..agents import icp as icp_mod
    data = icp_mod.effective_data(icp_id)
    cur_types = [k["content"] for k in learning.active_knowledge(icp_id) if k["kind"] == "message_type"]
    from .. import prompts
    user = prompts.render("learning.keyword_evolve_task", keywords=", ".join(data.get("product_words") or []),
                          types=", ".join(str(t) for t in cur_types) or "-",
                          examples="\n---\n".join(p["text"][:600] for p in pos[:25]))
    out, _ = llm.call_json(agent, "keyword_evolve", prompts.get("learning.keyword_evolve"), user, KW_SCHEMA, effort="low", context={"samples": [p["text"] for p in pos],
                                                                   "keywords": data.get("product_words") or []})
    have = {k.lower() for k in data.get("product_words") or []}
    learned_prev = learning.effective(icp_id)["keywords"]
    new_kw = [k.strip() for k in out.get("keywords") or [] if k.strip() and k.strip().lower() not in have][:10]
    for item in out.get("by_lang") or []:
        new_kw += [k.strip() for k in item.get("keywords") or [] if k.strip() and k.strip().lower() not in have][:6]
    res = {"keywords_added": 0, "types_proposed": 0}
    merged = list(dict.fromkeys(learned_prev + new_kw))
    if len(merged) > len(learned_prev):
        learning.add_knowledge(icp_id, "keywords", merged, note=f"+{len(merged) - len(learned_prev)}")
        res["keywords_added"] = len(merged) - len(learned_prev)
    types = [{"name": t.get("name", "")[:60], "description": t.get("description", "")[:200]}
             for t in out.get("message_types") or [] if t.get("name")]
    if types and {t["name"].lower() for t in types} != {str(c.get("name", c)).lower() if isinstance(c, dict) else
                                                         str(c).lower() for c in cur_types}:
        pending = db.query_one("SELECT id FROM icp_knowledge WHERE icp_id=? AND kind='message_type' AND "
                               "status='pending_confirmation'", (icp_id,))
        if not pending:
            for t in types:
                learning.add_knowledge(icp_id, "message_type", t, note="proposed")
            res["types_proposed"] = len(types)
            alerts.raise_alert(f"learning_pending:{icp_id}", "info", "learning_pending", link="#/learning",
                               n=len(types))
    last_ids[str(icp_id)] = max(p["id"] for p in pos)
    st = settings.get("learning")
    st["evolve_last_sample"] = last_ids
    st["last_evolve_at"] = time.time()
    settings.put("learning", st)
    return res


def evolve_if_due():
    st = settings.get("learning")
    if time.time() - float(st.get("last_evolve_at") or 0) < 86400:
        return
    for t in db.query("SELECT id FROM icp_templates"):
        try:
            evolve(t["id"])
        except UserError:
            continue
    st = settings.get("learning")
    st["last_evolve_at"] = time.time()
    settings.put("learning", st)


# ------------------------------------------------------------------ 判断模型自检

def test_set(limit=60) -> list[dict]:
    from .. import langdetect, prompts
    rows = db.query("SELECT polarity, text, agent, label FROM icp_samples ORDER BY id DESC LIMIT ?", (limit * 2,))
    out = []
    for r in rows:
        if not langdetect.allowed(r["text"]):      # v1 自检只用 中文 / 英文 / 马来语 的样本（PRD §1）
            continue
        if len(out) >= limit:
            break
        if r["agent"] == "monitor":
            q, opts = prompts.options("decision.selftest_message")
            out.append({"text": r["text"], "options": opts, "question": q,
                        "expected": "buy_request" if r["polarity"] > 0 else "other"})
        else:
            q, opts = prompts.options("decision.selftest_lead")
            out.append({"text": r["text"], "options": opts, "question": q,
                        "expected": "match" if r["polarity"] > 0 else "no_match"})
    return out


def retest(force=False) -> dict:
    from ..llm import decision
    cfg = decision.get_config()
    if not decision.configured(cfg) or cfg["provider"] == "llm":
        return {"skipped": "no_decision_model"}
    items = test_set()
    now = time.time()
    if len(items) < MIN_TEST_SAMPLES:
        db.execute("INSERT INTO decision_tests(ts, provider, model, n, correct, accuracy, action) VALUES(?,?,?,?,?,?,?)",
                   (now, cfg["provider"], cfg["model"], len(items), 0, 0, "insufficient_samples"))
        return {"skipped": "insufficient_samples", "n": len(items)}
    correct = 0
    for it in items:
        d = decision.decide(it["text"], it["options"], it["question"], agent="analyst", force_primary=True,
                            context={"mock_hint": it["expected"]})
        if d and d.choice == it["expected"]:
            correct += 1
    acc = correct / len(items)
    prev = db.query_one("SELECT MAX(accuracy) a FROM decision_tests WHERE provider=? AND model=? AND "
                        "action IN ('ok','baseline')", (cfg["provider"], cfg["model"]))
    baseline = prev["a"] if prev and prev["a"] else None
    action = "baseline" if baseline is None else "ok"
    if acc < 0.6 or (baseline is not None and acc < baseline - 0.10):
        action = "switched_fallback"
        decision.set_fallback(True, reason=f"{acc:.0%}")
        alerts.raise_alert("decision_drift", "general", "decision_drift", link="#/settings/models",
                           acc=f"{acc:.0%}", base=f"{(baseline or 0):.0%}")
    elif cfg.get("fallback_active"):
        decision.set_fallback(False)
        alerts.resolve("decision_drift")
    db.execute("INSERT INTO decision_tests(ts, provider, model, n, correct, accuracy, baseline, action) "
               "VALUES(?,?,?,?,?,?,?,?)", (now, cfg["provider"], cfg["model"], len(items), correct, acc, baseline,
                                           action))
    st = settings.get("learning")
    st["last_retest_at"] = now
    settings.put("learning", st)
    return {"n": len(items), "correct": correct, "accuracy": acc, "baseline": baseline, "action": action}


def retest_if_due():
    st = settings.get("learning")
    if time.time() - float(st.get("last_retest_at") or 0) >= RETEST_EVERY:
        retest()
        st = settings.get("learning")
        st["last_retest_at"] = time.time()
        settings.put("learning", st)


def tests_history(limit=30):
    return db.query("SELECT * FROM decision_tests ORDER BY id DESC LIMIT ?", (limit,))


def approval_curve(days=56, bucket="week") -> list[dict]:
    """审核通过率 = 通过 ÷（通过 + 拒绝），不计“稍后”（PRD 14）。"""
    since = time.time() - days * 86400
    rows = db.query("SELECT reviewed_at, status FROM leads WHERE reviewed_at>=? AND status IN ('approved','rejected',"
                    "'contacted','replied')", (since,))
    buckets: dict[str, list[int]] = {}
    for r in rows:
        t = time.localtime(r["reviewed_at"])
        key = time.strftime("%G-W%V", t) if bucket == "week" else time.strftime("%Y-%m-%d", t)
        b = buckets.setdefault(key, [0, 0])
        if r["status"] == "rejected":
            b[1] += 1
        else:
            b[0] += 1
    return [{"period": k, "approved": v[0], "rejected": v[1], "rate": v[0] / max(1, v[0] + v[1])}
            for k, v in sorted(buckets.items())]

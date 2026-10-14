"""全局设置（界面语言、搜索、爬虫、YouTube）+ 加密的第三方 Key + 高风险开关记录。"""
import copy

from . import db, vault

DEFAULTS = {
    "ui_lang": "zh",
    # AI 总预算（美元，全部智能体合计）。0 = 不限制。到达上限 → 所有智能体暂停，次日/次月自动恢复或提高上限
    "budget": {"daily": 10.0, "monthly": 150.0},
    # 搜索：auto = 有 Key 的优先（Serper > Brave），都没有用免费兜底；Claude 调研员优先用 Claude 自带联网搜索
    "search": {
        "provider": "auto",          # auto / claude / serper / brave / free
        "results_per_query": 8,
        "queries_per_source": 6,
    },
    "crawler": {
        "delay_seconds": 3.0,        # 同一网站两次请求最小间隔（慢速，模拟真人）
        "max_pages_per_task": 60,    # 每个任务最多抓取页数（保守值）
        "max_pages_per_site": 5,
        "daily_page_limit": 500,
        "respect_robots": True,      # 默认遵守 robots.txt
        "stealth_mode": True,        # 隐身模式：测试阶段默认开启、按需使用（PRD 4.1）——遇到 Cloudflare 类检查才用真实浏览器重试一次；正式版默认值测试后再定
        "render_js": False,          # 加载后才显示内容的网站：普通读取没文字时用真实浏览器读一次（0.2.2 先关闭，下个版本测试后再开）
        "timeout": 20,
    },
    "youtube": {
        "max_videos_per_query": 5,
        "max_comments_per_video": 30,
    },
    "analyst": {
        "enrich_website": True,      # 无联系方式时读取官网的联系/关于页
        "concurrency": 2,
    },
    # ---------------- 第 2 步 ----------------
    # AI 启用开关（PRD 2.1.2）：关闭的 AI 不消耗 token。销售员、跟进员第 3 步上线
    "agents_enabled": {"manager": True, "scout": True, "researcher": True, "monitor": True, "analyst": True,
                       "sales": False, "followup": False},
    "tg": {"backend": "telethon"},      # telethon（真实）/ mock（测试用假 TG，不联网）
    "monitor": {
        "interval": "1h",            # 30m / 1h / 6h / daily
        "daily_time": "09:00",
        "icp_id": None,              # 监控判断用的默认模板
        "max_msgs_per_chat": 300,    # 每轮每个群最多读多少条（剩下的下一轮补读）
        "backfill_round_cap": 1000,  # 补读期间每轮每群最多读多少条（分几轮读完，游标保证不重不漏）
        "min_len": 12,
        "chat_gap_s": [2, 8],        # 群与群之间的随机间隔（秒）
        "page_gap_s": [1, 3],        # 同一个群翻页之间的随机间隔（秒）
    },
    "joining": {
        "assisted_join": False,      # B. 辅助进群（高风险，默认关闭）
        "min_gap_min": 4, "max_gap_min": 12,   # 两次进群之间的随机间隔（分钟）
    },
    "notify": {
        "lead_recipients": [],       # 线索接收人（TG 用户名 / 群链接）
        "alert_recipients": [],      # 异常提醒接收人（可与线索接收人不同）
        "push_enabled": True,        # 配好通知号和接收人后默认开启
        "push_on_task_done": False,  # v1：只在客户设定的推送时间推送，每天一次（PRD 7.1）
        "cutoff": "20:00",           # 每天推送时间（客户在 设置 → 基本 里设定，本机时间）
        "last_push_day": "",
        "general_every_h": 2,        # 一般提醒每 2 小时汇总一次
        "last_general_at": 0,
        "send_gap_s": [3, 8],        # 每条消息之间的随机间隔（秒）
    },
    "learning": {"last_evolve_at": 0, "last_retest_at": 0, "embed_model": ""},
    "onboarding": {"max_pages": 200, "max_pdfs": 20, "delay_seconds": 1.5, "done": False},
    # 线索池管理（PRD 5.2.2）：都是销售主管可调的内部参数
    "lead_pool": {"recheck_days": 7, "expire_days": 30, "batch": 30, "run_every_h": 24, "last_run": 0},
    # 质量抽查（v1 没有人工审核）：主管可调
    "quality": {"spot_rate": 0.10, "max_rate": 0.5, "uncertain": 0.7, "window_days": 7},
}

# 高风险功能：开启需要确认并记录（PRD 第 9 节）
RISKY = {"crawler.respect_robots_off", "joining.assisted_join", "onboarding.ignore_robots",
         "onboarding.site_login"}


def ai_paused() -> bool:
    """菜单栏 / 托盘「暂停所有 AI」（PRD 9.0.1）：暂停时不开始新的找客户 / 监控 / 线索池任务，定时作业也不跑 AI 部分。"""
    return bool((get("pause_all", {}) or {}).get("on"))


def agent_enabled(agent: str) -> bool:
    return bool(get("agents_enabled").get(agent, False))


def get(key, default=None):
    row = db.query_one("SELECT value FROM settings WHERE key=?", (key,))
    if row is None:
        return copy.deepcopy(DEFAULTS.get(key, default))
    val = db.loads(row["value"], default)
    if isinstance(DEFAULTS.get(key), dict) and isinstance(val, dict):
        merged = copy.deepcopy(DEFAULTS[key])
        merged.update(val)
        return merged
    return val


def put(key, value):
    db.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, db.dumps(value)))


def set_secret(name: str, value: str):
    value = (value or "").strip()
    if not value:
        db.execute("DELETE FROM secrets WHERE name=?", (name,))
        return
    db.execute("INSERT INTO secrets(name, value_enc, updated_at) VALUES(?, ?, ?) "
               "ON CONFLICT(name) DO UPDATE SET value_enc=excluded.value_enc, updated_at=excluded.updated_at",
               (name, vault.encrypt(value), db.now()))


def get_secret(name: str) -> str:
    row = db.query_one("SELECT value_enc FROM secrets WHERE name=?", (name,))
    return vault.decrypt(row["value_enc"]) if row else ""


def log_risk(feature: str, enabled: bool, note: str = ""):
    db.execute("INSERT INTO risk_log(ts, feature, enabled, note) VALUES(?, ?, ?, ?)",
               (db.now(), feature, int(enabled), note))


def risk_log():
    return db.query("SELECT * FROM risk_log ORDER BY id DESC LIMIT 100")

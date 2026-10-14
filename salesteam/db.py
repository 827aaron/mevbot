"""SQLite 存储（data/salesteam.db）。单连接 + 锁，WAL 模式，足够本地单用户使用。

表结构为第 2–5 步预留了字段（审核状态、联系记录、创建者=销售主管 等）。
"""
import json
import sqlite3
import threading
import time

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
    name TEXT PRIMARY KEY,
    value_enc TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_configs (
    agent TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    preset TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT 'medium',
    api_key_enc TEXT NOT NULL DEFAULT '',
    -- 预算是“总预算”（设置 → 预算，客户只设一个日/月总额）。下面两列是第 3 步销售主管
    -- 在总预算内部给各智能体做的分配（客户不直接填写）；NULL = 不单独限制
    alloc_daily REAL,
    alloc_monthly REAL,
    alloc_by TEXT NOT NULL DEFAULT '',          -- 'manager'（第 3 步）
    price_in REAL,              -- 美元 / 百万输入 token；NULL = 用内置价格表
    price_out REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    ts REAL NOT NULL,
    day TEXT NOT NULL,          -- 本地日期 YYYY-MM-DD
    month TEXT NOT NULL,        -- YYYY-MM
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    extra_units INTEGER NOT NULL DEFAULT 0,   -- 如 Claude web search 次数
    cost REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_agent_day ON usage(agent, day);
CREATE INDEX IF NOT EXISTS idx_usage_agent_month ON usage(agent, month);

CREATE TABLE IF NOT EXISTS risk_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS icp_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    data TEXT NOT NULL,          -- ICP 字段 JSON
    reasons TEXT NOT NULL DEFAULT '{}',
    chat TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT '',   -- website / text / pdf / manual / copy
    learn TEXT NOT NULL DEFAULT '{}',  -- 第 2 步起：审核通过/拒绝原因、成交标记，用于“越用越准”
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'research',
    icp_id INTEGER,
    params TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending/running/done/cancelled/failed/stopped/interrupted
    target INTEGER NOT NULL DEFAULT 0,
    stats TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '',   -- 结束原因 key
    error TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'user',  -- user / manager（第 3 步销售主管）
    parent_id TEXT,              -- 第 3 步：每日目标 / 补救任务的父任务
    goal_date TEXT,              -- 第 3 步：属于哪一天的每日目标
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
-- 任务的运行方式（PRD 5.1.1）：定时（一次）/ 每天。到点由调度器开一轮找客户任务（tasks.schedule_id 指回这里）
CREATE TABLE IF NOT EXISTS task_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,                     -- once 定时（一次）/ daily 每天
    run_at REAL,                            -- once：开始时间（本机时间）
    time_of_day TEXT NOT NULL DEFAULT '',   -- daily：HH:MM（本机时间）
    params TEXT NOT NULL,                   -- 每一轮的任务参数（模板、数据源、目标 = 每天 / 这一轮的目标、网址）
    status TEXT NOT NULL DEFAULT 'active',  -- active / paused / done（定时的已开始过）/ missed（定时的那天电脑没开）
    created_by TEXT NOT NULL DEFAULT 'user',   -- user / manager（第 3 步销售主管）
    last_day TEXT NOT NULL DEFAULT '',      -- 最近一轮属于哪一天（YYYY-MM-DD，本机）：每天最多一轮（错过了当天补一次）
    last_task_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    level TEXT NOT NULL,
    key TEXT NOT NULL,
    params TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_agent_ts ON task_events(agent, ts);

-- 线索池：调研员 / 监控员采集的原始线索，等待数据师处理
CREATE TABLE IF NOT EXISTS raw_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    source TEXT NOT NULL,        -- google / web / youtube / (第2步) tg / (第4步) facebook ...
    source_url TEXT NOT NULL DEFAULT '',
    source_title TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL,          -- 抽取到的全部字段 JSON
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',   -- new/processing/qualified/backup/discarded/duplicate/filtered/error
    reason TEXT NOT NULL DEFAULT '',
    lead_id INTEGER,
    created_at REAL NOT NULL,
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_raw_task ON raw_leads(task_id);

-- 线索库：数据师处理后的线索（A/B 合格 + C 备用；D 丢弃不进库）
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    company_key TEXT NOT NULL,   -- 规范化公司名（去重用）
    domain TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    customer_type TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL,         -- A/B/C
    score INTEGER NOT NULL DEFAULT 0,
    qualified INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,        -- pending_review/backup；第2步起 approved/rejected/contacted/replied/unsubscribed
    not_qualified_reason TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '{}',
    contacts TEXT NOT NULL DEFAULT '[]',
    sources TEXT NOT NULL DEFAULT '[]',
    first_source TEXT NOT NULL DEFAULT '',
    icp_id INTEGER,
    task_id TEXT,
    -- 以下为第 2/3 步预留
    review_note TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    assigned_to TEXT NOT NULL DEFAULT '',
    contacted INTEGER NOT NULL DEFAULT 0,
    unsubscribed INTEGER NOT NULL DEFAULT 0,
    first_contact_channel TEXT NOT NULL DEFAULT '',
    first_contact_account TEXT NOT NULL DEFAULT '',
    deal INTEGER NOT NULL DEFAULT 0,
    feedback INTEGER NOT NULL DEFAULT 0,        -- 👍=1 / 👎=-1 / 未标=0（PRD 12 节）
    feedback_reason TEXT NOT NULL DEFAULT '',   -- 预设原因代码
    feedback_note TEXT NOT NULL DEFAULT '',     -- “其他”时的自由填写
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
CREATE INDEX IF NOT EXISTS idx_leads_key ON leads(company_key);

-- AI 学习（PRD 第 12 节）：每个模板的正/负样本库
CREATE TABLE IF NOT EXISTS icp_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icp_id INTEGER NOT NULL,
    polarity INTEGER NOT NULL,             -- 1 = 正样本（好例子）/ -1 = 负样本（坏例子）
    origin TEXT NOT NULL,                  -- manual_mark / review_approve / review_reject / manual_paste / outcome
    agent TEXT NOT NULL DEFAULT 'analyst', -- 供哪个智能体学习：analyst（线索打分）/ monitor（第 2 步群消息）
    reason TEXT NOT NULL DEFAULT '',       -- 预设原因代码：competitor / not_buyer / too_small / wrong_country / false_info / other
    note TEXT NOT NULL DEFAULT '',         -- 自由填写的原因 / 说明
    text TEXT NOT NULL DEFAULT '',         -- 样本原文（粘贴的消息或线索摘要）
    lead_id INTEGER,                       -- 来自线索库时关联线索（线索删除后保留快照）
    snapshot TEXT NOT NULL DEFAULT '{}',   -- 线索快照 JSON
    keywords TEXT NOT NULL DEFAULT '[]',   -- 简单关键词（用于第 1 步相关度排序；第 2 步换语义相似）
    weight REAL NOT NULL DEFAULT 1,        -- outcome（回复/询价/成交）在第 3 步为高权重
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_icp ON icp_samples(icp_id, agent);

-- AI 学到的知识（按模板、带版本，可回退）。第 1 步只建结构，第 2 步 AI 学习中心使用
CREATE TABLE IF NOT EXISTS icp_knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icp_id INTEGER NOT NULL,
    version INTEGER NOT NULL,              -- 模板内递增版本号
    kind TEXT NOT NULL,                    -- keywords / examples / rule / message_type
    content TEXT NOT NULL,                 -- JSON
    status TEXT NOT NULL,                  -- auto_applied（关键词/例子自动生效）/ pending_confirmation（大规则变化待确认）
                                           -- / confirmed / rejected / rolled_back
    origin TEXT NOT NULL DEFAULT 'learning', -- learning / manual
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_icp ON icp_knowledge(icp_id, version);

-- ============================== 第 2 步 ==============================
-- TG 账号池（PRD 6.2）。session 加密保存；role: monitor（监控/找群）/ notify（通知号，PRD 7.1）
CREATE TABLE IF NOT EXISTS tg_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'monitor',
    status TEXT NOT NULL DEFAULT 'new',   -- new/code_sent/need_password/active/need_relogin/restricted/flood_wait/disabled
    status_detail TEXT NOT NULL DEFAULT '',
    flood_until REAL,
    session_enc TEXT NOT NULL DEFAULT '',
    login_state TEXT NOT NULL DEFAULT '{}',  -- 登录过程中的临时数据（phone_code_hash 等）
    proxy TEXT NOT NULL DEFAULT '{}',        -- {type, host, port, username, password_enc}
    device TEXT NOT NULL DEFAULT '{}',       -- {device_model, system_version, app_version, lang_code}
    tg_user TEXT NOT NULL DEFAULT '{}',      -- {id, username, name}
    daily_join_limit INTEGER NOT NULL DEFAULT 5,
    daily_send_limit INTEGER NOT NULL DEFAULT 30,
    max_chats INTEGER NOT NULL DEFAULT 480,
    warmup_days INTEGER NOT NULL DEFAULT 3,
    added_at REAL NOT NULL,
    last_ok_at REAL,
    last_monitor_at REAL
);
CREATE TABLE IF NOT EXISTS tg_counters (
    account_id INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day, kind)
);
CREATE TABLE IF NOT EXISTS tg_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'group',     -- group / channel / discussion
    about TEXT NOT NULL DEFAULT '',
    members INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER,                      -- 讨论区所属的频道
    linked_chat_id TEXT NOT NULL DEFAULT '',
    monitored INTEGER NOT NULL DEFAULT 0,
    icp_id INTEGER,                         -- 用哪个模板判断（空 = 监控设置里的默认模板）
    join_method TEXT NOT NULL DEFAULT 'A',  -- A 人工 / B 辅助进群 / C AI 找群
    cursor INTEGER NOT NULL DEFAULT 0,      -- 已读到的消息 ID（只读新消息，断点补读）
    last_read_at REAL,
    msgs_read INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL,
    UNIQUE (account_id, chat_id)
);
CREATE TABLE IF NOT EXISTS tg_join_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/joined/needs_approval/failed/cancelled
    account_id INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',   -- manual（B 粘贴链接）/ discovery（C AI 找群）
    discovery_id INTEGER,
    icp_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    done_at REAL
);
CREATE TABLE IF NOT EXISTS tg_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icp_id INTEGER,
    link TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    about TEXT NOT NULL DEFAULT '',
    members INTEGER NOT NULL DEFAULT 0,
    activity TEXT NOT NULL DEFAULT '',
    relevance REAL NOT NULL DEFAULT 0,
    ad_prob REAL NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',        -- tg_search / web / directory
    status TEXT NOT NULL DEFAULT 'suggested', -- suggested/approved/rejected/joined
    created_at REAL NOT NULL,
    UNIQUE (link)
);
-- 通过通知号发出的所有消息（线索推送、异常提醒、手动转发）；mock 后端也记在这里
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    account_id INTEGER,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- lead_push / alert / manual / test
    text TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,                   -- sent / failed / mock
    error TEXT NOT NULL DEFAULT ''
);
-- 异常提醒（PRD 7.2）：同一问题只提醒一次（key 去重），恢复后发“已恢复”
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    level TEXT NOT NULL,                    -- urgent / general / info
    category TEXT NOT NULL DEFAULT '',
    params TEXT NOT NULL DEFAULT '{}',
    link TEXT NOT NULL DEFAULT '',          -- 软件内入口（#/...）
    status TEXT NOT NULL DEFAULT 'open',    -- open / resolved
    count INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    notified_at REAL,                       -- 已推送到 TG 的时间
    notify_failed INTEGER NOT NULL DEFAULT 0,
    resolved_at REAL,
    resolved_notified INTEGER NOT NULL DEFAULT 0,
    read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, status);
-- AI 记忆中心（PRD 2.5）：备忘录（客户交代的事），确认后长期生效
CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,                    -- 智能体 key，或 all
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending / confirmed / rejected
    origin TEXT NOT NULL DEFAULT 'chat',    -- chat / manual / meeting（第 3 步）
    source_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL
);
-- 单点对话（PRD 2.1.3）
CREATE TABLE IF NOT EXISTS agent_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    ts REAL NOT NULL
);
-- 判断模型每周自检（PRD 2.3 防跑偏）
CREATE TABLE IF NOT EXISTS decision_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    n INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    accuracy REAL NOT NULL,
    baseline REAL,
    action TEXT NOT NULL DEFAULT ''          -- ok / switched_fallback / insufficient_samples
);
-- 本地语义模型的向量缓存
CREATE TABLE IF NOT EXISTS embeddings (
    kind TEXT NOT NULL, ref_id INTEGER NOT NULL, model TEXT NOT NULL, vec BLOB NOT NULL,
    PRIMARY KEY (kind, ref_id, model)
);
-- 入职向导（PRD 3.0）：通读官网 + 追问 → 我方画像 + 理想客户画像（每次完成产生一个新版本）
CREATE TABLE IF NOT EXISTS onboarding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    icp_id INTEGER,                         -- 产出/更新的模板
    status TEXT NOT NULL DEFAULT 'reading', -- reading / interview / review / done / failed / cancelled（跳过 / 重新填写）
    inputs TEXT NOT NULL DEFAULT '{}',
    facts TEXT NOT NULL DEFAULT '[]',       -- 每条信息带来源 URL
    profile TEXT NOT NULL DEFAULT '{}',     -- {company: 我方画像, icp: 理想客户画像}
    chat TEXT NOT NULL DEFAULT '[]',
    login TEXT NOT NULL DEFAULT '{}',       -- 发现的登录入口
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS site_pages (
    onboarding_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'page',      -- page / pdf / member
    status TEXT NOT NULL DEFAULT 'read',    -- read / duplicate / skipped / failed / deferred（稍后再读）/ alias（跳转前的网址）
    simhash TEXT NOT NULL DEFAULT '',
    facts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (onboarding_id, url)
);
-- 我方画像版本（重新开始向导 = 新版本，可对比、可回退）
CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    onboarding_id INTEGER,
    icp_id INTEGER,
    company TEXT NOT NULL,
    icp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- draft / active / superseded / rolled_back
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

-- 智能体的自动调整记录（第 3 步销售主管写日报用）
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);

-- 线索的所有邮箱 / TG 用户名，用于“同一公司多人算 1 个”与跨公司去重
CREATE TABLE IF NOT EXISTS lead_identities (
    kind TEXT NOT NULL,          -- email / tg / domain
    value TEXT NOT NULL,
    lead_id INTEGER NOT NULL,
    PRIMARY KEY (kind, value)
);

-- 线索池管理（PRD 5.2.2）：黑名单（同行 / 已合作客户 / 永不联系）—— 按身份，任何来源再出现都自动排除
CREATE TABLE IF NOT EXISTS contact_blacklist (
    kind TEXT NOT NULL,          -- domain / company / email / tg / wa
    value TEXT NOT NULL,
    reason TEXT NOT NULL,        -- competitor / existing_customer / do_not_contact / unsubscribed
    note TEXT NOT NULL DEFAULT '',
    lead_id INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, value)
);
-- 联系记录（按身份）：同一客户不重复开发（第 3 步发送层先调 leadpool.can_contact）
CREATE TABLE IF NOT EXISTS contact_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    ident_kind TEXT NOT NULL,
    ident_value TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contact_hist ON contact_history(ident_kind, ident_value);
-- 渠道池（范围决定 #5）：所有找客户的地方（TG 群 / 频道、FB 群、论坛、B2B 求购板、招标网、名录、展会名单、
-- YouTube 频道、新闻源），每个渠道的产出按 leads.channel_id 统计
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,               -- tg:@username / tg:#chat_id / 网址
    kind TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    members INTEGER NOT NULL DEFAULT 0,
    activity TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',   -- candidate / monitored / retired
    source TEXT NOT NULL DEFAULT '',            -- tg_joined / ai_discovery / researcher / manual
    tg_chat_id INTEGER,
    discovery_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- 提示词：客户修改过的版本（默认提示词在 salesteam/prompts/default/，PRD 2.4.2）
CREATE TABLE IF NOT EXISTS prompt_overrides (
    name TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    updated_at REAL NOT NULL
);
-- 质量抽查（v1 没有人工审核）：裁判模型复核数据师的等级
CREATE TABLE IF NOT EXISTS quality_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    task_id TEXT,
    company TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    agree INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 1,
    by_model TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
-- 线索池事件：升级（备用→合格）/ 丢弃 / 过期 / 永不联系 —— 日报“线索池状况”用
CREATE TABLE IF NOT EXISTS pool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_plans (
 id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL UNIQUE, goal TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_commands (
 id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL, proposal TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, confirmed_at REAL
);
CREATE TABLE IF NOT EXISTS manager_reports (
 day TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scout_runs (
 day TEXT PRIMARY KEY, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect():
    global _conn
    with _lock:
        if _conn is None:
            paths.ensure_dirs()
            _conn = sqlite3.connect(paths.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


# 旧数据库升级：只增加列，不删数据
MIGRATIONS = [
    ("leads", "reviewed_at", "REAL"),
    ("leads", "review_later_until", "REAL"),
    ("leads", "review_plan", "TEXT NOT NULL DEFAULT ''"),
    ("leads", "review_edits", "TEXT NOT NULL DEFAULT '{}'"),
    ("leads", "pushed_at", "REAL"),
    ("leads", "deal_at", "REAL"),
    ("leads", "message_type", "TEXT NOT NULL DEFAULT ''"),
    ("leads", "intent", "TEXT NOT NULL DEFAULT ''"),
    ("leads", "found_by_account", "INTEGER"),     # PRD 8.3：优先用发现它的 TG 账号私信
    ("icp_samples", "label", "TEXT NOT NULL DEFAULT ''"),   # 监控员样本：buy_request / advertisement / chit_chat
    ("tasks", "kind_detail", "TEXT NOT NULL DEFAULT ''"),
    ("usage", "refused", "INTEGER NOT NULL DEFAULT 0"),
    ("tg_chats", "backfill", "INTEGER NOT NULL DEFAULT 0"),     # 第一次监控：按时间往回补读中
    ("tg_chats", "backlog_days", "REAL NOT NULL DEFAULT 7"),    # 往回读多少天（销售主管调整，程序上限 30 天）
    ("tg_chats", "min_read_id", "INTEGER NOT NULL DEFAULT 0"),  # 已读到的最早消息 ID（往更早补读的边界）
    ("tg_chats", "older_target_id", "INTEGER NOT NULL DEFAULT 0"),  # 往更早补读的目标 ID（0 = 没有补读任务）
    ("tg_chats", "older_cursor", "INTEGER NOT NULL DEFAULT 0"),
    ("tg_chats", "info_at", "REAL"),                            # 人数 / 简介 / 讨论区上次补全的时间（空 = 还没补，人数可能不准）
    ("tg_chats", "left_at", "REAL"),                            # 「已退出」：账号已不在这个群（完整同步时不在群列表里 / 读消息被拒）。空 = 还在
    ("tg_accounts", "dialogs_synced_at", "REAL"),               # 上次完整同步群列表的时间（每天一次，PRD 6.3「群列表自动更新」）
    ("tasks", "schedule_id", "INTEGER"),
    ("usage", "price_src", "TEXT NOT NULL DEFAULT ''"),          # 单价来源：api / customer / reference（参考价）/ estimate（估算）/ fixed                        # 定时 / 每天的任务：这一轮来自哪个安排（PRD 5.1.1）
    ("leads", "posted_at", "REAL"),                             # TG 求购消息的发布时间
    ("usage", "priced", "INTEGER NOT NULL DEFAULT 1"),
    ("leads", "pool_checked_at", "REAL"),                       # 备用池上次复查时间（PRD 5.2.2）
    ("leads", "upgraded_at", "REAL"),                           # 备用 → 合格 的时间
    ("leads", "do_not_contact", "INTEGER NOT NULL DEFAULT 0"),  # 永不联系（永久）
    ("leads", "dnc_reason", "TEXT NOT NULL DEFAULT ''"),
    ("leads", "dnc_at", "REAL"),
    ("leads", "channel_id", "INTEGER"),                         # 来自哪个渠道（渠道池产出统计）
    ("tg_accounts", "roles", "TEXT NOT NULL DEFAULT ''"),       # 用途（可重叠）：monitor,scout / notify（PRD 6.2.1）
    ("onboarding", "error", "TEXT NOT NULL DEFAULT ''"),        # 读资料失败 / 中断的原因（界面显示 + 重试）
    ("onboarding", "resumes", "INTEGER NOT NULL DEFAULT 0"),    # 软件重启后自动接着读的次数（只接一次，防止反复崩溃）
]


def _migrate(conn):
    for table, col, ddl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def execute(sql, args=()):
    with _lock:
        cur = connect().execute(sql, args)
        return cur.lastrowid


def executemany(sql, rows):
    with _lock:
        connect().executemany(sql, rows)


def query(sql, args=()):
    with _lock:
        return [dict(r) for r in connect().execute(sql, args).fetchall()]


def query_one(sql, args=()):
    with _lock:
        r = connect().execute(sql, args).fetchone()
        return dict(r) if r else None


class transaction:
    """with db.transaction(): ...  —— 多条写入原子执行。"""

    def __enter__(self):
        _lock.acquire()
        connect().execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type, exc, tb):
        try:
            connect().execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            _lock.release()


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def now():
    return time.time()

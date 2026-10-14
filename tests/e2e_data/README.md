# 完整走查用的测试数据（tests/e2e_data）

给 `docs/E2E_TEST_PLAN.md` 用。场景是老板举的例子（只是测试数据，不是产品规则）：**一家卖网络游戏充值的公司，找马来西亚的游戏充值代理 / 经销商**，语言中文 / 英文 / 马来文。

所有人名、用户名（都带 `_qa`）、号码、群、网址（`.example`）都是编的。号码只用来测试，不要拨打，也不要在真实 TG 里用这些用户名做任何事。

## 一、有哪些文件

| 文件 | 内容 |
|---|---|
| `template_game_topup.json` | 模板（线索词：求购词 / 产品词 / 排除词，中英马三种语言）、目标国家那句话、快速开始要填的一句话介绍和访谈回答 |
| `tg_chats.json` | 假 TG：5 个账号、10 个已加入的群 / 频道、6 个“AI 找群”能搜到的群、34 个发言人、56 条消息 |
| `search_results.json` | 假的搜索结果和网页（名录、论坛、公司官网）、免费搜索“被拦”的模拟开关 |
| `tools/check_fixture.py` | 用产品自己的规则核对测试数据（不启动服务） |
| `tools/seed_api.py` | 通过本机接口把一个全新的数据目录快速配好（打包版也能用） |
| `tools/fixture_mock.py` | 让假 TG / 假网页 / 免费搜索改用上面的测试数据 |
| `tools/fake_api.py` | 假的 OpenAI 协议 API：Key 无效、没余额、回答格式不对、很慢 |

## 二、测试数据里有什么

### TG 群（`tg_chats.json`）

| 群 | 语言 | 谁在里面 | 用来测什么 |
|---|---|---|---|
| C1 MY Game Topup Reseller Hub | 英 / 马来 | A、B 都在 | 主力群：买家、卖家、博彩、骗子、同行招代理；**两个号都在同一个群**（应只读一次，另一行标“由 xx 号读取”） |
| C2 Kumpulan Agen Topup Game Malaysia | 马来 | A（和 R） | 同一个人跨群重复、同一个 WhatsApp 不同 TG 号、没有用户名的买家 |
| C3 大马游戏充值代理交流群 | 中文 | B | 中文买家、同一个人再发一次、新加坡买家 |
| C4 MLBB Diamond Murah Promo | 马来 | A | 广告群里混着一个真买家 |
| C5 SEA Mobile Gamers Chat | 英文 | B（和 R） | 闲聊群里一个买家、个人玩家 |
| C6 Topup News MY（频道）+ C7 讨论区 | 马来 / 英 | A | 频道新闻（不是买家）、讨论区里的买家（频道要同时读讨论区） |
| C8 Grosir Voucher Game Indonesia | 印尼语 | B | 程序把印尼语当马来语读，但买家在印尼 → 国家不符 |
| C9 Thai Game Card | 泰语 / 越南语 | B | 其他语言直接跳过，不交给任何模型 |
| C10 James 销售团队（通知） | 中文 | 只有通知号 N | 通知号的群不应出现在监控列表，任何时候都不读 |
| P1–P6（还没加入） | — | — | AI 找群：P1、P2 高分（≥70），P3、P5 广告群，P4 只有邀请链接（要审核），P6 中等 |

账号（假 TG 规则：验证码 `12345`；号码以 0 结尾要两步验证密码 `test`；含 `666` 的号操作 2 次后被限制；含 `777` 的号进群时 FloodWait）：

| 号 | 号码 | 用途 |
|---|---|---|
| A | +60 11-2000 1001 | 监控号，新号（养号中） |
| B | +60 11-2000 1010 | 监控号，老号（添加时勾“跳过养号”），要两步验证密码 |
| N | +60 11-2000 1003 | 通知号 |
| R | +60 11-2666 1004 | 场景 10 才加：会被限制 |
| F | +60 11-2777 1005 | 可选：进群时 FloodWait |

TG 凭证（假的，只给假 TG 用）：api_id `1234567`，api_hash `0123456789abcdef0123456789abcdef`。接收人（假的）：线索 `@qa_sales_manager`、`https://t.me/+QAsalesTeamGroup`；提醒 `@qa_ops_oncall`。

### 消息的“期望结果”（expect）

| expect | 意思 | 条数 |
|---|---|---|
| `qualified` | 合格线索（A / B 级，有 TG 或明确的 WhatsApp，国家是马来西亚） | 15（第一天 13，“第二天”2） |
| `backup` | 进备用池（没有 TG 用户名，号码没说是 WhatsApp → 标“可能是 WhatsApp”），不算进目标 | 1 |
| `duplicate_tg` / `duplicate_wa` | 同一个 TG 用户名 / 同一个 WhatsApp → 合并到已有的线索，来源都保留，不重复推送 | 3 / 1 |
| `dropped_country` | 国家不是马来西亚（新加坡、印尼） | 4 |
| `dropped_not_buyer` | 交给 AI 后判断不是买家：卖家广告、闲聊、频道新闻、个人玩家 | 9 |
| `excluded_word` | 命中排除词（同行招代理、博彩、骗子、卖号）→ 程序直接不要，**不交给 AI、不花钱** | 11 |
| `no_keyword` | 没命中任何求购词 / 产品词 → 不交给 AI | 5 |
| `skipped_short` / `skipped_lang` | 太短（少于 12 个字）/ 其他语言（泰语、越南语）→ 跳过 | 3 / 2 |
| `not_read_backlog` | 8 天前发的：第一次监控只往回读 7 天，读不到 | 1 |
| `never_read` | 通知号所在的群，永远不读 | 1 |

消息的 `t` 是相对“测试开始时刻”（第一次读群消息的时候，见第三节“时间线”）的分钟数：负数 = 开始前已有的历史；`+5 / +10 / +20 / +30` 开始后不久出现（下一轮读到）；`+65 / +70` 第 2 轮；`+125` 第 3 轮；`+180 / +200 / +240` 当作“第二天”的新消息。

按产品规则算好的数字（`check_fixture.py` 会打印）：

| 时间段 | 读到的消息 | 太短 | 其他语言 | 命中排除词 | 没命中线索词 | 交给 AI |
|---|---|---|---|---|---|---|
| 第一轮（往回 7 天） | 44（C1 只由一个号读，PRD 15.1-5；变成 56、交给 AI 29 = 读了两遍） | 3 | 2 | 11 | 5 | 23 |
| +0～60 分钟 | 4 | 0 | 0 | 0 | 0 | 4 |
| +60～120 分钟 | 2 | 0 | 0 | 0 | 0 | 2 |
| +120～180 分钟 | 2 | 0 | 0 | 0 | 0 | 2 |
| +180～250 分钟（第二天） | 2 | 0 | 0 | 0 | 0 | 2 |

**假 AI（测试模式）和真实 AI 不一样的地方**（假 AI 只按关键词规则判断，这些不算软件问题）：

- m9（话里有 VIP）、m40（话里有 “bukan penjual = 不是卖家”）：假 AI 会判成广告 → 少 2 条合格
- m23（个人玩家 “nak beli 86 diamond untuk akaun sendiri”）：假 AI 会当成求购、打成 A → 多 1 条合格
- m53（印尼买家说 “jual”）：假 AI 判成广告，结果同样是不要
- 所以第一天合格：**真实 AI 13 条 TG 线索，假 AI 12 条**；再加网页上的 3 条（Kedai Reload Seri Kembangan、Penang eSports Hub、Kedai Baru Ipoh）
- 假 AI 在快速开始里只会生成固定的“光伏”示例模板，测试时用 `template_game_topup.json` 里的线索词覆盖；假 AI 自动补的多语言词带 `[zh]`、`[ms]` 标记，正常

### 搜索结果和网页（`search_results.json`）

| 公司 | 在哪 | 联系方式 | 期望 |
|---|---|---|---|
| Kedai Reload Seri Kembangan | 名录第 1 页（第 2 页又出现一次） | TG + WhatsApp | 合格（第 2 页那次算重复） |
| Gamer Point Cyber Cafe | 名录第 1 页 | 只有邮箱 | 备用池（只有邮箱不算合格） |
| Phone Galaxy Kajang | 名录第 1 页 | 只有普通电话 | 备用池，标“可能是 WhatsApp” |
| TopKing Reload Sdn Bhd | 名录第 1 页 | TG + WhatsApp | 不要（同行：排除词 open agent、排除公司） |
| Lion City Reload Pte Ltd | 名录第 2 页 | WhatsApp | 不要（新加坡） |
| Kedai Reload Shah Alam | 名录第 2 页 | WhatsApp 和 TG 群里 Ahmad 一样 | 合并到 Ahmad 那条线索（TG + 网页两个来源） |
| Penang eSports Hub | 名录第 2 页 | 只有 TG | 合格 |
| Kedai Baru Ipoh | 论坛求购帖 | wa.me 链接 | 合格 |

`free_search_mode`：`ok` 都能搜到；`blocked_first` 第一次被人机验证拦住、之后能搜到（默认）；`blocked_all` 全部被拦。测试中途改这个文件，下一次搜索就按新的值。

## 三、怎么装进一个全新的数据目录

**规矩**：只用自己新建的空数据目录和 8851 以上的端口；**不要碰** `~/Library/Application Support/JamesAgent`（老板的数据）、8765 端口、`/Applications` 里的软件（老板正在用）。

### 用法 A：打包版 + 接口（今天就能用，但只能用软件自带的假光伏群）

```bash
D=<你的新空目录>; APP=<从 DMG 复制出来的 QA 副本>/詹姆斯智能体.app
SALESTEAM_DATA=$D SALESTEAM_PORT=8851 SALESTEAM_HEADLESS=1 "$APP/Contents/MacOS/JamesAgent" &   # 无窗口
python3 -I tests/e2e_data/tools/seed_api.py --base http://127.0.0.1:8851 --steps agree,ai,tg,template,notify
```

- 安装版界面里看不到“测试模式”（这是对的），假 AI、假 TG 只能用 `seed_api.py`（或同样的接口）打开
- 假 AI = 地址 `mock://relay-a`、Key `test-key`（在 0.2.3 里这个 API 的 id 是 `mock_a`）
- **限制**：打包版的假 TG 写死了一套光伏群，读不到 `tg_chats.json`；假网页只认软件自带的几个 `.example` 网址。所以在打包版上只能把流程跑通（任务、监控轮次、去重、推送、提醒），**线索内容对不上这个场景**
- 已在打包好的 0.2.3 上试过 `seed_api.py`（agree / ai / tg / template / notify / budget 全部成功）

### 用法 B：打包版 + 测试钩子（主要用法；PM 2026-10-14 已批准，开发在做）

开发把 `tools/fixture_mock.py` 里的 `install()` 原样放进软件（例如 `salesteam/qa_fixture.py`），在用到假 TG / 假网页 / 免费搜索之前调用一次，只在设置了环境变量时生效：

```
SALESTEAM_QA_FIXTURES=<放 tg_chats.json 和 search_results.json 的目录>
```

- 没设这个变量：软件行为完全不变；客户界面上看不到任何东西（和 U6“安装版隐藏测试入口”不冲突）
- 设了：假 TG 用 `tg_chats.json`，假网页 / 假搜索 / 免费搜索用 `search_results.json`；真实 TG（Telethon）和付费搜索 Key 不受影响
- 约 30 行接线代码；`install()` 只替换函数，不改数据库、不联网
- 有了它，`docs/E2E_TEST_PLAN.md` 里标“数据 G”的步骤都能在**打包好的安装包**上跑：启动时加环境变量 `SALESTEAM_QA_FIXTURES=<本目录的完整路径>`

### 用法 C：开发环境（源码）——不用

PM 2026-10-14 定：**不对源码跑任何东西**。`tools/fixture_mock.py` 里的 `main()`（用源码开测试服务）只留着给开发参考；`install()` 是给开发放进软件的钩子代码。拿到的安装包里没有钩子时，数据 G 的步骤改用数据 S 跑流程，内容核对记“没测”并写明原因。

### 时间线和“快进”

- “测试开始时刻”（t = 0）= **第一次真正读群消息的时候**（第一个勾了 TG 群的任务第一次读群），不是软件启动的时候。所以前面花几个小时做快速开始、配 TG、找群都没关系
- 那一刻数据目录里会生成 `qa_fixture_clock.json` 记下这个时刻，重开软件不变；想从头再来：退出软件、删掉这个文件
- 想让后面的消息提前出现（例如马上测“第二天”的新消息），把里面的 `forward_min` 改成 250，保存即可（不用重启，也不改电脑时间）

### 假 API（`fake_api.py`）

```bash
python3 -I tests/e2e_data/tools/fake_api.py --port 8860
```

在软件里按“自定义 API”添加，协议选 OpenAI，Key 随便填（如 `qa-fake-key`）：

| 地址 | 效果 | 用在 |
|---|---|---|
| `http://127.0.0.1:8860/m/junk/v1` | 回答不是 JSON | 判断模型「测试」应提示“这个模型不能按格式回答选择题” |
| `http://127.0.0.1:8860/m/nokey/v1` | 401 | “Key 无效”+ 服务商原文 |
| `http://127.0.0.1:8860/m/nobalance/v1` | 402 余额不足 | 紧急提醒“API 没有余额” |
| `http://127.0.0.1:8860/v1` | 跟着全局模式（默认正常） | 先正常用，中途 `curl -X POST http://127.0.0.1:8860/_mode -d '{"mode":"nobalance"}'` 模拟“用着用着没钱了” |

`http://127.0.0.1:8860/_log` 看最近 50 次请求。

### 核对测试数据

```bash
.venv/bin/python -I tests/e2e_data/tools/check_fixture.py
```

用产品的规则（语言识别、线索词初筛、假 AI 分类）算一遍每条消息，和 `expect` 比（2026-10-14 跑过：全部一致）。它会导入源码里的规则函数（不启动服务），所以按“不对源码跑任何东西”的规定，**现在不再跑**；以后要跑须 PM 同意。

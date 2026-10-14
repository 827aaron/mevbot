"""第 3 步智能体的接口约定（只定义约定，避免后续改动核心结构）。第 2 步已实现的部分在括号里注明。

- 监控员（第 2 步已实现，agents/monitor.py）：定时读取 TG 群/频道，关键词 + 本机语义初筛 → 判断模型 → 大模型批量判断，
  命中消息写入 raw_leads（source='tg'），交给数据师 analyst.process_raw()——与调研员同一条流水线。

- 销售主管（第 3 步）
  * 下任务：tasks.create_task(params, created_by='manager', parent_id=..., goal_date=...)；读 tasks.stats 判断进度
  * 每日计划必须**落盘**（建议 settings 键 'manager_plan:<YYYY-MM-DD>' 或单独表）：卡住/重启后从计划继续，不重复下任务
  * 由主管在安全范围内调整的参数见 DECISIONS.md「K. 第 2 步」中的“主管可调参数”；调整写 agent_actions 表（可撤销）。
    已有入口：agents/monitor.set_chat_backlog()（每群首读天数 / 往更早补读）
  * 工作台主对话 + 每页抽屉：用 agents/chat.py（history(after)/send(page)/post()/do_action()）。
    主管回复里的“行内动作”只能是 chat.ACTION_TYPES 白名单；撤销类动作由程序生成，模型只能建议 goto。
    主管需要新动作（如“确认放宽条件”“确认备忘录”）时：在 ACTION_TYPES 加类型 + 在 do_action() 里由程序执行
  * 看门狗：主管作业用 heal.register_job('manager', max_silence_s, restart) 注册，并在每一步调用 heal.beat('manager')；
    卡住时看门狗（纯程序，不调用模型）经通知号发 TG（失败用桌面弹窗）并重启作业

- 副主管（第 3 步，接管钩子）：deputy_takeover() —— 主管作业连续重启仍失败时由看门狗调用。约定：
  只读当天计划和 tasks.stats，按计划继续派发已排好的任务（不做新的战略决定、不改客户数字），
  并在主管对话里 post() 一条说明“主管暂时不可用，由副主管按计划继续”。

- 销售员 / 跟进员（v1 不包含，FEATURES["outreach"]=False）：发送前必须调用 leadpool.can_contact(lead_id)
  （永不联系 / 退订 / 黑名单 / 同一客户已联系过 —— 按 TG 用户名 / WhatsApp 判断 → 不发），
  发送成功后调用 leadpool.record_contact(lead_id, channel, account)；语言用 langdetect.reply_lang(客户原文)
  （v1 只用 中文 / 英文 / 马来语，PRD §1）；
  v1 没有审核环节：线索状态 qualified（合格·待推送）→ pushed（已推送给销售）；审核台打开（FEATURES["review"]）时为 approved；
  首次联系后写 leads.first_contact_channel / first_contact_account，回复/成交写 icp_samples(origin='outcome')；
  用 TG 账号私信时必须先 lease.acquire("tg", ...) 借账号，并用 lease.use(..., "send") 计数（PRD 6.2.1）。

- 渠道拓展员（第 3 步完整上线，channels.py / tg/joining.py）：定时发现渠道（TG 搜索 + 网页 site:t.me + 名录 / 论坛 / 展会名单），
  打分后写 channels（register）；客户一次性确认「自动加群」风险后，在每个账号每日上限内自动加群（进群只能用 lease 的 join
  优先级、只在账号空闲时）；FB 群出候选名单 + 建议的入群问题回答；群 → 监控员，名录 / 展会 / 论坛 → 调研员抓取；
  按 channels 的产出（线索数、这条不好）淘汰低产出渠道（set_status → retired）。所有 TG 操作走 joining.scout_op()。

- 账号调度（PRD 6.2.1，lease.py，第 2 步已实现）：任何 AI 用平台账号前 lease.acquire(平台, 用途, purpose=…)，
  用完 release；每个动作 lease.use(…) 计数（每个账号每天的上限，与哪个 AI 无关）；优先级 settings.account_sched.priorities
  由销售主管调整；FB（第 4 步）用 platform="fb"：严格一个一个来、时间片 fb_min_gap_s、所有动作合并计数。
"""


def deputy_takeover(reason: str) -> None:  # pragma: no cover - 第 3 步实现
    """副主管接管钩子（第 3 步）。第 2 步只占位：看门狗目前直接重启作业，不调用这里。"""
    raise NotImplementedError("deputy manager arrives in step 3")

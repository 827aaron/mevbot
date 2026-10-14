"""Telegram（PRD 第 6、7 节）：账号池、进群、监控读取、通知号发送。

后端可切换：telethon（真实，Telethon 官方用户 API）/ mock（测试用假 TG 世界，不联网、不登录真实账号）。
上层代码（pool / joining / 监控员 / 通知）只依赖 base.Backend 接口。
"""

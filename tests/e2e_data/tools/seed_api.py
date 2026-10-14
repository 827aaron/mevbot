"""通过本机接口（127.0.0.1）把一个**全新的测试数据目录**快速配到“可以开始测某个场景”的状态。
只用标准库；打包版（无窗口模式）和开发环境都能用。界面上不显示的测试项（假 AI、假 TG）只能这样打开。

    python3 -I tests/e2e_data/tools/seed_api.py --base http://127.0.0.1:8851 --steps agree,ai,tg,template,notify

步骤（按需要选，逗号分开；all = agree,ai,skipqs,tg,template,notify）：
    agree     同意用户协议（场景 1 要在界面上点，别用这个）
    ai        添加假 API mock://relay-a（Key test-key）给主管；其他 4 个 AI 用 acme/balanced（--other-model same =
              改用「和主管一样」）；判断模型 = 同一个 API 上的 acme/balanced
    skipqs    跳过快速开始的“读资料 / 确认”（只给从场景 3 以后开始的测试用）
    tg        打开假 TG（测试模式）+ 假的 api_id / api_hash；添加 A、B、N 三个号并用验证码 12345 登录（B 要两步密码 test）
    risky     再加 R（会被限制）和 F（进群 FloodWait）两个号（场景 10 用）
    template  建模板「马来西亚游戏充值代理（测试）」（线索词全是测试数据里的）
    notify    线索接收人 / 提醒接收人（假的 TG 用户名和群链接）
    budget    总预算改成 --daily / --monthly（默认 2 / 30 美元）
接口可能随版本变化：每一步都打印返回结果，失败的步骤写“失败”并继续下一步，不会停。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, ".."))
AGENTS = ("scout", "researcher", "monitor", "analyst")
BASE = "http://127.0.0.1:8851"
FAILED = []


def call(method, path, obj=None):
    url = BASE + path
    data = json.dumps(obj).encode() if obj is not None else (b"{}" if method in ("POST", "PUT") else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode() or "{}"
            return r.status, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": str(e)}


def step(name, method, path, obj=None, ok=(200,)):
    code, res = call(method, path, obj)
    good = code in ok
    short = json.dumps(res, ensure_ascii=False)[:220]
    print(f"  {'成功' if good else '失败'} {name}: HTTP {code} {short}")
    if not good:
        FAILED.append(name)
    return res if good else None


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def do_agree():
    step("同意用户协议", "POST", "/api/quickstart/agree")


def do_ai(other_model="acme/balanced"):
    res = step("主管：添加假 API mock://relay-a", "POST", "/api/relay",
               {"base_url": "mock://relay-a", "api_key": "test-key", "protocol": "openai", "role": "manager",
                "name": "测试 API A"})
    rid = ((res or {}).get("relay") or {}).get("id") or "mock_a"       # 0.2.3 里这个假 API 的 id 是 mock_a
    for a in AGENTS:
        if other_model == "same":
            step(f"{a}：和主管一样", "POST", f"/api/agents/{a}/same-as-manager", {})
        else:   # 默认固定用 acme/balanced：「和主管一样」会挑最便宜的 acme/tiny，它一半回答格式不对，跑流程不稳定
            step(f"{a}：{other_model}", "POST", "/api/relay",
                 {"base_url": "mock://relay-a", "api_key": "test-key", "protocol": "openai", "role": a,
                  "model": other_model})
    step(f"判断模型：{rid} 上的 acme/balanced", "PUT", "/api/decision",
         {"provider": "relay", "relay": rid, "model": "acme/balanced"})
    code, d = call("GET", "/api/decision")
    print(f"  判断模型现在：{json.dumps(d, ensure_ascii=False)[:160]}")


def do_skipqs():
    for s in ("read", "confirm"):
        step(f"跳过快速开始：{s}", "POST", "/api/quickstart/skip", {"step": s})


def _accounts(keys):
    tg = load("tg_chats.json")
    have = {a["phone"]: a for a in (call("GET", "/api/tg/accounts")[1].get("accounts") or [])}
    for acc in [a for a in tg["accounts"] if a["key"] in keys]:
        row = have.get(acc["phone"]) or step(f"添加 {acc['key']} {acc['label']}", "POST", "/api/tg/accounts",
                                             {"phone": acc["phone"], "label": acc["label"], "role": acc["role"],
                                              "skip_warmup": acc["skip_warmup"]})
        if not row:
            continue
        aid = row["id"]
        step(f"{acc['key']} 发送验证码", "POST", f"/api/tg/accounts/{aid}/login")
        res = step(f"{acc['key']} 输入验证码 12345", "POST", f"/api/tg/accounts/{aid}/code", {"code": "12345"})
        if res and res.get("status") == "need_password":
            step(f"{acc['key']} 两步验证密码", "POST", f"/api/tg/accounts/{aid}/code",
                 {"code": "12345", "password": acc["password"] or "test"})


def do_tg():
    tg = load("tg_chats.json")
    step("打开假 TG + 测试凭证", "PUT", "/api/tg/config",
         {"backend": "mock", "api_id": tg["tg_api"]["api_id"], "api_hash": tg["tg_api"]["api_hash"]})
    _accounts({"A", "B", "N"})


def do_risky():
    _accounts({"R", "F"})


def do_template():
    t = load("template_game_topup.json")
    step("建模板（游戏充值）", "POST", "/api/icps", {"data": t["data"]})


def do_notify():
    r = load("tg_chats.json")["recipients"]
    step("接收人", "PUT", "/api/notify", {"lead_recipients": r["lead"], "alert_recipients": r["alert"]})


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--steps", default="all")
    ap.add_argument("--daily", type=float, default=2.0)
    ap.add_argument("--monthly", type=float, default=30.0)
    ap.add_argument("--other-model", default="acme/balanced",
                    help="其他 4 个 AI 用的假模型；写 same = 用「和主管一样」接口")
    a = ap.parse_args()
    BASE = a.base.rstrip("/")
    if BASE.split(":")[-1] == "8765":
        raise SystemExit("8765 是老板正在用的软件的端口，不能对它用这个脚本")
    code, meta = call("GET", "/api/meta")
    if code != 200:
        raise SystemExit(f"连不上 {BASE}（{meta}）")
    print(f"软件版本 {meta.get('version')}，地址 {BASE}")
    steps = a.steps.split(",") if a.steps != "all" else ["agree", "ai", "skipqs", "tg", "template", "notify"]
    fns = {"agree": do_agree, "ai": lambda: do_ai(a.other_model), "skipqs": do_skipqs, "tg": do_tg, "risky": do_risky,
           "template": do_template, "notify": do_notify,
           "budget": lambda: step("总预算", "PUT", "/api/budget", {"daily": a.daily, "monthly": a.monthly})}
    for s in steps:
        print(f"[{s}]")
        fns[s.strip()]()
    print("全部完成" if not FAILED else f"有 {len(FAILED)} 步失败：{', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

"""核对测试数据本身对不对：用产品自己的规则（语言识别、线索词初筛、假 AI 的分类规则）算一遍每条消息，
和 tg_chats.json 里写的 expect 比。不启动服务、不联网、不碰任何数据目录。

    <仓库>/.venv/bin/python -I tests/e2e_data/tools/check_fixture.py [--repo <仓库>]

输出：每条消息走到哪一步（规则部分）+ 假 AI 会怎么判；最后列出不一致的地方（有不一致 → 退出码 1）。
"""
import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RULE_EXPECTS = {"never_read", "not_read_backlog", "skipped_short", "skipped_lang", "excluded_word", "no_keyword"}
AI_EXPECTS = {"qualified", "backup", "dropped_country", "dropped_not_buyer", "duplicate_tg", "duplicate_wa"}
BUYERISH = {"buyer", "personal"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", "..")))
    a = ap.parse_args()
    os.environ["SALESTEAM_DATA"] = tempfile.mkdtemp(prefix="qa-fixture-check-")   # 保险：不碰仓库的 data/
    sys.path.insert(0, a.repo)
    from salesteam import langdetect
    from salesteam.agents import icp
    from salesteam.llm.mock import mock_kind

    base = os.path.join(HERE, "..")
    tpl = json.load(open(os.path.join(base, "template_game_topup.json"), encoding="utf-8"))["data"]
    tg = json.load(open(os.path.join(base, "tg_chats.json"), encoding="utf-8"))
    words = icp.lead_words(icp.normalize(tpl))
    users = {u["id"]: u for u in tg["users"]}
    chats = {c["key"]: c for c in tg["chats"]}
    notify_chats = {k for acc in tg["accounts"] if acc["role"] == "notify" for k in acc["chats"]}
    problems, rows = [], []
    ids = [m["id"] for m in tg["messages"]]
    if len(ids) != len(set(ids)):
        problems.append("消息 id 有重复")
    for m in tg["messages"]:
        if m["chat"] not in chats:
            problems.append(f"{m['id']}: 群 {m['chat']} 不存在")
            continue
        if m["from"] not in users:
            problems.append(f"{m['id']}: 发言人 {m['from']} 不存在")
        text = m["text"].strip()
        lang = langdetect.detect(text)
        hit = icp.match_words(text, dict(words))
        if m["chat"] in notify_chats:
            rule = "never_read"
        elif m["t"] < -7 * 1440:
            rule = "not_read_backlog"
        elif len(text) < 12:
            rule = "skipped_short"
        elif not langdetect.allowed(text):
            rule = "skipped_lang"
        elif hit == "excluded":
            rule = "excluded_word"
        elif hit == "":
            rule = "no_keyword"
        else:
            rule = "to_ai"
        mk = mock_kind(text) if rule == "to_ai" else "-"
        exp = m["expect"]
        if exp in RULE_EXPECTS and rule != exp:
            problems.append(f"{m['id']}: 写的是 {exp}，按规则其实是 {rule}  «{text[:60]}»")
        if exp in AI_EXPECTS and rule != "to_ai":
            problems.append(f"{m['id']}: 写的是 {exp}（要交给 AI），按规则其实是 {rule}  «{text[:60]}»")
        mock_diff = ""
        if rule == "to_ai":
            want_buy = m["truth"] == "buyer"
            if want_buy != (mk == "buy_request") and not (m["truth"] == "personal" and mk != "buy_request"):
                mock_diff = f"假 AI 会判成 {mk}"
                if not m.get("mock_note"):
                    problems.append(f"{m['id']}: {mock_diff}，但没写 mock_note")
            elif m.get("truth") == "personal" and mk == "buy_request":
                mock_diff = "假 AI 会当成求购"
        rows.append((m["id"], m["chat"], m["t"], lang, hit or "-", rule, exp, mk, mock_diff))
    print(f"{'id':5} {'群':4} {'t(分)':>7} {'语言':6} {'线索词':9} {'规则结果':17} {'期望':18} {'假AI':15} 备注")
    for r in rows:
        print(f"{r[0]:5} {r[1]:4} {r[2]:>7} {r[3]:6} {r[4]:9} {r[5]:17} {r[6]:18} {r[7]:15} {r[8]}")
    day1 = [m for m in tg["messages"] if m["expect"] == "qualified" and -7 * 1440 <= m["t"] <= 125]
    day2 = [m for m in tg["messages"] if m["expect"] == "qualified" and m["t"] > 125]
    print(f"\n第一天（t ≤ 125 分钟）期望合格：{len(day1)} 条：{', '.join(m['id'] for m in day1)}")
    print(f"“第二天”（t > 125 分钟）期望新合格：{len(day2)} 条：{', '.join(m['id'] for m in day2)}")
    by = {}
    for r in rows:
        by[r[6]] = by.get(r[6], 0) + 1
    print("各类数量：" + " · ".join(f"{k} {v}" for k, v in sorted(by.items())))
    if problems:
        print("\n不一致：")
        for p in problems:
            print(" - " + p)
        sys.exit(1)
    print("\n测试数据和产品规则一致。")


if __name__ == "__main__":
    main()

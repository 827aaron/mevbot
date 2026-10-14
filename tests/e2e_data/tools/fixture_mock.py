"""让测试模式的假 TG / 假网页 / 免费搜索改用 tests/e2e_data 里的测试数据（只给测试用）。

产品里的假 TG（salesteam/tg/mock.py）写死了一套光伏群，假网页（salesteam/sources/mockweb.py）按产品词现编公司，
都读不了外部数据。这个文件把它们换成 tg_chats.json / search_results.json 里的内容：

    install(tg_json, web_json, state_dir)   # 在同一个进程里替换（不改产品代码文件）

用法：开发把 install() 放进软件里，用环境变量 SALESTEAM_QA_FIXTURES=<目录> 打开（PM 2026-10-14 已批准，见 README 用法 B）。
main()（用源码开测试服务）只留给开发参考；测试不对源码跑任何东西。

时间线：tg_chats.json 里每条消息的 t（分钟）相对“测试开始时刻”= 第一次真正读群消息的时候，记在
<state_dir>/qa_fixture_clock.json（重开软件不变）。想“快进”（让后面的消息提前出现）就改这个文件的 forward_min；
想从头再来就退出软件、删掉这个文件。
"""
import json
import os
import sys
import threading
import time

_lock = threading.Lock()


def _digits(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Clock:
    """t=0 的时刻 = 第一次真正读群消息的时候（第一个勾了 TG 群的任务第一次读群），不是软件启动的时候——
    这样前面花几个小时做快速开始、配 TG、找群，也不会让“开始后 5 / 65 / 125 分钟才出现”的消息提前出现。
    记在 qa_fixture_clock.json（重开软件不变）；文件改了（快进）就重新读；删掉文件 = 下次读群时重新开始。"""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "qa_fixture_clock.json")
        self.state_dir = state_dir
        self._cache = None

    def _create(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"epoch": time.time(), "forward_min": 0,
                       "_说明": "epoch = 测试开始时刻（第一次读群消息）；forward_min = 快进的分钟数（改大 = 后面的消息提前出现）"}, f)

    def epoch(self) -> float:
        with _lock:
            if not os.path.exists(self.path):
                self._create()
        try:
            mt = os.stat(self.path).st_mtime
            if self._cache and self._cache[0] == mt:
                return self._cache[1]
            c = _load(self.path)
            v = float(c["epoch"]) - float(c.get("forward_min") or 0) * 60
            self._cache = (mt, v)
            return v
        except (OSError, ValueError, KeyError):
            return self._cache[1] if self._cache else time.time()

    def now_min(self) -> float:
        return (time.time() - self.epoch()) / 60


class TGData:
    def __init__(self, path, clock):
        d = _load(path)
        self.d, self.clock = d, clock
        self.chats = d["chats"] + d.get("discover_only", [])
        self.key2id = {c["key"]: c["chat_id"] for c in self.chats}
        self.users = {int(u["id"]): u for u in d["users"]}
        self.by_name = {u["username"].lower(): u for u in d["users"] if u.get("username")}
        per = {}
        for m in d["messages"]:
            per.setdefault(self.key2id[m["chat"]], []).append(m)
        self.msgs = {cid: sorted(ms, key=lambda m: m["t"]) for cid, ms in per.items()}
        self.members = {_digits(a["phone"]): [self.key2id[k] for k in a["chats"]] for a in d["accounts"]}

    def as_tuple(self, c):
        """产品假 TG 用的格式：(chat_id, title, username, kind, members, about, period_min, mix, linked)。"""
        per_day = c.get("msgs_per_day") or max(1, len(self.msgs.get(c["chat_id"], [])))
        return (c["chat_id"], c["title"], c.get("username") or "", c.get("kind") or "group", int(c.get("members") or 0),
                c.get("about") or "", max(1, round(1440 / per_day)), (0.0, 0.0), c.get("linked_chat_id") or "")

    def released(self, chat_id):
        ms = self.msgs.get(str(chat_id), [])
        if not ms:                       # 没有消息的群（例如 AI 找群预览）不启动时钟
            return []
        now = self.clock.now_min()
        return [m for m in ms if m["t"] <= now]

    def message(self, chat_id, n):
        ms = self.msgs.get(str(chat_id), [])
        if not 1 <= n <= len(ms):
            return None
        m = ms[n - 1]
        u = self.users.get(int(m["from"]), {})
        c = next(x for x in self.chats if x["chat_id"] == str(chat_id))
        un = u.get("username") or ""
        link = f"https://t.me/{c['username']}/{n}" if c.get("username") else f"https://t.me/c/{str(chat_id)[4:]}/{n}"
        return {"id": n, "date": int(self.clock.epoch() + m["t"] * 60), "text": m["text"], "sender_id": int(m["from"]),
                "sender_username": un, "sender_name": u.get("name") or "", "link": link, "reply_to": None,
                "_kind": m.get("truth", "")}


class WebData:
    def __init__(self, path):
        self.path = path
        self.calls = 0
        self._mt = None
        self.reload()

    def reload(self):
        mt = os.stat(self.path).st_mtime
        if mt != self._mt:
            self.d, self._mt = _load(self.path), mt
            self.pages = {k.rstrip("/"): v for k, v in self.d.get("pages", {}).items()}
        return self.d

    def results(self, query, n):
        d = self.reload()
        q = (query or "").lower()
        hit = next((g for g in d["results"] if not g.get("default") and any(w.lower() in q for w in g["when_any"])), None)
        hit = hit or next((g for g in d["results"] if g.get("default")), {"items": []})
        return [dict(x) for x in hit["items"]][:n]

    def page(self, url):
        self.reload()
        p = self.pages.get(str(url).rstrip("/"))
        if not p:
            return None
        return {"url": url, "title": p.get("title", ""), "markdown": p.get("markdown", ""),
                "links": [tuple(x) for x in p.get("links", [])]}


def install(tg_json=None, web_json=None, state_dir=None):
    """把测试数据装进产品的假 TG / 假网页 / 免费搜索（同一进程里替换函数，不改文件）。"""
    state_dir = state_dir or os.environ.get("SALESTEAM_DATA") or "."
    clock = Clock(state_dir)
    out = {}
    if tg_json:
        from salesteam.tg import mock as tgm
        tg = TGData(tg_json, clock)
        tgm.CHATS[:] = [tg.as_tuple(c) for c in tg.d["chats"]]
        tgm.PUBLIC_EXTRA[:] = [tg.as_tuple(c) for c in tg.d.get("discover_only", [])]   # P4（邀请链接）必须排第 4 个

        def _latest(c, now=None):
            return len(tg.released(c[0]))

        def _message(c, mid):
            m = tg.message(c[0], int(mid))
            if m is None:                                  # 产品代码不会要不存在的 ID；保险起见给个空消息
                return {"id": mid, "date": int(time.time()), "text": "", "sender_id": 0, "sender_username": "",
                        "sender_name": "", "link": "", "reply_to": None, "_kind": ""}
            return m

        def _my_chat_ids(self, acc):
            base = list(tg.members.get(_digits(acc["phone"]), []))
            left = self.left.get(acc["phone"], set())
            return [cid for cid in base + sorted(self.joined.get(acc["phone"], set())) if cid not in left]

        def id_before(self, acc, chat, ts):
            ms = tg.released(chat["chat_id"])
            if not ms:
                return 0
            ep = clock.epoch()
            return max([i + 1 for i, m in enumerate(ms) if ep + m["t"] * 60 < ts] or [0])

        def user_info(self, acc, user_ref):
            ref = str(user_ref).lstrip("@").lower()
            u = tg.users.get(int(ref)) if ref.isdigit() else tg.by_name.get(ref)
            u = u or {}
            return {"username": u.get("username", ""), "name": u.get("name", ""), "bio": u.get("bio", "")}

        tgm._latest = _latest
        tgm._message = _message
        tgm.MockBackend._my_chat_ids = _my_chat_ids
        tgm.MockBackend.id_before = id_before
        tgm.MockBackend.user_info = user_info
        out["tg"] = tg
    if web_json:
        from salesteam.errors import UserError
        from salesteam.sources import mockweb, search
        web = WebData(web_json)
        orig_page = mockweb.page

        def page(url):
            return web.page(url) or orig_page(url)

        def fake_search(query, countries=None, keyword="", n=4):
            return web.results(query, n)

        def make_free(name):
            def free(query, country="", lang="", n=8, crawler=None, *a, **k):
                with _lock:
                    web.calls += 1
                    k_ = web.calls
                mode = web.reload().get("free_search_mode", "ok")
                if mode == "blocked_all" or (mode == "blocked_first" and k_ == 1):
                    raise UserError("search_blocked", provider="DuckDuckGo")
                return web.results(query, n)
            free.__name__ = name
            return free

        mockweb.page = page
        mockweb.search = fake_search
        for name in [x for x in dir(search) if x.startswith("free") and callable(getattr(search, x))]:
            setattr(search, name, make_free(name))      # 免费搜索（包括以后加的其他免费入口）
        out["web"] = web
    return out


def main():
    import argparse
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="开发环境：用测试数据跑一个测试服务（只监听 127.0.0.1）")
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(here, "..", "..", "..")))
    ap.add_argument("--data", required=True, help="新的空数据目录（不要用老板的数据目录）")
    ap.add_argument("--port", type=int, default=8852)
    ap.add_argument("--tg", default=os.path.join(here, "..", "tg_chats.json"))
    ap.add_argument("--web", default=os.path.join(here, "..", "search_results.json"))
    a = ap.parse_args()
    data = os.path.abspath(a.data)
    if "Application Support/JamesAgent" in data or "\\JamesAgent" in data:
        raise SystemExit("不能用老板的数据目录")
    os.environ.update(SALESTEAM_DATA=data, SALESTEAM_PORT=str(a.port), SALESTEAM_NO_BROWSER_DOWNLOAD="1")
    sys.path.insert(0, a.repo)
    install(a.tg, a.web, data)
    import uvicorn
    from salesteam import server
    server.startup()
    print(f"测试服务：http://127.0.0.1:{a.port}/  数据目录：{data}", flush=True)
    uvicorn.run(server.app, host="127.0.0.1", port=a.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

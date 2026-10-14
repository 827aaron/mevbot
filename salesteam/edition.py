"""Edition selection and evidence-based market checks; never starts business work."""
import copy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse

GENERAL = {"id": "general", "name": {"zh": "詹姆斯智能体", "en": "James Agent"},
           "tagline": {"zh": "本地多智能体销售团队", "en": "Your local AI sales team"},
           "data_dir_name": "JamesAgent", "geo_mode": "registered_country", "target_markets": []}
SPECIAL = {"id": "my_igaming", "name": {"zh": "JamesAgent 马来西亚 iGaming 代理版", "en": "JamesAgent MY iGaming Channels"},
           "tagline": {"zh": "发现公开 iGaming 业务代理线索，市场信息待核实", "en": "Discover public iGaming agent signals; verify market details"},
           "data_dir_name": "JamesAgent-MY-iGaming", "geo_mode": "served_market", "target_markets": ["MY"]}


def current():
    selected = os.environ.get("SALESTEAM_EDITION")
    if selected is None:
        try:
            selected = json.loads(Path(__file__).with_name("edition.json").read_text(encoding="utf-8")).get("edition", "general")
        except (OSError, ValueError, AttributeError):
            selected = "general"
    if selected not in ("general", "my_igaming"):
        raise ValueError("Unsupported SALESTEAM_EDITION")
    return copy.deepcopy(SPECIAL if selected == "my_igaming" else GENERAL)


def presets():
    if current()["id"] != "my_igaming":
        return []
    common = {"geo_mode": "served_market", "target_markets": ["MY"], "countries": [],
              "industries": ["iGaming"], "qualified_grades": ["A", "B"],
              "exclude_words": [], "exclude_companies": [],
              "grading_criteria": [
                  {"name": "Public agent signal", "weight": 80, "description": "The candidate's own sourced iGaming business agent identity or professional referral promotion; mark suspected, not certified"},
                  {"name": "Market information", "weight": 10, "description": "Record sourced Malaysia information as matched, unverified or mismatch; never gate an agent signal"},
                  {"name": "Contact information", "weight": 10, "description": "Record available public business contact; absence must not gate an agent signal"}]}
    specs = [
        ("channel_team", "MY iGaming 代理线索", ["affiliate_agency", "channel_partner", "master_agent"],
         ["iGaming代理团队", "博彩渠道合作", "iGaming affiliate agency", "master agent network", "pasukan ejen iGaming", "rakan saluran iGaming"],
         ["代理合作", "渠道合作", "affiliate partnership", "agent partnership", "kerjasama ejen", "rakan kongsi saluran"]),
    ]
    return [{"key": key, "name": name, "data": {**copy.deepcopy(common), "name": name,
             "b2b_segment": key, "customer_types": types, "product_words": products, "buy_words": buys,
             # A search subject, not an assertion about the user's own product.
             "product": "查找面向马来西亚市场的公开 iGaming 业务代理线索"}}
            for key, name, types, products, buys in specs]


def apply_presets(keys=None, confirmed=False, product=None):
    from . import background, db, settings
    from .agents import icp
    from .errors import UserError
    if confirmed is not True:
        raise UserError("risk_confirm_required")
    if product is not None and (not isinstance(product, str) or not product.strip() or len(product) > 10000):
        raise UserError("invalid_value", field="product")
    available = {p["key"]: p for p in presets()}
    keys = list(available) if keys is None else keys
    if not isinstance(keys, list) or not keys or any(k not in available for k in keys):
        raise UserError("invalid_value", field="edition_presets")
    with background.operation("edition:apply_presets"):
        created = []
        with db.transaction():
            for key in dict.fromkeys(keys):
                # Repeated clicks do not overwrite a user's edited template.
                origin = "edition:my_igaming:" + key
                existing = db.query_one("SELECT id FROM icp_templates WHERE origin=? ORDER BY id LIMIT 1", (origin,))
                if existing:
                    created.append(icp.get(existing["id"]))
                else:
                    data = icp.normalize({**available[key]["data"], **({"product": product.strip()} if product is not None else {})})
                    timestamp = db.now()
                    pid = db.execute("INSERT INTO icp_templates(name,data,origin,created_at,updated_at) VALUES(?,?,?,?,?)",
                                     (data["name"], db.dumps(data), origin, timestamp, timestamp))
                    created.append(icp.get(pid))
            settings.put("edition_presets_confirmed", {"edition": current()["id"], "at": db.now(), "keys": keys})
        from .agents import learning
        for template in created:
            learning.ensure_defaults(template["id"])
        return created


def served_market(icp):
    return icp.get("geo_mode") == "served_market"


def signal_mode(icp):
    return current()["id"] == "my_igaming" and icp.get("b2b_segment") == "channel_team"


def market_instructions(icp):
    if not served_market(icp):
        return ""
    if signal_mode(icp):
        return ("\nAgent discovery rule: retain the candidate's own public iGaming business agent identity or "
                "professional referral/registration promotion with a read source URL and verbatim quote as a "
                "suspected agent lead. Individual professional agents, teams and companies are all allowed. "
                "Target MY is a search direction, not a registration-country or confirmed-service gate. "
                "Record market evidence as matched, unverified or mismatch without discarding agent signals. "
                "Telephone +60, RM, server locations or a bare Malaysia mention never prove market service. "
                "Missing contacts and low AI grades do not discard sourced agent signals. Never invent evidence "
                "or borrow another entity's statement. Never infer agency from player activity or spending; "
                "a normal signup button or a player's personal referral share is insufficient. Do not label "
                "signals confirmed. Preserve geo_mode, target_markets and b2b_segment when editing.")
    return ("\nGeographic rule: target_markets=" + json.dumps(icp.get("target_markets") or []) +
            ". These are markets SERVED, not incorporation or registered-country requirements. "
            "Foreign businesses may qualify. Require a read source URL and verbatim quote explicitly stating "
            "service/operations/customers in the target market. Telephone +60, RM, server locations, "
            "a bare Malaysia mention or assumed language are insufficient. Never invent market evidence. "
            "Unknown market or B2B role belongs in backup; explicit non-service/individual consumers are excluded. "
            "B2B segment=" + str(icp.get("b2b_segment") or "business") + ". Preserve these ICP fields when editing.")


def _sentences(text):
    # Keep original spelling in the evidence displayed/exported to the customer.
    return [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|[\r\n;；]+", text or "") if s.strip()]


def _valid_documents(documents):
    out = []
    for d in documents or []:
        url, text = str(d.get("url") or ""), str(d.get("text") or "")
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc and text:
            out.append({"url": url, "text": text})
    return out


def validate_market(icp, documents, registered_country=""):
    """Deterministic original-text checks. LLM metadata is never evidence."""
    from . import countries
    targets = icp.get("target_markets") or []
    evidence, negative, found = [], [], set()
    docs = _valid_documents(documents)
    for code in targets:
        names = [countries.name(code, "en"), countries.name(code, "zh")]
        if code == "MY":
            names += ["Malaysian", "马来西亚", "Malaysia"]
        alias = "(?:" + "|".join(re.escape(n) for n in names if n) + ")"
        pos = re.compile(r"(?:\b(?:serv(?:e|es|ing)|operat(?:e|es|ing)|offer(?:s|ing)?|support(?:s|ing)?|provide(?:s|ing)?|deliver(?:s|ing)?|supply|supplies|sells?)\b.{0,90}" + alias +
                         r"|" + alias + r".{0,70}\b(?:customers|clients|operators|market|partners)\b.{0,60}\b(?:serv(?:e|ed|ice)|support|offer|provid)\w*" +
                         r"|(?:服务|面向|覆盖|为|向|提供).{0,50}" + alias + r".{0,35}(?:市场|客户|企业|运营|代理|服务|提供|合作)" +
                         r"|\b(?:melayani|menyediakan|beroperasi|menawarkan)\b.{0,90}" + alias + r")", re.I)
        neg = re.compile(r"(?:\b(?:do(?:es)? not|don't|doesn't|no longer|not available|not supported|exclude(?:s|d)?|cannot|can't)\b.{0,65}" + alias +
                         r"|不(?:服务|支持|接受|覆盖|提供).{0,40}" + alias + r"|" + alias + r".{0,35}(?:不支持|不提供|不接受|excluded|not supported|not available)" +
                         r"|\b(?:tidak melayani|tidak tersedia|tidak menyokong)\b.{0,70}" + alias + r")", re.I)
        for d in docs:
            for sentence in _sentences(d["text"]):
                if neg.search(sentence):
                    negative.append({"url": d["url"], "quote": sentence[:1200], "market": code})
                elif pos.search(sentence):
                    # Infrastructure/currency/telephone location is not a served market.
                    infrastructure = re.search(r"servers?|data cent(?:er|re)|hosting location|服务器|伺服器|数据中心", sentence, re.I)
                    customer_scope = re.search(r"customers|clients|operators|markets?|partners|客户|市场|企业|pelanggan|pasaran", sentence, re.I)
                    if infrastructure and not customer_scope:
                        continue
                    evidence.append({"url": d["url"], "quote": sentence[:1200], "market": code})
                    found.add(code)
    status = "mismatch" if negative else "matched" if targets and set(targets) <= found else "unverified"
    return {"status": status, "target_markets": targets, "registered_country": registered_country,
            "evidence": evidence[:10], "negative_evidence": negative[:10]}


def validate_b2b(icp, documents):
    segment = icp.get("b2b_segment") or ""
    positive = (r"iGaming agent|casino agent|betting agent|iGaming affiliate|casino affiliate|sportsbook affiliate|agent (?:for|of|in) (?:iGaming|casino|betting)|(?:I am|we are) .{0,25}agents?|gaming affiliate|affiliate (?:marketing agency|agency|network|team)|master agent|agent (?:company|network|team)|channel partner|博彩代理|iGaming代理|代理公司|代理团队|渠道团队|总代理|ejen iGaming|agen kasino|syarikat ejen|pasukan ejen|rangkaian ejen"
                if segment == "channel_team" else
                r"iGaming (?:operator|company|supplier|platform|provider)|(?:casino|sportsbook|betting) (?:operator|platform provider|software supplier)|博彩(?:企业|运营商|平台供应商)|iGaming公司|pengendali iGaming|pembekal (?:platform|iGaming)")
    consumer = re.compile(r"\bi(?:'m| am) (?:a )?(?:player|gambler)\b|个人玩家|我是玩家|saya (?:seorang )?pemain", re.I)
    denies = re.compile(r"\bnot (?:an? )?(?:affiliate agency|agent|channel partner)\b|不是代理|非代理|bukan ejen", re.I)
    pos = re.compile(positive, re.I)
    industry = re.compile(r"iGaming|betting|casino|gambling|sportsbook|博彩|赌博|kasino|pertaruhan", re.I)
    promotion = re.compile(r"(?:https?://\S+|(?:referral|affiliate|agent|invite|promotion|promo)\s*(?:code|id)\s*[:=]?\s*[A-Za-z0-9_-]{3,}|(?:邀请码|推广码|代理码|注册码)\s*[:：]?\s*[A-Za-z0-9_-]{3,})", re.I)
    business = re.compile(r"affiliate|\bagents?\b|commission|referral (?:business|partner)|professional promot(?:er|ion)|promotion partnership|推广合作|职业推广|代理推广|返佣合作|代理|佣金|ejen|komisen", re.I)
    evidence, negative, consumer_negative = [], [], []
    for d in _valid_documents(documents):
        for sentence in _sentences(d["text"]):
            item = {"url": d["url"], "quote": sentence[:1200]}
            if denies.search(sentence):
                negative.append(item)
                continue
            if consumer.search(sentence):
                consumer_negative.append(item)
            role = bool(pos.search(sentence))
            promotional = bool(promotion.search(sentence) and business.search(sentence)
                               and not consumer.search(d["text"]))
            if (role or promotional) and (not signal_mode(icp) or industry.search(d["text"])):
                evidence.append(item)
    return {"status": "mismatch" if negative or (consumer_negative and not evidence) else "matched" if evidence else "unverified",
            "segment": segment, "evidence": evidence[:10], "negative_evidence": (negative + consumer_negative)[:10]}

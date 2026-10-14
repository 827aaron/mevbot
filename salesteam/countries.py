"""国家 / 大区数据：用于显示国名、推断国家，以及“选国家后自动适配”搜索语言。

每个国家：代码、中文名、英文名、主要商务语言、主时区（时区暂不显示，以后发消息时用）。
受全面制裁的国家/地区（如伊朗、朝鲜、叙利亚、古巴）未列入，见 docs/DECISIONS.md。
"""

REGIONS = [
    ("western_europe", "西欧", "Western Europe", [
        ("DE", "德国", "Germany", ["de"], "Europe/Berlin"),
        ("FR", "法国", "France", ["fr"], "Europe/Paris"),
        ("GB", "英国", "United Kingdom", ["en"], "Europe/London"),
        ("NL", "荷兰", "Netherlands", ["nl", "en"], "Europe/Amsterdam"),
        ("BE", "比利时", "Belgium", ["nl", "fr"], "Europe/Brussels"),
        ("LU", "卢森堡", "Luxembourg", ["fr", "de"], "Europe/Luxembourg"),
        ("AT", "奥地利", "Austria", ["de"], "Europe/Vienna"),
        ("CH", "瑞士", "Switzerland", ["de", "fr", "it"], "Europe/Zurich"),
        ("IE", "爱尔兰", "Ireland", ["en"], "Europe/Dublin"),
    ]),
    ("northern_europe", "北欧", "Northern Europe", [
        ("SE", "瑞典", "Sweden", ["sv", "en"], "Europe/Stockholm"),
        ("NO", "挪威", "Norway", ["no", "en"], "Europe/Oslo"),
        ("DK", "丹麦", "Denmark", ["da", "en"], "Europe/Copenhagen"),
        ("FI", "芬兰", "Finland", ["fi", "sv"], "Europe/Helsinki"),
        ("IS", "冰岛", "Iceland", ["is", "en"], "Atlantic/Reykjavik"),
        ("EE", "爱沙尼亚", "Estonia", ["et", "en"], "Europe/Tallinn"),
        ("LV", "拉脱维亚", "Latvia", ["lv", "en"], "Europe/Riga"),
        ("LT", "立陶宛", "Lithuania", ["lt", "en"], "Europe/Vilnius"),
    ]),
    ("southern_europe", "南欧", "Southern Europe", [
        ("IT", "意大利", "Italy", ["it"], "Europe/Rome"),
        ("ES", "西班牙", "Spain", ["es"], "Europe/Madrid"),
        ("PT", "葡萄牙", "Portugal", ["pt"], "Europe/Lisbon"),
        ("GR", "希腊", "Greece", ["el", "en"], "Europe/Athens"),
        ("MT", "马耳他", "Malta", ["en"], "Europe/Malta"),
        ("CY", "塞浦路斯", "Cyprus", ["el", "en"], "Asia/Nicosia"),
        ("HR", "克罗地亚", "Croatia", ["hr"], "Europe/Zagreb"),
        ("SI", "斯洛文尼亚", "Slovenia", ["sl"], "Europe/Ljubljana"),
    ]),
    ("eastern_europe", "东欧", "Eastern Europe", [
        ("PL", "波兰", "Poland", ["pl"], "Europe/Warsaw"),
        ("CZ", "捷克", "Czechia", ["cs"], "Europe/Prague"),
        ("SK", "斯洛伐克", "Slovakia", ["sk"], "Europe/Bratislava"),
        ("HU", "匈牙利", "Hungary", ["hu"], "Europe/Budapest"),
        ("RO", "罗马尼亚", "Romania", ["ro"], "Europe/Bucharest"),
        ("BG", "保加利亚", "Bulgaria", ["bg"], "Europe/Sofia"),
        ("RS", "塞尔维亚", "Serbia", ["sr"], "Europe/Belgrade"),
        ("BA", "波黑", "Bosnia and Herzegovina", ["bs", "hr", "sr"], "Europe/Sarajevo"),
        ("AL", "阿尔巴尼亚", "Albania", ["sq"], "Europe/Tirane"),
        ("MK", "北马其顿", "North Macedonia", ["mk"], "Europe/Skopje"),
        ("MD", "摩尔多瓦", "Moldova", ["ro", "ru"], "Europe/Chisinau"),
        ("UA", "乌克兰", "Ukraine", ["uk", "ru"], "Europe/Kyiv"),
        ("RU", "俄罗斯", "Russia", ["ru"], "Europe/Moscow"),
    ]),
    ("north_america", "北美", "North America", [
        ("US", "美国", "United States", ["en", "es"], "America/New_York"),
        ("CA", "加拿大", "Canada", ["en", "fr"], "America/Toronto"),
    ]),
    ("latin_america", "拉丁美洲", "Latin America", [
        ("MX", "墨西哥", "Mexico", ["es"], "America/Mexico_City"),
        ("GT", "危地马拉", "Guatemala", ["es"], "America/Guatemala"),
        ("CR", "哥斯达黎加", "Costa Rica", ["es"], "America/Costa_Rica"),
        ("PA", "巴拿马", "Panama", ["es"], "America/Panama"),
        ("DO", "多米尼加", "Dominican Republic", ["es"], "America/Santo_Domingo"),
        ("CO", "哥伦比亚", "Colombia", ["es"], "America/Bogota"),
        ("VE", "委内瑞拉", "Venezuela", ["es"], "America/Caracas"),
        ("EC", "厄瓜多尔", "Ecuador", ["es"], "America/Guayaquil"),
        ("PE", "秘鲁", "Peru", ["es"], "America/Lima"),
        ("BR", "巴西", "Brazil", ["pt"], "America/Sao_Paulo"),
        ("BO", "玻利维亚", "Bolivia", ["es"], "America/La_Paz"),
        ("CL", "智利", "Chile", ["es"], "America/Santiago"),
        ("AR", "阿根廷", "Argentina", ["es"], "America/Argentina/Buenos_Aires"),
        ("UY", "乌拉圭", "Uruguay", ["es"], "America/Montevideo"),
        ("PY", "巴拉圭", "Paraguay", ["es"], "America/Asuncion"),
    ]),
    ("middle_east", "中东", "Middle East", [
        ("AE", "阿联酋", "United Arab Emirates", ["ar", "en"], "Asia/Dubai"),
        ("SA", "沙特阿拉伯", "Saudi Arabia", ["ar", "en"], "Asia/Riyadh"),
        ("QA", "卡塔尔", "Qatar", ["ar", "en"], "Asia/Qatar"),
        ("KW", "科威特", "Kuwait", ["ar", "en"], "Asia/Kuwait"),
        ("OM", "阿曼", "Oman", ["ar", "en"], "Asia/Muscat"),
        ("BH", "巴林", "Bahrain", ["ar", "en"], "Asia/Bahrain"),
        ("IL", "以色列", "Israel", ["he", "en"], "Asia/Jerusalem"),
        ("JO", "约旦", "Jordan", ["ar", "en"], "Asia/Amman"),
        ("LB", "黎巴嫩", "Lebanon", ["ar", "fr", "en"], "Asia/Beirut"),
        ("IQ", "伊拉克", "Iraq", ["ar"], "Asia/Baghdad"),
        ("TR", "土耳其", "Türkiye", ["tr"], "Europe/Istanbul"),
    ]),
    ("africa", "非洲", "Africa", [
        ("EG", "埃及", "Egypt", ["ar", "en"], "Africa/Cairo"),
        ("MA", "摩洛哥", "Morocco", ["ar", "fr"], "Africa/Casablanca"),
        ("DZ", "阿尔及利亚", "Algeria", ["ar", "fr"], "Africa/Algiers"),
        ("TN", "突尼斯", "Tunisia", ["ar", "fr"], "Africa/Tunis"),
        ("NG", "尼日利亚", "Nigeria", ["en"], "Africa/Lagos"),
        ("GH", "加纳", "Ghana", ["en"], "Africa/Accra"),
        ("CI", "科特迪瓦", "Côte d'Ivoire", ["fr"], "Africa/Abidjan"),
        ("SN", "塞内加尔", "Senegal", ["fr"], "Africa/Dakar"),
        ("CM", "喀麦隆", "Cameroon", ["fr", "en"], "Africa/Douala"),
        ("KE", "肯尼亚", "Kenya", ["en", "sw"], "Africa/Nairobi"),
        ("ET", "埃塞俄比亚", "Ethiopia", ["am", "en"], "Africa/Addis_Ababa"),
        ("TZ", "坦桑尼亚", "Tanzania", ["sw", "en"], "Africa/Dar_es_Salaam"),
        ("UG", "乌干达", "Uganda", ["en"], "Africa/Kampala"),
        ("RW", "卢旺达", "Rwanda", ["en", "fr"], "Africa/Kigali"),
        ("AO", "安哥拉", "Angola", ["pt"], "Africa/Luanda"),
        ("ZM", "赞比亚", "Zambia", ["en"], "Africa/Lusaka"),
        ("ZA", "南非", "South Africa", ["en"], "Africa/Johannesburg"),
    ]),
    ("south_asia", "南亚", "South Asia", [
        ("IN", "印度", "India", ["en", "hi"], "Asia/Kolkata"),
        ("PK", "巴基斯坦", "Pakistan", ["en", "ur"], "Asia/Karachi"),
        ("BD", "孟加拉国", "Bangladesh", ["bn", "en"], "Asia/Dhaka"),
        ("LK", "斯里兰卡", "Sri Lanka", ["en", "si"], "Asia/Colombo"),
        ("NP", "尼泊尔", "Nepal", ["ne", "en"], "Asia/Kathmandu"),
    ]),
    ("southeast_asia", "东南亚", "Southeast Asia", [
        ("SG", "新加坡", "Singapore", ["en", "zh", "ms"], "Asia/Singapore"),
        ("MY", "马来西亚", "Malaysia", ["ms", "en", "zh"], "Asia/Kuala_Lumpur"),
        ("ID", "印度尼西亚", "Indonesia", ["id"], "Asia/Jakarta"),
        ("TH", "泰国", "Thailand", ["th"], "Asia/Bangkok"),
        ("VN", "越南", "Vietnam", ["vi"], "Asia/Ho_Chi_Minh"),
        ("PH", "菲律宾", "Philippines", ["en", "tl"], "Asia/Manila"),
        ("KH", "柬埔寨", "Cambodia", ["km", "en"], "Asia/Phnom_Penh"),
        ("MM", "缅甸", "Myanmar", ["my", "en"], "Asia/Yangon"),
    ]),
    ("east_asia", "东亚", "East Asia", [
        ("JP", "日本", "Japan", ["ja"], "Asia/Tokyo"),
        ("KR", "韩国", "South Korea", ["ko"], "Asia/Seoul"),
        ("HK", "中国香港", "Hong Kong SAR", ["zh-hant", "en"], "Asia/Hong_Kong"),
        ("TW", "中国台湾", "Taiwan", ["zh-hant"], "Asia/Taipei"),
        ("MO", "中国澳门", "Macao SAR", ["zh-hant", "pt"], "Asia/Macau"),
        ("CN", "中国大陆", "Mainland China", ["zh"], "Asia/Shanghai"),
        ("MN", "蒙古", "Mongolia", ["mn", "en"], "Asia/Ulaanbaatar"),
    ]),
    ("central_asia", "中亚及高加索", "Central Asia & Caucasus", [
        ("KZ", "哈萨克斯坦", "Kazakhstan", ["ru", "kk"], "Asia/Almaty"),
        ("UZ", "乌兹别克斯坦", "Uzbekistan", ["uz", "ru"], "Asia/Tashkent"),
        ("KG", "吉尔吉斯斯坦", "Kyrgyzstan", ["ru", "ky"], "Asia/Bishkek"),
        ("TJ", "塔吉克斯坦", "Tajikistan", ["tg", "ru"], "Asia/Dushanbe"),
        ("TM", "土库曼斯坦", "Turkmenistan", ["tk", "ru"], "Asia/Ashgabat"),
        ("AZ", "阿塞拜疆", "Azerbaijan", ["az", "ru"], "Asia/Baku"),
        ("GE", "格鲁吉亚", "Georgia", ["ka", "en"], "Asia/Tbilisi"),
        ("AM", "亚美尼亚", "Armenia", ["hy", "ru"], "Asia/Yerevan"),
    ]),
    ("oceania", "大洋洲", "Oceania", [
        ("AU", "澳大利亚", "Australia", ["en"], "Australia/Sydney"),
        ("NZ", "新西兰", "New Zealand", ["en"], "Pacific/Auckland"),
        ("FJ", "斐济", "Fiji", ["en"], "Pacific/Fiji"),
        ("PG", "巴布亚新几内亚", "Papua New Guinea", ["en"], "Pacific/Port_Moresby"),
    ]),
]

LANGUAGES = {
    "en": ("英语", "English"), "de": ("德语", "German"), "fr": ("法语", "French"), "es": ("西班牙语", "Spanish"),
    "it": ("意大利语", "Italian"), "pt": ("葡萄牙语", "Portuguese"), "nl": ("荷兰语", "Dutch"),
    "pl": ("波兰语", "Polish"), "cs": ("捷克语", "Czech"), "sk": ("斯洛伐克语", "Slovak"),
    "hu": ("匈牙利语", "Hungarian"), "ro": ("罗马尼亚语", "Romanian"), "bg": ("保加利亚语", "Bulgarian"),
    "sr": ("塞尔维亚语", "Serbian"), "hr": ("克罗地亚语", "Croatian"), "bs": ("波斯尼亚语", "Bosnian"),
    "sl": ("斯洛文尼亚语", "Slovenian"), "sq": ("阿尔巴尼亚语", "Albanian"), "mk": ("马其顿语", "Macedonian"),
    "uk": ("乌克兰语", "Ukrainian"), "ru": ("俄语", "Russian"), "el": ("希腊语", "Greek"),
    "sv": ("瑞典语", "Swedish"), "no": ("挪威语", "Norwegian"), "da": ("丹麦语", "Danish"),
    "fi": ("芬兰语", "Finnish"), "is": ("冰岛语", "Icelandic"), "et": ("爱沙尼亚语", "Estonian"),
    "lv": ("拉脱维亚语", "Latvian"), "lt": ("立陶宛语", "Lithuanian"), "tr": ("土耳其语", "Turkish"),
    "ar": ("阿拉伯语", "Arabic"), "he": ("希伯来语", "Hebrew"), "sw": ("斯瓦希里语", "Swahili"),
    "am": ("阿姆哈拉语", "Amharic"), "hi": ("印地语", "Hindi"), "ur": ("乌尔都语", "Urdu"),
    "bn": ("孟加拉语", "Bengali"), "si": ("僧伽罗语", "Sinhala"), "ne": ("尼泊尔语", "Nepali"),
    "ms": ("马来语", "Malay"), "id": ("印尼语", "Indonesian"), "th": ("泰语", "Thai"),
    "vi": ("越南语", "Vietnamese"), "tl": ("他加禄语", "Tagalog"), "km": ("高棉语", "Khmer"),
    "my": ("缅甸语", "Burmese"), "ja": ("日语", "Japanese"), "ko": ("韩语", "Korean"),
    "zh": ("简体中文", "Chinese (Simplified)"), "zh-hant": ("繁体中文", "Chinese (Traditional)"),
    "mn": ("蒙古语", "Mongolian"), "kk": ("哈萨克语", "Kazakh"), "uz": ("乌兹别克语", "Uzbek"),
    "ky": ("吉尔吉斯语", "Kyrgyz"), "tg": ("塔吉克语", "Tajik"), "tk": ("土库曼语", "Turkmen"),
    "az": ("阿塞拜疆语", "Azerbaijani"), "ka": ("格鲁吉亚语", "Georgian"), "hy": ("亚美尼亚语", "Armenian"),
}

COUNTRIES = {}
for _rkey, _rzh, _ren, _items in REGIONS:
    for _c in _items:
        COUNTRIES[_c[0]] = {"code": _c[0], "zh": _c[1], "en": _c[2], "langs": _c[3], "tz": _c[4], "region": _rkey}


# ------------------------------------------------------------------ v1 范围（PRD §1）
# 目标国家属于业务内容（老板决定）：软件里不写死任何国家、也没有国家选择器 —— 客户在补充访谈或对主管说出来，
# 存在客户画像 / 理想客户模板里（icp.countries）。合格判定按模板里的国家；客户没说就不按国家筛。
# 第一版只限制客户语言：中文 / 英文 / 马来语（LEAD_LANGS）。下面的国家数据只用来显示国名和推断国家。
V1_SCOPE = True                     # 只表示“第一版的语言范围”
LEAD_LANGS = ["zh", "en", "ms"]


def profile_countries(icp: dict | None) -> list[str]:
    """客户自己说的目标国家（模板里）；空 = 不按国家筛。"""
    return [c for c in (normalize_country(x) for x in (icp or {}).get("countries") or []) if c]


def country_ok(country: str, icp: dict | None) -> bool | None:
    """国家硬条件：客户没说国家 → True；说了 → 在里面才 True；线索国家不明 → None（进备用，复查时再判断）。"""
    allowed = profile_countries(icp)
    if not allowed:
        return True
    if not country:
        return None
    return country in allowed


def lead_langs() -> list[str]:
    return list(LEAD_LANGS) if V1_SCOPE else list(LANGUAGES)


def as_json():
    return {
        "countries": COUNTRIES,          # 只用来显示国名
        "languages": {k: {"zh": v[0], "en": v[1]} for k, v in LANGUAGES.items()},
        "scope": {"v1": V1_SCOPE, "langs": lead_langs() if V1_SCOPE else []},
    }


def name(code: str, lang: str = "en") -> str:
    c = COUNTRIES.get((code or "").upper())
    return c[lang if lang in ("zh", "en") else "en"] if c else (code or "")


def languages_for(codes) -> list[str]:
    """所选国家涉及的语言（去重，保持顺序，总包含英语）。v1 只用 中文 / 英文 / 马来语。"""
    out = []
    for code in codes or []:
        for lg in COUNTRIES.get(code, {}).get("langs", []):
            if lg not in out and (not V1_SCOPE or lg in LEAD_LANGS):
                out.append(lg)
    if not codes and V1_SCOPE:
        out = [x for x in LEAD_LANGS]        # 客户没说国家：用第一版的三种语言
    if "en" not in out:
        out.append("en")
    return out


# ---- 从文字 / 域名 / 电话 / 地址推断国家（合格检查用：和客户说的目标国家比对）
_MY_WORDS = ["malaysia", "kuala lumpur", "selangor", "johor", "penang", "pulau pinang", "sabah", "sarawak", "melaka",
             "malacca", "perak", "ipoh", "kedah", "kelantan", "terengganu", "pahang", "negeri sembilan", "putrajaya",
             "shah alam", "petaling jaya", "klang", "seremban", "kuching", "kota kinabalu", "johor bahru", "sdn bhd",
             "sdn. bhd", "cyberjaya", "puchong", "subang", "马来西亚", "大马", "吉隆坡", "槟城", "柔佛", "新山", "雪兰莪",
             "马六甲", "怡保", "沙巴", "砂拉越", "古晋", "巴生"]
_MY_SHORT = [r"\bkl\b", r"\bjb\b", r"\bmy\b(?=\s*(?:only|stock|delivery))", r"\bmsia\b"]
_SG_WORDS = ["singapore", "s'pore", "pte ltd", "pte. ltd", "jurong", "tuas", "woodlands", "tampines", "changi",
             "ang mo kio", "toa payoh", "paya lebar", "新加坡", "狮城", "星洲", "裕廊"]
_SG_SHORT = [r"\bsg\b", r"\bspore\b"]
_TLD = {"my": "MY", "sg": "SG"}
_DIAL = {"60": "MY", "65": "SG", "62": "ID", "66": "TH", "84": "VN", "63": "PH", "86": "CN", "852": "HK", "886": "TW",
         "91": "IN", "971": "AE", "966": "SA", "44": "GB", "49": "DE", "48": "PL", "33": "FR", "34": "ES", "39": "IT",
         "31": "NL", "61": "AU", "81": "JP", "82": "KR", "7": "RU", "90": "TR", "52": "MX", "55": "BR"}


def infer_country(text: str = "", domain: str = "", phones=()) -> tuple[str, str]:
    """返回 (国家代码, 依据强度 strong/weak/"")。强依据：域名后缀 .my/.sg、电话区号 +60/+65 等；弱依据：文字提到的地名。"""
    import re
    d = (domain or "").lower().strip(".")
    tld = d.rsplit(".", 1)[-1] if "." in d else ""
    if tld in _TLD:
        return _TLD[tld], "strong"
    if len(tld) == 2 and tld.upper() in COUNTRIES and tld not in ("co", "io", "ai", "me", "tv", "cc", "ly"):
        return tld.upper(), "strong"
    for ph in phones or []:
        digits = re.sub(r"\D", "", ph or "")
        if (ph or "").strip().startswith("+") or len(digits) >= 10:
            for n in (3, 2, 1):
                if digits[:n] in _DIAL:
                    return _DIAL[digits[:n]], "strong"
    low = " " + (text or "").lower() + " "
    my = sum(low.count(w) for w in _MY_WORDS) + sum(len(re.findall(p, low)) for p in _MY_SHORT)
    sg = sum(low.count(w) for w in _SG_WORDS) + sum(len(re.findall(p, low)) for p in _SG_SHORT)
    if my or sg:
        return ("MY" if my >= sg else "SG"), "weak"
    for code, c in COUNTRIES.items():
        if code in ("MY", "SG"):
            continue
        if re.search(r"\b" + re.escape(c["en"].lower()) + r"\b", low) or (c["zh"] and c["zh"] in low):
            return code, "weak"
    return "", ""


def lang_name(code: str) -> str:
    return LANGUAGES.get(code, (code, code))[1]


def normalize_country(value: str) -> str:
    """把 AI 返回的国家（代码或中英文名）规范成两位代码；认不出返回空。"""
    v = (value or "").strip()
    if not v:
        return ""
    if v.upper() in COUNTRIES:
        return v.upper()
    if v.upper() == "UK":
        return "GB"
    low = v.lower()
    for code, c in COUNTRIES.items():
        if low in (c["en"].lower(), c["zh"]):
            return code
    aliases = {"usa": "US", "united states of america": "US", "england": "GB", "britain": "GB",
               "great britain": "GB", "uae": "AE", "turkey": "TR", "korea": "KR", "czech republic": "CZ",
               "holland": "NL", "deutschland": "DE", "españa": "ES", "méxico": "MX", "brasil": "BR",
               "ivory coast": "CI", "vietnam": "VN", "viet nam": "VN", "hong kong": "HK", "taiwan": "TW"}
    return aliases.get(low, "")

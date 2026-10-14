"""【仅供开发测试】假网页 / 假搜索结果 / 假 YouTube 数据。

当调研员使用 mock 厂商时，所有数据源都走这里，不访问真实网络。
所有网址都用保留域名 .example，界面上会标明“测试数据”。
数据故意包含：重复公司、没有联系方式的公司、零售商（用于测试排除）、模板外国家，方便验证数据师的筛选/去重。
"""
import hashlib
import random
import re
import urllib.parse

MARK = "(TEST DATA / 测试数据)"

_SUFFIX = {"DE": "GmbH", "AT": "GmbH", "CH": "AG", "FR": "SARL", "ES": "S.L.", "IT": "S.r.l.", "GB": "Ltd",
           "US": "LLC", "NL": "B.V.", "BE": "BV", "PL": "Sp. z o.o.", "AE": "LLC", "SA": "Co.", "BR": "Ltda",
           "MX": "S.A. de C.V.", "AU": "Pty Ltd", "JP": "K.K.", "KR": "Co., Ltd.", "TR": "A.Ş.",
           "MY": "Sdn Bhd", "SG": "Pte Ltd"}
_PREFIX = ["Nord", "Sun", "Blue", "Alpha", "Euro", "Prime", "Green", "Vertex", "Atlas", "Nova", "Delta", "Orbit",
           "Summit", "Pioneer", "Helio", "Terra", "Union", "Metro", "Star", "Royal"]
_TYPES = ["distributor", "wholesaler", "contractor", "retailer", "brand_owner", "installer"]
_FIRST = ["Ahmad", "Siti", "Wei Ming", "Mei Ling", "Raj", "Nurul", "Jason", "Aisyah", "Kelvin", "Farid", "Hui Min", "Daniel"]
_LAST = ["Tan", "Lim", "Wong", "Abdullah", "Ismail", "Lee", "Ng", "Rahman", "Chong", "Kumar"]
_TITLES = ["Purchasing Manager", "Head of Procurement", "CEO", "Sales Director", "Category Manager"]

_REGISTRY: dict[str, dict] = {}


def _rng(*parts) -> random.Random:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def companies(countries: list[str], keyword: str) -> list[dict]:
    """给定国家和关键词，生成一份固定的“市场公司池”（60 家）。"""
    countries = [c for c in countries if c] or ["MY"]
    word = (keyword or "Solar").split()[0].capitalize()
    out = []
    for i in range(60):
        r = _rng(word, i)
        country = countries[i % len(countries)]
        if i % 11 == 10:
            country = "CN"   # 模板外国家，应被数据师筛掉
        name = f"{_PREFIX[i % len(_PREFIX)]} {word} {['Trade', 'Systems', 'Supply', 'Energy', 'Group'][(i + i // 20) % 5]} " \
               f"{_SUFFIX.get(country, 'Ltd')}"
        slug = _slug(name)
        ctype = _TYPES[i % len(_TYPES)]
        person = f"{r.choice(_FIRST)} {r.choice(_LAST)}"
        c = {
            "name": name, "slug": slug, "country": country, "type": ctype,
            "website": f"https://{slug}.example/",
            "email": f"info@{slug}.example" if i % 4 != 3 else "",
            # 目录页没写邮箱的，官网联系页里一半能找到（测试数据师“读官网补全联系方式”）
            "site_email": f"sales@{slug}.example" if i % 8 == 3 else "",
            "telegram": f"@{slug.replace('-', '_')[:20]}" if i % 5 == 0 else "",
            "whatsapp": (f"+65 9{100 + i:03d} {2000 + i:04d}" if country == "SG" else f"+60 12-{100 + i:03d} {2000 + i:04d}")
                        if i % 3 == 0 else "",
            "site_wa": (f"659{100 + i:03d}{3000 + i:04d}" if country == "SG" else f"6012{100 + i:03d}{3000 + i:04d}")
                       if i % 4 == 1 else "",
            "contact": f"{person}, {_TITLES[i % len(_TITLES)]}",
            "employees": [12, 35, 80, 150, 400][i % 5],
            "about": f"{name} is a {ctype} of {word.lower()} products serving customers in "
                     f"{ {'MY': 'Malaysia', 'SG': 'Singapore'}.get(country, country) }. Founded {2001 + i % 20}. {['Recently opened a new warehouse.', 'Hiring a procurement specialist.', 'Expanding to neighbouring markets.', 'Looking for new suppliers from Asia.', ''][i % 5]}",
        }
        _REGISTRY[slug] = c
        out.append(c)
    return out


def _company_block(c: dict, with_email=True) -> str:
    lines = [f"## Company: {c['name']}", f"- Country: {c['country']}", f"- Type: {c['type']}",
             f"- Website: {c['website']}"]
    if with_email and c["email"]:
        lines.append(f"- Email: {c['email']}")
    if c["telegram"]:
        lines.append(f"- Telegram: {c['telegram']}")
    if c.get("whatsapp"):
        lines.append(f"- WhatsApp: {c['whatsapp']}")
    lines.append(f"- Contact: {c['contact']}")
    lines.append(f"- Employees: {c['employees']}")
    lines.append(f"- About: {c['about']}")
    return "\n".join(lines)


def search(query: str, countries: list[str], keyword: str, n: int = 4) -> list[dict]:
    """假搜索结果：返回几个“名录页”网址。"""
    r = _rng("search", query)
    out = []
    for k in range(n):
        page = r.randint(1, 12)
        qs = urllib.parse.urlencode({"c": ",".join(countries), "k": keyword, "p": page})
        out.append({"url": f"https://directory-{k + 1}.example/list?{qs}",
                    "title": f"{keyword} suppliers directory page {page} {MARK}",
                    "snippet": f"List of {keyword} companies {MARK}"})
    return out


def page(url: str) -> dict:
    """假网页：名录页列出 5 家公司；公司官网页给出简介和（可能的）联系邮箱。"""
    u = urllib.parse.urlparse(url)
    host = u.netloc
    sp = site_page(url)
    if sp is not None:
        return sp
    if host.startswith("directory-"):
        q = urllib.parse.parse_qs(u.query)
        countries = (q.get("c", ["MY"])[0] or "MY").split(",")
        keyword = q.get("k", ["Solar"])[0]
        p = int(q.get("p", ["1"])[0] or 1)
        pool = companies(countries, keyword)
        picked = [pool[(p * 4 + j) % len(pool)] for j in range(5)]   # 不同页之间有重叠 → 测试去重
        md = f"# {keyword} companies — directory page {p} {MARK}\n\n" + "\n\n".join(_company_block(c) for c in picked)
        return {"url": url, "title": f"{keyword} directory p{p} {MARK}", "markdown": md, "links": []}
    slug = host.removesuffix(".example")
    c = _REGISTRY.get(slug)
    if c:
        md = f"# {c['name']} {MARK}\n\n{c['about']}\n\nEmployees: about {c['employees']}.\n\n"
        md += "## Contact\n"
        if c["email"]:
            md += f"Email: {c['email']}\n"
        if c["site_email"]:
            md += f"Email: {c['site_email']}\n"
        md += f"Contact person: {c['contact']}\n"
        links = [(f"https://wa.me/{c['site_wa']}", "Chat on WhatsApp")] if c.get("site_wa") else []
        return {"url": url, "title": c["name"], "markdown": md, "links": links}
    return {"url": url, "title": f"Test page {MARK}",
            "markdown": f"# Test page {MARK}\n\nThis is a mock page for {url}. No company information here.",
            "links": []}


def youtube(query: str, countries: list[str], keyword: str, n: int = 3) -> list[dict]:
    """假 YouTube 视频：频道简介 + 评论区（含一条买家求购评论）。"""
    pool = companies(countries, keyword)
    r = _rng("yt", query)
    out = []
    for k in range(n):
        c = pool[r.randrange(len(pool))]
        buyer = pool[r.randrange(len(pool))]
        out.append({
            "video_id": f"mock{k}{abs(hash(query)) % 1000}",
            "url": f"https://www.youtube.com/watch?v=mock{k}",
            "title": f"{keyword} installation review {MARK}",
            "channel_title": c["name"],
            "channel_description": _company_block(c),
            "description": f"Product review by {c['name']}. {MARK}",
            "comments": [
                {"author": "@random_viewer", "text": "Nice video!"},
                {"author": f"@{buyer['slug'][:15]}",
                 "text": f"We are looking to buy 200 units next quarter. Who is the manufacturer?\n"
                         f"{_company_block(buyer)}"},
            ],
        })
    return out


def is_mock_url(url: str) -> bool:
    return ".example" in (urllib.parse.urlparse(url).netloc or "")


# ------------------------------------------------------------------ 入职向导用的假官网（测试用）

SITE_HOST = "www.sunrise-energy.example"
_SITE = {
    "/": ("Sunrise Energy — Hybrid inverters & storage", "# Sunrise Energy Co., Ltd. (TEST DATA)\n\nManufacturer of hybrid "
          "solar inverters (3–12 kW) and LiFePO4 home batteries since 2009. Factory in Shenzhen, 35,000 m², 600 staff. "
          "OEM / ODM available.\n\n[Products](/en/products) · [About](/en/about) · [Cases](/en/cases) · "
          "[Dealer login](/dealer/login) · [Deutsch](/de/)"),
    "/en/products": ("Products", "# Products\n\n- [Hybrid inverter 5 kW](/en/products/hybrid-inverter-5kw)\n"
                     "- [LiFePO4 battery 10 kWh](/en/products/battery-lifepo4)\n- [Download catalogue (PDF)](/files/catalog-2026.pdf)"),
    "/en/products/hybrid-inverter-5kw": ("Hybrid inverter 5 kW", "# Hybrid inverter SR-H5K\n\nSingle-phase hybrid "
                                         "inverter, 5 kW, 2 MPPT, IP65, 10-year warranty. CE, IEC 62109, VDE-AR-N 4105 certified."),
    "/en/products/hybrid-inverter-5kw?color=black": ("Hybrid inverter 5 kW (black)", "# Hybrid inverter SR-H5K\n\n"
                                                     "Single-phase hybrid inverter, 5 kW, 2 MPPT, IP65, 10-year warranty. "
                                                     "CE, IEC 62109, VDE-AR-N 4105 certified. Colour: black."),
    "/en/products/battery-lifepo4": ("LiFePO4 battery 10 kWh", "# SR-B10 home battery\n\nLiFePO4, 10 kWh, 6000 cycles, "
                                     "stackable to 40 kWh. UN38.3, IEC 62619."),
    "/en/about": ("About us", "# About Sunrise Energy\n\nFounded 2009 in Shenzhen. 600 employees, 4 production lines, "
                  "annual capacity 300,000 inverters. ISO 9001 and ISO 14001 certified. Exporting to 40 countries; "
                  "strongest markets: Malaysia, Singapore, Australia."),
    "/en/cases": ("Cases", "# Customer cases\n\n- 2 MW rooftop programme with a distributor in Selangor, Malaysia (2025)\n"
                  "- 1,200 home storage systems for an installer network in Singapore"),
    "/en/contact": ("Contact", "# Contact\n\nsales@sunrise-energy.example · +86 755 0000 0000"),
    "/de/": ("Sunrise Energy (DE)", "# Sunrise Energy\n\nHybrid-Wechselrichter und Speicher."),
    "/dealer/login": ("Dealer login", "# Dealer portal\n\nPlease sign in to see dealer prices.\n\n<form>password</form>"),
}


def site_page(url: str) -> dict | None:
    u = urllib.parse.urlparse(url)
    if u.netloc != SITE_HOST:
        return None
    path = u.path or "/"
    if path == "/sitemap.xml":
        locs = "".join(f"<url><loc>https://{SITE_HOST}{p}</loc></url>" for p in _SITE if p != "/dealer/login")
        xml = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
        return {"url": url, "title": "sitemap", "markdown": "", "links": [], "raw": xml}
    key = path + (("?" + u.query) if u.query else "")
    if key not in _SITE:
        key = path
    if key not in _SITE:
        return {"url": url, "title": "404", "markdown": "", "links": [], "raw": ""}
    title, md = _SITE[key]
    links = [(urllib.parse.urljoin(url, m), t) for t, m in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", md)]
    raw = md + ('<input type="password" name="pw">' if "login" in path else "")
    return {"url": url, "title": title + " " + MARK, "markdown": md, "links": links, "raw": raw}


def make_pdf(text: str) -> bytes:
    text = text.replace("(", "[").replace(")", "]")
    stream = f"BT /F1 11 Tf 40 760 Td ({text[:900]}) Tj ET".encode("latin-1", "replace")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    return out


def pdf_bytes(url: str) -> bytes:
    return make_pdf("Sunrise Energy catalogue 2026 TEST DATA. SR-H5K hybrid inverter 5 kW. SR-H10K three-phase 10 kW. "
                    "SR-B10 LiFePO4 battery 10 kWh. MOQ 50 units. Lead time 25 days. Payment T/T 30% deposit.")

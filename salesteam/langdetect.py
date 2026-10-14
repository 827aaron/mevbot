"""轻量语言识别（v1 只做 中文 / 英文 / 马来语，PRD §1）。

纯规则、不联网、不调用大模型、没有第三方依赖（无许可证问题），每条消息微秒级：
1. 文字系统：汉字 → zh（夹杂英文也算中文）；假名 / 韩文 / 西里尔 / 阿拉伯 / 泰文 / 天城文 / 泰米尔 等 → other
2. 拉丁字母：按常用词判断英语 / 马来语（印尼语与马来语大量重合，按马来语处理）；
   德 / 波 / 西 / 法 / 意 / 葡 / 荷 / 越 等的常用词或特殊字母明显更多 → other
3. 太短或没有线索 → unknown（不丢弃，交给后面的关键词和模型）
"""
import re

LANGS = ("zh", "en", "ms")

_CJK = re.compile(r"[一-鿿㐀-䶿]")
_OTHER_SCRIPT = re.compile(r"[぀-ヿ가-힯Ѐ-ӿ؀-ۿ֐-׿฀-๿"
                           r"ऀ-ॿঀ-৿஀-௿Ͱ-Ͽက-႟ក-៿]")
_WORD = re.compile(r"[a-zA-ZÀ-ɏḀ-ỿ']+")
_OTHER_LETTERS = re.compile(r"[äöüßąćęłńśźżñçãõâêîôûăđơưáéíóúàèìòùýřšžčěů]")

EN = set("""the and for with we are is am looking need needs please price prices supplier suppliers anyone who can of to in
our you your this that have has any want wanted buy buying quote quotation units pcs from what where how much per
order orders send dm pm us it be will would could should hi hello thanks thank good morning does do help project
company distributor importer wholesale""".split())
MS = set("""saya kami kita anda awak nak mahu hendak beli cari harga boleh tak tidak dengan untuk yang ini itu ada dan di ke
dari sila terima kasih pembekal borong pemborong perlukan perlu sebut minta berapa kedai barang lagi juga sahaja saja
sudah dah akan bila mana siapa macam mcm dgn utk yg tq tolong encik tuan puan bos stok pengedar syarikat sdn bhd
jual pesanan hantar murah unit biji butuh mau supplier harga borongan sebutharga bagi""".split())
OTHER = set("""und der die das wir suchen ich nicht mit für ein eine oder auch bitte lieferant angebot
szukam jest nie dla się oraz dostawcy potrzebujemy proszę ofertę
los las para que una buscamos con por del proveedor unidades cotizar necesitamos
nous pour avec les des est une cherche fournisseur
il per che sono della cerco fornitore
com não uma procuro fornecedor
een het voor met zoeken leverancier
chúng tôi cần mua nhà cung cấp""".split())


def detect(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "unknown"
    cjk = len(_CJK.findall(t))
    if cjk >= 2:
        return "zh"
    other_script = len(_OTHER_SCRIPT.findall(t))
    words = [w.lower() for w in _WORD.findall(t)]
    latin_letters = sum(len(w) for w in words)
    if other_script and other_script >= latin_letters * 0.3:
        return "other"
    if not words:
        return "unknown"
    en = sum(w in EN for w in words)
    ms = sum(w in MS for w in words)
    ot = sum(w in OTHER for w in words) + len(_OTHER_LETTERS.findall(t.lower())) // 2
    if ot and ot > en + ms:
        return "other"
    if ms > en:
        return "ms"
    if en:
        return "en"
    if ms:
        return "ms"
    return "unknown"


def allowed(text: str) -> bool:
    """v1：只处理中文 / 英文 / 马来语；认不出（太短等）也放行，交给后面的规则和模型。"""
    return detect(text) != "other"


def reply_lang(text: str, default: str = "en") -> str:
    """第 3 步开发信 / 跟进的语言：按客户自己用的语言（中 / 英 / 马来），认不出用英语。"""
    lg = detect(text)
    return lg if lg in LANGS else default

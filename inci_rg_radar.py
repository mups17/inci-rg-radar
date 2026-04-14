# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         🏛️  İNCİ HOLDİNG — RESMİ GAZETE MEVZUAT RADARI  v10                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v10 — Tam Refactor:                                                        ║
║   🔧  Scraper: Retry + backoff, doğru encoding, URL filtresi düzeltildi     ║
║   🔧  Regex: Türkçe karakter sınıfı düzeltildi (ş-ö range bug giderildi)   ║
║   🔧  Scoring: Keyword/semantic denge yeniden kalibre edildi                 ║
║   🔧  Çoklu şirket: Bir karar birden fazla şirketi ilgilendirebilir         ║
║   🔧  Fulltext: Çekme ve kullanım limiti tutarlı hale getirildi             ║
║   🔧  HTML: Çoklu eşleşme desteği, iyileştirilmiş kart görünümü            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import subprocess, sys

def install():
    deps = {
        "beautifulsoup4":        "bs4",
        "requests":              "requests",
        "pandas":                "pandas",
        "lxml":                  "lxml",
        "sentence-transformers": "sentence_transformers",
        "torch":                 "torch",
    }
    for pkg, mod in deps.items():
        try:
            __import__(mod)
        except ImportError:
            print(f"📦 Kuruluyor: {pkg}")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])

install()

import os, re, logging, time, urllib.parse, json, html as html_mod
from pathlib  import Path
from datetime import datetime
from urllib.parse import urljoin
from functools import wraps

import requests
from bs4 import BeautifulSoup
import torch
from sentence_transformers import SentenceTransformer, util


# ═══════════════════════════════════════════════════════════════════════════════
#  3 KATMANLI FİLTRE SİSTEMİ (inci_filters entegrasyonu)
#  KATMAN 1: BLACKLIST — kesin eleme (~60-70%)
#  KATMAN 2: WHITELIST — kesin geçirme (düşük eşik)
#  KATMAN 3: NLP/HYBRID — sadece gri bölge için
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_pattern(pat):
    """Regex pattern'i temizle: yorum, boşluk, | etrafı."""
    cleaned = re.sub(r'#.*$', '', pat, flags=re.MULTILINE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    cleaned = re.sub(r'\s*\|\s*', '|', cleaned)
    return cleaned.strip('|')

_BLACKLIST = {
    "atama_personel": r"atama\s*karar[ıi]|münhal\s*kadro|görevlendirme\s*karar|terfii\s*karar|emeklilik\s*karar|kadro\s*ihdas|görevden\s*alma|bireysel\s*atama|tayin\s*karar|naklen\s*atama|asaleten\s*atama|açıktan\s*atama|sözleşmeli\s*personel\s*al|disiplin\s*karar|ihraç\s*karar|göreve\s*iade",
    "yuksek_makam": r"\bvali\b|\bkaymakam\b|\bhakim\b|\bsavc[ıi]\b|\brektör\b|büyükelçi\s*atama|müsteşar\s*atama|genel\s*müdür\s*atama|bakan\s*yardımcısı\s*atama|cumhurbaşkanl[ıi]ğ[ıi]\s*atama",
    "universite": r"üniversite|rektörlük\s*ilan|öğretim\s*üye\s*ilan|akademik\s*kadro|yök\s*karar|fakülte\s*dekan|enstitü\s*müdür|doçentlik\s*sınav|profesörlük\s*kadro|araştırma\s*görevlisi\s*ilan",
    "yargi": r"anayasa\s*mahkemesi|yargıtay|danıştay|uyuşmazlık\s*mahkemesi|sayıştay\s*karar|ceza\s*mahkemesi\s*karar|temyiz\s*karar",
    "ilanlar": r"yarg[ıi]\s*il[aâ]n|artırma\s*il[aâ]n|eksiltme\s*il[aâ]n|ihale\s*il[aâ]n|çeşitli\s*il[aâ]n|icra\s*il[aâ]n|noter\s*il[aâ]n|tasfiye\s*il[aâ]n|iflas\s*il[aâ]n|konkordato\s*il[aâ]n",
    "nufus_kisisel": r"gaiplik|vasi\s*atan|isim\s*değiş|boşanma\s*il[aâ]n|nüfus\s*(müdürlüğü|hizmet)|evlendirme\s*memur|vatandaşlık\s*karar",
    "secim": r"seçim\s*kurulu|seçim\s*karar|milletvekili\s*seçim",
    "kamulastirma": r"kamulaştırma\s*(bilgi|karar|bedel)|acele\s*kamulaştır|taşınmaz.*kamulaştır|taşınmaz.*tescil|\d+\s*kV|enerji\s*(nakil|iletim)\s*hat|trafo\s*merkez.*proje|boru\s*hattı\s*proje.*kamulaştır|doğal\s*gaz\s*boru\s*hattı.*kamulaştır",
    "askeri": r"harp\s*araç|silah\s*(ve|,)\s*mühimmat|askeri\s*(personel|teçhizat|bölge|yasak)|savunma\s*sanayi\s*müsteşar|genelkurmay|jandarma\s*genel|sahil\s*güvenlik\s*komutanlığı|istiklal\s*madalyası|şehit.*gazi.*mirasçı|kurtuluş\s*savaşı.*madalya",
    "uluslararasi": r"(hükümet|devlet).*arasında.*(anlaşma|sözleşme|mutabakat)|milletlerarası\s*andlaşma|anlaşma.*yürürlüğe\s*gir.*tarih.*tespit|bazı\s*anlaşmaların\s*yürürlüğe|mutabakat\s*zaptı|oecd.*mutabakat|ipard|kırsal\s*kalkınma\s*programı",
    "doviz_borc": r"döviz\s*kuru|devlet\s*iç\s*borç|hazine\s*bonosu\s*ihraç|tahvil\s*ihraç\s*koşul",
    "belediye": r"belediye\s*meclis\s*karar|büyükşehir\s*belediye.*(imar|karar)|il\s*özel\s*idare|imar\s*plan[ıi].*onay|nazım\s*imar|uygulama\s*imar|büyükşehir.*imar\s*yönetmeli",
    "tasinmaz": r"kadastro\s*(ilan|müdür|karar)|tapu\s*müdür|arazi\s*toplulaştır|orman\s*kadastro|taşınmaz\s*kültür\s*varlık|sit\s*alanı\s*ilan",
    "sektor_disi_kurum": r"meteoroloji\s*genel|diyanet\s*işleri|kızılay|afad\s*karar|vakıflar\s*genel\s*müdür|trt\s*genel\s*müdür|ptt\s*genel\s*müdür|orman\s*genel\s*müdür.*karar|maden\s*tetkik|devlet\s*su\s*işleri.*karar|karayolları\s*genel\s*müdür.*karar|kalkınma\s*ajans|sosyal\s*güvenlik\s*kurum.*karar|tüik\s*karar|istatistik\s*program|yazma\s*eserler\s*kurumu|ulusal\s*su\s*planı",
    "kamu_idari": r"personel\s*yönetmeliği.*yürürlükten|başbakanlık\s*(uzmanlığı|personeli)|mal[iî]\s*suçlar.*mücadele|disiplin\s*amirleri\s*yönetmeli|görevde\s*yükselme.*yönetmeli.*yürürlükten|döner\s*sermaye\s*işletme|kömür\s*dağıtım|ısınma\s*amaçlı",
    "ozellestirme": r"özelleştirme\s*idare|özelleştirme.*(taşınmaz|satış|devir|ihale)",
    "sulama_baraj": r"sulama[sş]?[ıi]?\s*(projesi|birliği|alanı|dilimli)|baraj\s*(projesi|inşaat)|gölet\s*projesi|dsi\s*proje|ulusal\s*su\s*planı",
    "bireysel_ithalat": r"ticari\s*ithalat\s*maksadı\s*dışında|kişisel\s*kullanım.*ithalat|yolcu\s*beraberi.*eşya",
    "saglik": r"sağlık\s*bakan.*ilan|hastane.*(karar|yapım|renovasyon)|tıbbi\s*cihaz\s*ilan|ilaç\s*fiyat|eczacılık|sağlık\s*personel\s*al|beşeri\s*tıbbi\s*ürün|sağlık\s*enstitü|devlet\s*hastanesi",
    "egitim": r"milli\s*eğitim\s*bakan.*karar|öğrenci\s*seçim|burs\s*(ilan|başvuru)|okul\s*müdür.*atama",
    "tarim_orman": r"tarım.*bakan.*il\s*müdür|orman.*bakan.*kadastro|bitki\s*sağlığı|veteriner|gıda\s*tarım.*il\s*müdür|hayvan\s*sağlığı|balıkçılık\s*ilan",
    "scraper_noise": r"^MADDE\s*\d+|hükümlerini.*bakan[ıi]\s*yürüt|^Yürürlük$|^Yürütme$|ekleri\s*için\s*t[ıi]kla|buraya\s*t[ıi]kla|pdf\s*(g[oö]r[uü]nt[uü]le|indir)|^\s*\d+\s*$|sayfan[ıi]n\s*ba[şs][ıi]|ana\s*sayfa|geri\s*d[oö]n",
}

_WHITELIST = {
    "gumruk_dis_ticaret": r"ithalat\s*rejim|ihracat\s*rejim|gümrük\s*(tarife|vergisi|kanun|yönetmeli)|dahilde\s*işleme|hariçte\s*işleme|işleme\s*izin\s*belge|dış\s*ticaret\s*tebliğ|ticaret\s*bakanlığı\s*tebliğ|anti\s*damping|korunma\s*önlemi|ithalat\s*tebliğ|ihracat\s*tebliğ|serbest\s*bölge.*(yönetmeli|tebliğ|uygulama|faaliyet|işlem|mevzuat)|serbest\s*ticaret\s*anlaşma",
    "enerji_piyasasi": r"epdk|enerji\s*piyasası|elektrik\s*piyasası|yenilenebilir\s*enerji.*(karar|yönetmeli|teşvik)|doğal\s*gaz\s*piyasası|petrol\s*piyasası|lpg\s*piyasası|şarj\s*(hizmeti|istasyonu)|lisanssız\s*elektrik",
    "cevre_emisyon": r"çevresel\s*etki\s*değerlendirme|çed\s*(rapor|yönetmeli)|tehlikeli\s*atık|atık\s*yönetim|sera\s*gazı\s*(emisyon|izleme)|karbon\s*(vergisi|ticareti|sınır)|çevre\s*izni|florlu\s*sera\s*gaz|soğutucu\s*akışkan|soğutma.*(yönetmeli|tebliğ)",
    "otomotiv_sanayi": r"otomotiv\s*(sanayi|teşvik|standart)|motorlu\s*taşıt|araç\s*üretim|demir\s*çelik.*(ithalat|ihracat|tebliğ)|çelik.*(ithalat|ihracat|gümrük|anti\s*damping)|alüminyum.*(ithalat|ihracat|gümrük|anti\s*damping)|sanayi\s*sicil|imalat\s*sanayi",
    "rekabet_sermaye": r"rekabet\s*kurul|spk\s*tebliğ|sermaye\s*piyasası\s*(kanun|tebliğ)|halka\s*arz|birleşme.*devralma",
    "vergi_tesvik": r"kurumlar\s*vergisi|kdv\s*(istisna|oran|tebliğ)|vergi\s*muafiyeti|yatırım\s*teşvik|teşvik\s*belgesi|ar-?ge\s*(teşvik|destek|indirim)|teknopark|teknoloji\s*geliştirme\s*bölge|tasarım\s*merkezi",
    "urun_guvenlik": r"ce\s*işareti|ürün\s*güvenliği|teknik\s*düzenleme|uygunluk\s*değerlendirme|enerji\s*etiketi|ekotasarım|beyaz\s*eşya",
    "lojistik": r"karayolu\s*taşıma|uluslararası\s*nakliye|liman.*(yönetmeli|tebliğ|karar)|denizcilik.*(yönetmeli|tebliğ)|antrepo|gümrük\s*müşavir|taşımacılık.*(yönetmeli|tebliğ)",
    "batarya": r"akümülatör|batarya.*(yönetmeli|tebliğ|standart)|lityum.*(yönetmeli|tebliğ|ithalat)|pil\s*atık|enerji\s*depolama",
}

# Compile
_BL_COMPILED = {cat: re.compile(_clean_pattern(pat), re.IGNORECASE) for cat, pat in _BLACKLIST.items()}
_WL_COMPILED = {cat: re.compile(_clean_pattern(pat), re.IGNORECASE) for cat, pat in _WHITELIST.items()}

# Zehirli keyword'ler — eşleşse bile hybrid skora katkısı azaltılır
TOXIC_KEYWORDS = {"anonim şirket", "holding", "ticari işletme", "ticaret kanunu", "dış ticaret", "gümrük", "ithalatçı", "ihracatçı"}

def filter_item(title: str, fulltext: str = "") -> tuple:
    """
    3 katmanlı filtre. Returns: (action, reason, category)
    action: "reject" | "accept" | "analyze"
    """
    # KATMAN 1: Blacklist
    for cat, pat in _BL_COMPILED.items():
        if pat.search(title):
            return ("reject", f"Blacklist: {cat}", cat)
    if fulltext:
        snippet = fulltext[:500]
        for cat in ("kamulastirma", "askeri", "scraper_noise"):
            if _BL_COMPILED[cat].search(snippet):
                return ("reject", f"Blacklist: {cat} (fulltext)", cat)

    # KATMAN 2: Whitelist
    search_text = title + (" " + fulltext[:800] if fulltext else "")
    for cat, pat in _WL_COMPILED.items():
        if pat.search(search_text):
            return ("accept", f"Whitelist: {cat}", cat)

    # KATMAN 3: Gri bölge
    return ("analyze", "Gri bölge", "")

# ═══════════════════════════════════════════════════════════════════════════════
#  DİZİN AYARLARI
# ═══════════════════════════════════════════════════════════════════════════════
_default_dir = '/content' if os.path.isdir('/content') else os.path.join(Path.home(), 'rg_radar_data')
BASE_DIR    = Path(os.getenv("BULLETIN_DATA_DIR", _default_dir))
EXPORTS_DIR = BASE_DIR / "exports"
LOGS_DIR    = BASE_DIR / "logs"
for d in (EXPORTS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  KONFIGÜRASYON — v10
# ═══════════════════════════════════════════════════════════════════════════════
HYBRID_THRESHOLD     = 38.0    # v10.1: 28→38 (false positive azaltmak için)
SEMANTIC_FALLBACK    = 72.0    # v10.1: 60→72 (semantic-only çok nadir geçmeli)
SEM_FALLBACK_MULT    = 0.35   # v10.1: semantic fallback çarpanı (sem*0.35, 72*0.35=25 → eşik altı!)
MIN_KW_HITS          = 1
MAX_FULLTEXT_CHARS   = 2500
NEG_KW_PENALTY       = 10.0   # v10.1: 8→10 (daha sert ceza)
MULTI_COMPANY_THRESH = 0.80   # v10.1: 0.75→0.80 (çoklu eşleşme daha sıkı)
KW_WEIGHT            = 0.60   # v10.1: keyword ağırlığı artırıldı
SEM_WEIGHT           = 0.40   # v10.1: semantic ağırlık düşürüldü
MIN_KW_FOR_PASS      = 1      # v10.1: en az 1 keyword olmalı (semantic-only pratik olarak geçmez)

# Retry ayarları
MAX_RETRIES     = 2       # 3→2: daha hızlı başarısızlık tespiti
RETRY_BACKOFF   = 1.0     # 1.5→1.0: daha kısa bekleme
REQUEST_TIMEOUT = 12      # 15→12: GitHub Actions datacenter hızlı

# ═══════════════════════════════════════════════════════════════════════════════
#  TÜRKÇE YARDIMCI SABİTLER
# ═══════════════════════════════════════════════════════════════════════════════
_AY_TR = {1:"ocak",2:"subat",3:"mart",4:"nisan",5:"mayis",6:"haziran",
          7:"temmuz",8:"agustos",9:"eylul",10:"ekim",11:"kasim",12:"aralik"}
# Türkçe alfanümerik karakter sınıfı — regex negative lookbehind/lookahead için
_TR_ALPHA = r'a-zA-ZçÇğĞıİöÖşŞüÜâÂîÎûÛ0-9'
_TR_WORD_PRE  = rf'(?<![{_TR_ALPHA}])'   # Öncesinde Türkçe alfanümerik yok
_TR_WORD_POST = rf'(?![{_TR_ALPHA}])'    # Sonrasında Türkçe alfanümerik yok


# ═══════════════════════════════════════════════════════════════════════════════
#  ŞİRKET PROFİLLERİ
# ═══════════════════════════════════════════════════════════════════════════════
INCI_COMPANIES = {
    "Maxion İnci & Jantaş": {
        "short":  "MİJ",
        "sector": "Otomotiv & Ağır Sanayi",
        "color":  "#1F4E79",
        "badge":  "🚗",
        "desc": (
            "Otomotiv sanayi teşvikleri, demir-çelik ve alüminyum ithalat kararları, "
            "sanayi bölgeleri, üretim standartları, gümrük vergileri, araç üretimi, "
            "jant, tekerlek, döküm, hadde, imalat."
        ),
        "keywords": [
            "motorlu taşıt", "taşıt araçları", "araç üretimi", "yan sanayi",
            "karoseri", "otomotiv sanayi", "otomotiv teşvik", "otomotiv sanayii",
            "lastik", "jant", "tekerlek imalat", "jant üretimi",
            "alüminyum", "alüminyum alaşım", "alüminyum ithalat",
            "demir çelik", "çelik ithalat", "hadde", "döküm",
            "çelik boru", "çelik profil", "demir dışı metal",
            "sanayi sicil", "imalat sanayi", "organize sanayi",
            "sanayi bölgesi", "üretim tesisi", "fabrika kurulumu",
            "gümrük tarifesi", "ithalat kotası", "ihracat teşvik",
            "dahilde işleme", "hariçte işleme", "yurt içi satış teslim",
            "d1 belgesi", "d3 belgesi", "h1 belgesi",
            "firma talebine istinaden", "işleme izin belgesi",
        ],
        "negative_keywords": [
            "akümülatör", "batarya", "lityum", "enerji depolama",
            "soğutma", "minibar", "girişim sermayesi", "epdk",
            "kamulaştırma", "harp", "silah", "mühimmat",
            "meteoroloji", "sulama projesi", "enerji nakil",
        ],
    },
    "İnci GS Yuasa & Vflow Tech": {
        "short":  "IGY",
        "sector": "Enerji & Çevre",
        "color":  "#1E6B3C",
        "badge":  "⚡",
        "desc": (
            "EPDK kararları, elektrik piyasası, yenilenebilir enerji, "
            "ÇED raporları, tehlikeli atık yönetimi, batarya, akümülatör, "
            "enerji depolama, güneş, rüzgar, hidrojen."
        ),
        "keywords": [
            "epdk", "elektrik piyasası", "elektrik üretim", "lisanssız elektrik",
            "enerji depolama", "şarj istasyonu", "elektrik dağıtım",
            "yenilenebilir enerji", "güneş enerjisi", "rüzgar enerjisi",
            "enerji verimliliği", "elektrik iletim", "doğal gaz dağıtım",
            "petrol piyasası", "lpg piyasası",
            "çevresel etki değerlendirmesi", "çed", "tehlikeli atık",
            "atık yönetimi", "çevre izni", "emisyon", "karbon",
            "sera gazı", "çevre mevzuatı",
            "akümülatör", "batarya", "lityum", "akü", "pil",
            "fuel cell", "hidrojen", "enerji hücresi",
            "lityum madeni", "kobalt", "nikel maden",
        ],
        "negative_keywords": [
            "jant", "tekerlek", "otomotiv", "karoseri",
            "antrepo", "nakliye", "soğutma", "minibar",
            "rekabet kurulu", "spk tebliğ",
            "kamulaştırma", "harp", "silah", "mühimmat",
            "meteoroloji", "sulama projesi",
        ],
    },
    "Yusen İnci Lojistik": {
        "short":  "YİL",
        "sector": "Gümrük & Lojistik",
        "color":  "#7B3F00",
        "badge":  "🚚",
        "desc": (
            "Gümrük yönetmelikleri, antrepo, karayolu taşıma, uluslararası nakliye, "
            "liman ve denizcilik, dış ticaret tebliğleri, dahilde işleme, "
            "hariçte işleme, ithalat, ihracat, transit."
        ),
        "keywords": [
            "dahilde işleme", "hariçte işleme", "işleme izin belgesi",
            "d1 belgesi", "d3 belgesi", "h1 belgesi",
            "yurt içi satış teslim belgesi", "firma talebine istinaden iptal",
            "gümrük tarife", "gümrük kanunu", "gümrük yönetmeliği",
            "gümrük vergisi", "gümrük beyanname",
            "antrepo", "gümrük müşavirliği", "transit ticaret",
            "ithalat rejimi", "ihracat rejimi", "serbest bölge",
            "karayolu taşıma", "taşımacılık", "uluslararası nakliye",
            "taşıma belgesi", "nakliye", "freight", "lojistik",
            "liman", "denizcilik", "konteyner", "navlun", "ardiye",
            "dış ticaret tebliğ", "ticaret bakanlığı tebliğ", "ihracatçı birliği",
            "ithalatçı birliği", "döviz kazandırıcı",
            "ticaret politikası", "miktar kısıtlaması",
        ],
        "negative_keywords": [
            "jant", "tekerlek", "otomotiv",
            "akümülatör", "batarya", "epdk",
            "soğutma", "minibar", "rekabet kurulu",
            "harp", "silah", "mühimmat", "askeri", "savunma sanayi",
            "kamulaştırma", "meteoroloji", "sulama projesi",
        ],
    },
    "ISM Minibar & Starcool": {
        "short":  "IMS",
        "sector": "Soğutma & Ticari Ekipman",
        "color":  "#6B2D8B",
        "badge":  "🧊",
        "desc": (
            "Beyaz eşya ve soğutma sistemleri enerji verimliliği, "
            "florlu sera gazları, ticari soğutma, klima, buzdolabı, "
            "enerji etiketi, ce işareti, soğutucu akışkan."
        ),
        "keywords": [
            "soğutma sistemi", "soğutucu akışkan", "florlu sera gazı",
            "hfc", "r410a", "soğutucu madde", "enerji etiketi soğutma",
            "beyaz eşya", "enerji verimliliği sınıfı",
            "buzdolabı", "dondurucu", "klima", "iklimlendirme",
            "ticari soğutma", "minibar", "kompresör",
            "ce işareti", "ürün güvenliği", "teknik düzenleme",
            "uygunluk değerlendirme", "standart zorunlu",
            "enerji performans", "ekotasarım",
            "soğuk zincir", "soğuk hava deposu", "frigorifik",
        ],
        "negative_keywords": [
            "jant", "tekerlek", "otomotiv",
            "akümülatör", "batarya", "epdk",
            "antrepo", "lojistik", "nakliye",
            "rekabet kurulu", "spk",
            "kamulaştırma", "harp", "silah", "mühimmat",
            "meteoroloji", "sulama projesi", "enerji nakil",
            "döner sermaye",
        ],
    },
    "Vinci B.V. & Holding (Genel)": {
        "short":  "VBH",
        "sector": "Kurumsal Hukuk & Yatırım",
        "color":  "#8B1A1A",
        "badge":  "🏛️",
        "desc": (
            "Kurumlar vergisi, rekabet kurumu, ar-ge teşvik, teknopark, "
            "yabancı sermaye, GSYF, ticaret hukuku, şirket birleşme, "
            "spk, sermaye piyasası, teşvik belgesi."
        ),
        "keywords": [
            "rekabet kurulu", "rekabet ihlali", "birleşme izni",
            "hakim durum", "pazar payı",
            "kurumlar vergisi", "kdv istisnası", "vergi muafiyeti",
            "transfer fiyatlandırması", "teşvik belgesi", "yatırım teşvik",
            "yatırım indirimi", "vergi avantajı",
            "ar-ge teşvik", "teknopark", "teknoloji geliştirme bölge",
            "inovasyon merkezi", "tasarım merkezi",
            "girişim sermayesi yatırım fonu", "gsyf", "spk tebliğ",
            "yabancı sermaye", "doğrudan yabancı yatırım",
            "sermaye piyasası kanun", "halka arz",
            "şirket birleşmesi", "şirket bölünmesi", "ticaret sicil gazetesi",
            "sermaye artırım kararı",
        ],
        "negative_keywords": [
            "jant", "tekerlek", "otomotiv",
            "akümülatör", "batarya", "epdk",
            "antrepo", "lojistik", "soğutma", "minibar",
            "kamulaştırma", "meteoroloji", "sulama projesi",
            "harp", "silah", "mühimmat",
            "enerji iletim hattı", "enerji nakil",
            "kalkınma ajansı",
        ],
    },
}

# v10.1: Global negatif — herhangi bir şirkete eşleşmeyi engelleyecek terimler
GLOBAL_NEGATIVES = re.compile(
    r"(kamulaştırma\s*(bilgi|karar)|harp\s*araç|silah\s*mühimmat|"
    r"askeri\s*teçhizat|meteoroloji|sulama\s*projesi|"
    r"enerji\s*nakil\s*hattı|enerji\s*iletim\s*hattı|"
    r"taşınmaz.*kamulaştır|baraj\s*projesi|"
    r"^madde\s*\d+|hükümlerini.*bakan[ıi]\s*yürüt)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  GÜRÜLTÜ FİLTRELERİ
# ═══════════════════════════════════════════════════════════════════════════════
# v10.2: Sadece navigasyon öğeleri — içerik filtreleme 3 katmanlı sisteme bırakıldı
NAV_SKIP_RE = re.compile(
    r"(ekleri\s*(için)?\s*t[iı]klay|buraya\s*t[iı]klay|pdf\s*(g[oö]r[uü]nt[uü]le|indir)|"
    r"^\s*\d+\s*$|sayfan[iı]n\s*ba[şs][iı]|ana\s*sayfa|geri\s*d[oö]n)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════
def setup_logging():
    lf = LOGS_DIR / f"rg_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(lf, encoding="utf-8"),
        ],
    )
    return logging.getLogger("rg_radar")


# ═══════════════════════════════════════════════════════════════════════════════
#  RETRY DECORATÖRLERİ
# ═══════════════════════════════════════════════════════════════════════════════
def retry_request(max_retries=MAX_RETRIES, backoff=RETRY_BACKOFF):
    """HTTP istekleri için retry + exponential backoff decorator."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    if result is not None:
                        return result
                except requests.exceptions.Timeout as e:
                    last_err = e
                    wait = backoff * (2 ** (attempt - 1))
                    logging.getLogger("rg_radar").warning(
                        f"Timeout (deneme {attempt}/{max_retries}), {wait:.1f}s bekleniyor..."
                    )
                    time.sleep(wait)
                except requests.exceptions.ConnectionError as e:
                    last_err = e
                    wait = backoff * (2 ** (attempt - 1))
                    logging.getLogger("rg_radar").warning(
                        f"Bağlantı hatası (deneme {attempt}/{max_retries}), {wait:.1f}s bekleniyor..."
                    )
                    time.sleep(wait)
                except Exception as e:
                    last_err = e
                    break  # Bilinmeyen hata, retry etme
            if last_err:
                logging.getLogger("rg_radar").error(f"İstek başarısız: {last_err}")
            return None
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
#  SCRAPER — v10: Retry, daha iyi encoding, URL filtresi düzeltildi
# ═══════════════════════════════════════════════════════════════════════════════
class ResmiGazeteScraper:
    BASE = "https://www.resmigazete.gov.tr"

    def __init__(self, logger):
        self.logger  = logger
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection":      "keep-alive",
            "Cache-Control":   "no-cache",
        })
        # İstatistik
        self._stats = {"fetched": 0, "failed": 0, "retried": 0}

    # ── Encoding yardımcıları ─────────────────────────────────────────────
    @staticmethod
    def _safe_decode(response) -> str:
        """Response'u doğru encoding ile decode et."""
        # Önce content-type header'dan dene
        ct = response.headers.get("Content-Type", "")
        if "charset=" in ct:
            charset = ct.split("charset=")[-1].strip().split(";")[0]
            try:
                return response.content.decode(charset)
            except (UnicodeDecodeError, LookupError):
                pass
        # Yaygın Türkçe encoding'leri dene
        for enc in ("utf-8", "iso-8859-9", "windows-1254", "latin-1"):
            try:
                decoded = response.content.decode(enc)
                # Türkçe karakterler varsa doğru encoding bulunmuş demektir
                if any(c in decoded for c in "çğışöüÇĞİŞÖÜ"):
                    return decoded
            except (UnicodeDecodeError, LookupError):
                continue
        return response.text  # Son çare: requests'in kendi tahmini

    @staticmethod
    def _fix_enc(text: str) -> str:
        if not text:
            return text
        for enc in ("utf-8", "iso-8859-9", "windows-1254"):
            try:
                return text.encode("latin-1").decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
        return text

    @staticmethod
    def _abs(href: str, page_url: str) -> str:
        if href.startswith("http"):
            return href
        return urljoin(page_url, href)

    # ── HTTP: Ana istek + proxy fallback ──────────────────────────────────
    def _get_html(self, url: str, timeout: int = REQUEST_TIMEOUT) -> str | None:
        """URL'den HTML çek. Doğrudan erişim başarısız olursa proxy dene."""

        # 1. Doğrudan erişim (retry ile)
        html = self._try_direct(url, timeout)
        if html:
            return html

        # 2. Proxy fallback (her proxy 1 kez)
        html = self._try_proxies(url)
        if html:
            return html

        self._stats["failed"] += 1
        self.logger.warning(f"❌ Erişilemedi: {url}")
        return None

    def _try_direct(self, url: str, timeout: int) -> str | None:
        """Doğrudan erişim, retry destekli."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code == 200 and len(r.content) > 200:
                    self._stats["fetched"] += 1
                    return self._safe_decode(r)
                elif r.status_code == 429:
                    # Rate limited — bekle ve tekrar dene
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    self.logger.warning(f"⏳ Rate limit (429), {wait:.0f}s bekleniyor...")
                    self._stats["retried"] += 1
                    time.sleep(wait)
                    continue
                elif r.status_code >= 500:
                    # Sunucu hatası — retry
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    self._stats["retried"] += 1
                    time.sleep(wait)
                    continue
                else:
                    # 404, 403 vs — retry etme
                    break
            except requests.exceptions.Timeout:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                self._stats["retried"] += 1
                self.logger.warning(f"⏳ Timeout (deneme {attempt}), {wait:.0f}s bekleniyor...")
                time.sleep(wait)
            except requests.exceptions.ConnectionError:
                wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                self._stats["retried"] += 1
                time.sleep(wait)
            except Exception as e:
                self.logger.debug(f"İstek hatası: {e}")
                break
        return None

    def _try_proxies(self, url: str) -> str | None:
        """CORS proxy fallback — her biri 1 deneme."""
        enc = urllib.parse.quote(url, safe="")
        proxies = [
            (f"https://corsproxy.io/?{url}",                    None),
            (f"https://api.allorigins.win/get?url={enc}",       "contents"),
            (f"https://api.codetabs.com/v1/proxy?quest={url}",  None),
        ]
        for purl, jkey in proxies:
            try:
                r = self.session.get(purl, timeout=20)
                if r.status_code != 200:
                    continue
                if jkey:
                    raw = r.json().get(jkey, "")
                    html_text = self._fix_enc(raw)
                else:
                    html_text = self._safe_decode(r)
                if html_text and len(html_text) > 200:
                    self._stats["fetched"] += 1
                    return html_text
            except Exception:
                continue
        return None

    # ── İlan mirror fallback ──────────────────────────────────────────────
    def _try_ilan_mirror(self, cat_url: str, today) -> str | None:
        """RG ilan sayfası timeout verdiğinde proxy + farklı timing ile tekrar dene."""
        # Proxy üzerinden dene (RG direkt engelliyor olabilir)
        html = self._try_proxies(cat_url)
        if html:
            print(f"      📡 İlan proxy'den çekildi")
            return html

        # Son çare: 30sn timeout ile tek istek daha
        try:
            time.sleep(3)  # Rate limit'ten kaçınmak için bekle
            r = self.session.get(cat_url, timeout=30)
            if r.status_code == 200 and len(r.content) > 200:
                self._stats["fetched"] += 1
                print(f"      📡 İlan uzun timeout ile çekildi")
                return self._safe_decode(r)
        except Exception:
            pass
        return None

    # ── Fihrist (ana sayfa) ───────────────────────────────────────────────
    def _fetch_index(self, today):
        ds  = today.strftime("%Y%m%d")
        url = f"{self.BASE}/eskiler/{today.year}/{today.strftime('%m')}/{ds}.htm"
        self.logger.info(f"📄 Fihrist: {url}")
        print(f"🌐 Fihrist çekiliyor: {url}")
        html_text = self._get_html(url)
        if not html_text:
            return [], url

        soup  = BeautifulSoup(html_text, "html.parser")
        links, seen = [], set()
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href.endswith((".htm", ".pdf")) or len(title) < 5:
                continue
            full = self._abs(href, url)
            if full in seen or full == url:
                continue
            seen.add(full)
            links.append((title, full))

        self.logger.info(f"   └─ {len(links)} link bulundu.")
        return links, url

    # ── Fulltext çekme ────────────────────────────────────────────────────
    def _extract_fulltext(self, url: str) -> str:
        if url.endswith(".pdf"):
            return ""
        html_text = self._get_html(url, timeout=20)
        if not html_text:
            return ""
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:MAX_FULLTEXT_CHARS]  # Tutarlı limit

    # ── Kategori sayfası ──────────────────────────────────────────────────
    MAX_LINKS_PER_CAT = 25  # v10: daha fazla link

    def _is_valid_item_url(self, href: str, page_url: str, today) -> bool:
        """v10: URL filtresini düzelttik — daha kapsayıcı."""
        full = self._abs(href, page_url)
        if full == page_url:
            return False
        # Resmi gazete domainindeyse veya eskiler dizinindeyse kabul et
        if "resmigazete.gov.tr" in full:
            return True
        # Dışarıya çıkan linkler genellikle ilgisiz
        return False

    def _fetch_category(self, cat_title: str, cat_url: str, today) -> list:
        items = []
        if cat_url.endswith(".pdf"):
            ft = self._extract_fulltext(cat_url)
            return [self._make(cat_title, cat_url, today, cat_title, ft)]

        # İlan sayfaları için daha uzun timeout
        is_ilan = "/ilanlar/" in cat_url
        tout = 25 if is_ilan else REQUEST_TIMEOUT

        html_text = self._get_html(cat_url, timeout=tout)

        # Fallback: RG timeout verdiyse haber mirror'dan dene
        if not html_text and is_ilan:
            html_text = self._try_ilan_mirror(cat_url, today)

        if not html_text:
            # İçerik çekilemedi — bunu raporda belirt
            ft = f"[TIMEOUT] {cat_url} sayfasına erişilemedi, içerik kontrol edilemedi."
            return [self._make(cat_title, cat_url, today, cat_title, ft)]

        soup = BeautifulSoup(html_text, "html.parser")
        candidates = []
        seen = set()

        for a in soup.find_all("a", href=True):
            t    = a.get_text(strip=True)
            href = a["href"]
            if not href.endswith((".htm", ".pdf")) or len(t) < 8:
                continue
            if NAV_SKIP_RE.search(t):
                continue
            full = self._abs(href, cat_url)
            if full in seen:
                continue
            # v10: Düzeltilmiş URL filtresi
            if not self._is_valid_item_url(href, cat_url, today):
                continue
            seen.add(full)
            candidates.append((t, full))

        if candidates:
            limited = candidates[:self.MAX_LINKS_PER_CAT]
            print(f"      └─ {len(candidates)} link, {len(limited)} işleniyor...")
            for t, full in limited:
                ft = self._extract_fulltext(full)
                items.append(self._make(t, full, today, cat_title, ft))
                time.sleep(0.15)

        # Fallback: sayfada link yoksa içerikten çıkart
        if not items:
            for tag in soup.find_all(["p", "li", "td", "div"]):
                t = re.sub(r"^[—–\-]+\s*", "", tag.get_text(strip=True))
                if len(t) >= 12 and not NAV_SKIP_RE.search(t):
                    items.append(self._make(t, cat_url, today, cat_title, ""))

        if not items:
            ft = self._extract_fulltext(cat_url)
            items.append(self._make(cat_title, cat_url, today, cat_title, ft))

        return items

    @staticmethod
    def _make(title, url, today, category, fulltext=""):
        title = re.sub(r"\s+", " ", re.sub(r"^[—–\-]+\s*", "", title)).strip()
        return {
            "title":      title,
            "fulltext":   fulltext,
            "category":   category,
            "url":        url,
            "source":     "T.C. Resmi Gazete",
            "date":       today.strftime("%Y-%m-%d"),
            "scraped_at": datetime.now().isoformat(),
        }

    # ── RSS fallback ─────────────────────────────────────────────────────
    def _fetch_rss(self, today):
        feeds = [
            "https://www.resmigazete.gov.tr/rss/anasayfa.xml",
            "https://www.resmigazete.gov.tr/rss/y%C3%BCr%C3%BCtmeveidare.xml",
            "https://www.resmigazete.gov.tr/rss/tebligler.xml",
        ]
        items = []
        for url in feeds:
            try:
                r = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, "xml")
                for e in soup.find_all(["item", "entry"]):
                    t = e.find("title")
                    l = e.find(["link", "guid"])
                    title = t.get_text(strip=True) if t else ""
                    link  = l.get_text(strip=True) if l else ""
                    if len(title) >= 10 and link:
                        ft = self._extract_fulltext(link)
                        items.append(self._make(title, link, today, "RSS", ft))
            except Exception as err:
                self.logger.warning(f"RSS hatası: {err}")
        return list({i["url"]: i for i in items}.values())

    # ── Ana akış ─────────────────────────────────────────────────────────
    def fetch_today(self):
        today = datetime.now()
        cat_links, _ = self._fetch_index(today)

        if not cat_links:
            self.logger.info("📡 RSS fallback...")
            print("📡 Fihrist boş — RSS fallback deneniyor...")
            return self._fetch_rss(today)

        all_items, skipped = [], 0
        total_cats = len(cat_links)
        for i, (cat_title, cat_url) in enumerate(cat_links, start=1):
            # Sadece navigasyon öğelerini atla
            if NAV_SKIP_RE.search(cat_title) or len(cat_title.strip()) < 5:
                skipped += 1
                continue
            # Merkez Bankası döviz kuru tablosu (günlük veri, mevzuat değil)
            if re.search(r"merkez\s*bankasınca\s*belirlenen|günlük\s*değerleri", cat_title, re.IGNORECASE):
                skipped += 1
                print(f"   [{i}/{total_cats}] ⏭️  ATLANDI: {cat_title[:55]}")
                continue

            # ── BAŞLIK ÖN-FİLTRE: Sadece içeriğe bakılmadan kesin elenebilecek kategoriler ──
            # İlanlar, tebliğler, yönetmelikler ASLA atlanmaz — içlerinde ilgili karar olabilir
            _SAFE_SKIP_CATS = {
                "universite",       # Üniversite yönetmeliği asla İnci'yi ilgilendirmez
                "yargi",            # AYM, Yargıtay, Danıştay kararları
                "doviz_borc",       # Döviz kuru tabloları
                "scraper_noise",    # MADDE xx hükümlerini...
                "nufus_kisisel",    # Gaiplik, boşanma, nüfus
                "secim",            # Seçim kurulu kararları
            }
            pre_action, pre_reason, pre_cat = filter_item(cat_title, "")
            if pre_action == "reject" and pre_cat in _SAFE_SKIP_CATS:
                # %100 ilgisiz — HTTP istek yapma, direkt kaydet
                all_items.append(self._make(cat_title, cat_url, today, cat_title, ""))
                skipped += 1
                print(f"   [{i}/{total_cats}] ❌ BL: {cat_title[:55]}")
                continue

            # Geri kalan her şey (ilanlar, tebliğler, kararlar, yönetmelikler) → İÇERİĞİNİ ÇEK
            self.logger.info(f"   ↳ Giriliyor: {cat_title[:60]}")
            print(f"   [{i}/{total_cats}] ✅ Taranıyor: {cat_title[:55]}")
            all_items.extend(self._fetch_category(cat_title, cat_url, today))
            time.sleep(0.2)

        unique = list({i["url"]: i for i in all_items}.values())
        self.logger.info(
            f"✅ {len(unique)} benzersiz karar ({len(cat_links)} link, {skipped} atlanan) "
            f"| HTTP: {self._stats['fetched']} OK, {self._stats['failed']} fail, "
            f"{self._stats['retried']} retry"
        )
        print(
            f"\n📊 Scraper istatistik: {self._stats['fetched']} başarılı, "
            f"{self._stats['failed']} başarısız, {self._stats['retried']} yeniden deneme"
        )
        return unique


# ═══════════════════════════════════════════════════════════════════════════════
#  AI ANALİZ MOTORU — v10: Düzeltilmiş regex, çoklu şirket, dengeli skor
# ═══════════════════════════════════════════════════════════════════════════════
class RG_AIEngine:
    def __init__(self, logger):
        self.logger = logger
        print("🧠 AI modeli yükleniyor...")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"   └─ Cihaz: {self.device.upper()}")

        try:
            self.model = SentenceTransformer(
                "emrecan/bert-base-turkish-cased-mean-nli-stsb-tr",
                device=self.device
            )
            print("   ✅ Türkçe NLI modeli yüklendi.")
        except Exception as e:
            print(f"   ⚠️  Fallback modele geçiliyor... ({e})")
            self.model = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2",
                device=self.device
            )
            print("   ✅ Fallback model yüklendi.")

        self.company_names = list(INCI_COMPANIES.keys())
        descs   = [INCI_COMPANIES[c]["desc"]               for c in self.company_names]
        sectors = [INCI_COMPANIES[c]["sector"]              for c in self.company_names]
        kws     = [" ".join(INCI_COMPANIES[c]["keywords"])  for c in self.company_names]

        self.emb_d = self.model.encode(descs,   convert_to_tensor=True)
        self.emb_s = self.model.encode(sectors, convert_to_tensor=True)
        self.emb_k = self.model.encode(kws,     convert_to_tensor=True)

        neg = (
            "Akademik kadro ilanı öğretim üyesi üniversite rektörlük hakim savcı "
            "kaymakam vali atama bireysel icra iflas boşanma gaiplik vasi "
            "isim değişikliği dernek vakıf YÖK yargıtay noter seçim burs."
        )
        self.emb_n = self.model.encode([neg], convert_to_tensor=True)

    def _kw_match(self, kw: str, text: str) -> bool:
        """v10: Düzeltilmiş Türkçe karakter sınıfı — ş-ö range bug giderildi."""
        kw_l = kw.lower()
        if " " in kw_l:
            # Çok kelimeli: düz substring arama yeterli
            return kw_l in text
        else:
            # Tek kelime: Türkçe-uyumlu word boundary
            pattern = rf'{_TR_WORD_PRE}{re.escape(kw_l)}{_TR_WORD_POST}'
            return bool(re.search(pattern, text, re.IGNORECASE))

    def _score_company(self, search_text: str, comp_name: str, comp_data: dict,
                       semantic_score: float) -> dict:
        """v10.1: Daha sıkı scoring — false positive azaltma."""
        kw_hits = [kw for kw in comp_data["keywords"]
                   if self._kw_match(kw, search_text)]

        # Keyword skoru: azalan verim
        # 1 hit = 25, 2 = 45, 3 = 60, 4+ = 72+
        if kw_hits:
            kw_score = min(100.0, 25.0 * (len(kw_hits) ** 0.65))
        else:
            kw_score = 0.0

        # Hybrid hesapla
        if kw_hits:
            hybrid = (semantic_score * SEM_WEIGHT) + (kw_score * KW_WEIGHT)
        elif semantic_score >= SEMANTIC_FALLBACK:
            # v10.1: Semantic fallback çok daha düşük çarpanla
            hybrid = semantic_score * SEM_FALLBACK_MULT
        else:
            hybrid = 0.0

        # Negatif keyword penaltısı
        neg_kw_hits = [nkw for nkw in comp_data.get("negative_keywords", [])
                       if nkw in search_text]
        if neg_kw_hits:
            hybrid = max(0.0, hybrid - len(neg_kw_hits) * NEG_KW_PENALTY)

        return {
            "company_name":     comp_name,
            "short":            comp_data["short"],
            "macro_sector":     comp_data["sector"],
            "matched_keywords": ", ".join(kw_hits[:6]) if kw_hits else "Semantik Uyum",
            "negative_hits":    ", ".join(neg_kw_hits[:3]) if neg_kw_hits else "—",
            "kw_count":         len(kw_hits),
            "hybrid_score":     round(hybrid, 1),
            "semantic_score":   round(semantic_score, 1),
        }

    def analyze_all(self, items: list) -> list:
        if not items:
            return []

        _REJECT = {"has_potential": False, "hybrid_score": 0.0, "semantic_score": 0.0,
                    "matches": [], "best_match": {"company_name": "—", "short": "—",
                    "macro_sector": "—", "matched_keywords": "—"}}

        # ── KATMAN 1+2: Filtre sistemi — NLP'den ÖNCE ────────────────────
        nlp_indices = []   # NLP'ye gidecek item index'leri
        filter_stats = {"reject": 0, "accept": 0, "analyze": 0}

        for idx, item in enumerate(items):
            action, reason, cat = filter_item(item.get("title", ""), item.get("fulltext", ""))
            item["_filter_action"] = action
            item["_filter_reason"] = reason
            item["_filter_cat"]    = cat
            filter_stats[action] += 1

            if action == "reject":
                item.update({**_REJECT, "reject_reason": reason})
            else:
                nlp_indices.append(idx)

        print(f"\n📊 Filtre sonuçları: ❌ {filter_stats['reject']} blacklist, "
              f"✅ {filter_stats['accept']} whitelist, 🔍 {filter_stats['analyze']} gri bölge")
        print(f"   NLP'ye giden: {len(nlp_indices)} / {len(items)} karar\n")

        if not nlp_indices:
            return items

        # ── KATMAN 3: NLP — sadece whitelist + gri bölge ─────────────────
        texts = []
        for idx in nlp_indices:
            i = items[idx]
            combined = i.get("title", "")
            ft = i.get("fulltext", "")
            if ft:
                combined += " " + ft[:MAX_FULLTEXT_CHARS]
            texts.append(combined)

        emb   = self.model.encode(texts, convert_to_tensor=True, batch_size=32)
        sim_d = util.cos_sim(emb, self.emb_d)
        sim_s = util.cos_sim(emb, self.emb_s)
        sim_k = util.cos_sim(emb, self.emb_k)
        sim_n = util.cos_sim(emb, self.emb_n)
        max_s, _ = torch.max(torch.stack([sim_d, sim_s, sim_k]), dim=0)

        for ti, idx in enumerate(nlp_indices):
            item   = items[idx]
            action = item["_filter_action"]

            scores   = max_s[ti]
            best_idx = torch.argmax(scores).item()
            raw_sim  = scores[best_idx].item()
            neg_sim  = sim_n[ti][0].item()

            # Bireysel ilan kontrolü (NLP-based)
            if neg_sim > raw_sim + 0.10:
                item.update({**_REJECT, "reject_reason": "Bireysel İlan (NLP)",
                             "semantic_score": round(((raw_sim + 1.0) / 2.0) * 100.0, 1)})
                continue

            # Her şirket için skor hesapla
            search_text = texts[ti].lower()
            all_matches = []

            for cidx, comp_name in enumerate(self.company_names):
                comp_data = INCI_COMPANIES[comp_name]
                raw = scores[cidx].item()
                sem = ((raw + 1.0) / 2.0) * 100.0
                match = self._score_company(search_text, comp_name, comp_data, sem)
                all_matches.append(match)

            all_matches.sort(key=lambda m: m["hybrid_score"], reverse=True)
            best = all_matches[0]

            # Çoklu şirket eşleşme
            matches = [best]
            if best["hybrid_score"] > 0:
                threshold = best["hybrid_score"] * MULTI_COMPANY_THRESH
                for m in all_matches[1:]:
                    if m["hybrid_score"] >= max(threshold, HYBRID_THRESHOLD):
                        matches.append(m)

            # Eşik: herkes için aynı — whitelist sadece "NLP'ye git" demek
            has_potential = best["hybrid_score"] >= HYBRID_THRESHOLD

            item.update({
                "has_potential":  has_potential,
                "hybrid_score":  best["hybrid_score"],
                "semantic_score": best["semantic_score"],
                "best_match":    best,
                "matches":       matches,
            })

        return items


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML EXPORTER — v10: Çoklu eşleşme desteği
# ═══════════════════════════════════════════════════════════════════════════════
class HTMLExporter:
    """v10 — Editorial / Gazette-style dashboard design."""

    @staticmethod
    def _e(s):
        return html_mod.escape(str(s), quote=True)

    @staticmethod
    def _score_color(s):
        if s >= 65: return "#E8A838"
        if s >= 45: return "#7B9EBC"
        return "#5A6272"

    @staticmethod
    def _pri_label(s):
        if s >= 65: return "Kritik"
        if s >= 45: return "Takip"
        return "Düşük"

    @staticmethod
    def _pri_css(s):
        if s >= 65: return "background:#C8553D18;color:#C8553D;border:1px solid #C8553D33"
        if s >= 45: return "background:#E8A83818;color:#E8A838;border:1px solid #E8A83833"
        return "background:#5A627218;color:#7B9EBC;border:1px solid #7B9EBC33"

    def export(self, items: list, filepath: str):
        today      = datetime.now()
        potentials = [i for i in items if i.get("has_potential")]
        total      = len(items)
        relevant   = len(potentials)
        hit_pct    = round(relevant / total * 100) if total else 0

        # Çoklu eşleşme: bir karar birden fazla şirkette görünebilir
        by_company = {comp: [] for comp in INCI_COMPANIES}
        for i in potentials:
            seen_companies = set()
            for m in i.get("matches", []):
                cn = m.get("company_name", "")
                if cn in by_company and cn not in seen_companies:
                    by_company[cn].append(i)
                    seen_companies.add(cn)

        active_co = sum(1 for v in by_company.values() if v)

        # ── JSON veri ──────────────────────────────────────────────────
        rows_json = []
        for i in sorted(items, key=lambda x: x.get("hybrid_score", 0), reverse=True):
            bm = i.get("best_match", {})
            extra_matches = [m.get("short", "") for m in i.get("matches", [])[1:]]
            rows_json.append({
                "company":   bm.get("company_name", ""),
                "short":     bm.get("short", ""),
                "title":     i.get("title", ""),
                "category":  i.get("category", ""),
                "kw":        bm.get("matched_keywords", "—"),
                "hybrid":    i.get("hybrid_score", 0),
                "url":       i.get("url", ""),
                "date":      i.get("date", ""),
                "potential": i.get("has_potential", False),
                "extra":     extra_matches,
            })

        rows_json_str = json.dumps(rows_json, ensure_ascii=False)

        # ── Şirket kartları ────────────────────────────────────────────
        company_cards_html = ""
        for comp_name, comp_data in INCI_COMPANIES.items():
            comp_items = by_company.get(comp_name, [])
            count = len(comp_items)

            if not comp_items:
                card_content = '<p class="no-items">Bugün ilgili karar bulunamadı.</p>'
            else:
                card_content = ""
                for item in sorted(comp_items, key=lambda x: x.get("hybrid_score", 0), reverse=True)[:4]:
                    score = item.get("hybrid_score", 0)
                    title = self._e(item.get("title", ""))
                    url   = self._e(item.get("url", "#"))
                    kw = "—"
                    for m in item.get("matches", []):
                        if m.get("company_name") == comp_name:
                            kw = self._e(m.get("matched_keywords", "—"))
                            break
                    kw_dots = " · ".join(k.strip() for k in kw.split(",")[:3]) if kw != "—" else "—"

                    multi_badge = ""
                    mc = len(item.get("matches", []))
                    if mc > 1:
                        multi_badge = f'<span class="multi-chip">+{mc - 1}</span>'

                    card_content += f"""
                    <div class="ci">
                      <div class="ci-head">
                        <span class="pri-tag" style="{self._pri_css(score)}">{self._pri_label(score)}</span>
                        {multi_badge}
                        <span class="ci-score">{score:.1f}</span>
                      </div>
                      <a href="{url}" target="_blank" class="ci-title">{title}</a>
                      <div class="ci-kw">{kw_dots}</div>
                    </div>"""

            more = f'<p class="more-hint">+ {count - 4} karar daha</p>' if count > 4 else ""
            company_cards_html += f"""
            <div class="cc" style="--co:{comp_data['color']}">
              <div class="cc-head">
                <span class="cc-icon">{comp_data['badge']}</span>
                <div class="cc-info">
                  <span class="cc-short">{comp_data['short']}</span>
                  <span class="cc-name">{self._e(comp_name)}</span>
                </div>
                <div class="cc-count {'cc-count-on' if count else 'cc-count-off'}">{count}</div>
              </div>
              <div class="cc-sector">{self._e(comp_data['sector'])}</div>
              <div class="cc-body">{card_content}{more}</div>
            </div>"""

        # ── HTML ───────────────────────────────────────────────────────
        date_long  = today.strftime('%d %B %Y')
        date_time  = today.strftime('%H:%M')
        colors_js  = ', '.join(f'"{k}": "{v["color"]}"' for k, v in INCI_COMPANIES.items())

        html_out = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>İnci Holding — Mevzuat Radarı {today.strftime('%d.%m.%Y')}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ── v10 Editorial Gazette Theme ───────────────────────────── */
:root{{
  --bg:#0C0C0E;--s1:#111114;--s2:#0E0E10;--bd:#1E1E22;--bd2:#1A1A1E;
  --t1:#EEEAE2;--t2:#B0ACA4;--t3:#8A8578;--t4:#5A5750;--t5:#3A3A3E;--t6:#2A2A2E;
  --red:#C8553D;--gold:#E8A838;--blue:#2E86AB;--violet:#7E64A8;--slate:#7B9EBC;
  --serif:'Cormorant Garamond',Georgia,serif;
  --mono:'JetBrains Mono',Consolas,monospace;
  --r:8px;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:var(--serif);background:var(--bg);color:var(--t2);
  min-height:100vh;line-height:1.5;-webkit-font-smoothing:antialiased}}
/* Noise overlay */
body::before{{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.025;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}}

/* ── Masthead ──────────────────────────────────────────────── */
.mast{{position:sticky;top:0;z-index:100;background:linear-gradient(180deg,var(--bg),rgba(12,12,14,.97));
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--bd);padding:0 48px}}
.mast-inner{{max-width:1440px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:72px}}
.mast-brand{{display:flex;align-items:center;gap:20px}}
.mast-logo{{width:40px;height:40px;background:linear-gradient(135deg,var(--red),#A33A25);
  border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:18px;
  box-shadow:0 2px 12px rgba(200,85,61,.3)}}
.mast-title{{font-size:18px;font-weight:700;letter-spacing:-.01em;color:var(--t1)}}
.mast-v{{margin-left:10px;font-size:9px;font-family:var(--mono);background:rgba(200,85,61,.15);
  border:1px solid rgba(200,85,61,.3);border-radius:3px;padding:2px 7px;color:var(--red);
  vertical-align:middle;letter-spacing:.5px}}
.mast-sub{{font-size:11px;color:var(--t4);font-family:var(--mono);letter-spacing:.3px;margin-top:2px}}
.mast-meta{{text-align:right;font-family:var(--mono);font-size:11px;color:var(--t4);line-height:1.7}}
.mast-date{{color:var(--t3)}}

/* ── Main ──────────────────────────────────────────────────── */
.main{{max-width:1440px;margin:0 auto;padding:40px 48px 100px;position:relative;z-index:1}}

/* ── Stats ─────────────────────────────────────────────────── */
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:52px}}
.st{{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);
  padding:24px 28px;position:relative;overflow:hidden;
  animation:fadeUp .5s ease both}}
.st::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--st-c)}}
.st-label{{font-size:10px;font-family:var(--mono);letter-spacing:1.5px;color:var(--t4);margin-bottom:12px;font-weight:500}}
.st-val{{font-size:42px;font-weight:300;line-height:1;color:var(--st-c)}}
.st-hint{{font-size:10px;font-family:var(--mono);color:var(--t5);margin-top:8px}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(12px)}}to{{opacity:1;transform:translateY(0)}}}}
.st:nth-child(1){{animation-delay:.05s}}
.st:nth-child(2){{animation-delay:.1s}}
.st:nth-child(3){{animation-delay:.15s}}
.st:nth-child(4){{animation-delay:.2s}}

/* ── Section ───────────────────────────────────────────────── */
.sec{{display:flex;align-items:center;gap:16px;margin-bottom:24px}}
.sec-t{{font-size:10px;font-family:var(--mono);letter-spacing:2px;color:var(--t4);font-weight:600}}
.sec-line{{flex:1;height:1px;background:var(--bd)}}

/* ── Company Cards ─────────────────────────────────────────── */
.cg{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;margin-bottom:60px}}
.cc{{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden;
  animation:fadeUp .5s ease both}}
.cc:nth-child(1){{animation-delay:.25s}}
.cc:nth-child(2){{animation-delay:.3s}}
.cc:nth-child(3){{animation-delay:.35s}}
.cc:nth-child(4){{animation-delay:.4s}}
.cc:nth-child(5){{animation-delay:.45s}}
.cc-head{{padding:18px 22px;display:flex;align-items:center;gap:14px;
  border-bottom:1px solid color-mix(in srgb,var(--co) 8%,transparent);
  background:linear-gradient(135deg,color-mix(in srgb,var(--co) 4%,transparent),transparent)}}
.cc-icon{{font-size:22px}}
.cc-info{{flex:1}}
.cc-short{{display:block;font-size:9px;font-family:var(--mono);color:var(--co);letter-spacing:1.5px;font-weight:600;opacity:.8}}
.cc-name{{font-size:14px;font-weight:600;color:var(--t1);line-height:1.3}}
.cc-count{{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:15px;font-weight:700;font-family:var(--mono)}}
.cc-count-on{{background:color-mix(in srgb,var(--co) 15%,transparent);color:var(--co);
  border:1px solid color-mix(in srgb,var(--co) 30%,transparent)}}
.cc-count-off{{background:var(--bd2);color:var(--t5);border:1px solid var(--t6)}}
.cc-sector{{font-size:10px;color:var(--t4);padding:8px 22px;border-bottom:1px solid var(--bd2);
  font-family:var(--mono);letter-spacing:.3px}}
.cc-body{{padding:14px 22px}}
.no-items{{font-size:12px;color:var(--t5);padding:4px 0;font-style:italic}}

/* Card items */
.ci{{margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--bd2)}}
.ci:last-child{{margin-bottom:0;padding-bottom:0;border-bottom:none}}
.ci-head{{display:flex;align-items:center;gap:8px;margin-bottom:6px}}
.pri-tag{{font-size:9px;font-weight:600;padding:2px 8px;border-radius:3px;letter-spacing:.5px;font-family:var(--mono)}}
.multi-chip{{font-size:8px;font-family:var(--mono);padding:2px 6px;border-radius:3px;
  background:rgba(126,100,168,.12);color:var(--violet);border:1px solid rgba(126,100,168,.25)}}
.ci-score{{font-size:10px;font-family:var(--mono);color:var(--t4);margin-left:auto}}
.ci-title{{display:block;font-size:13px;line-height:1.55;color:var(--t2);text-decoration:none;
  font-weight:500;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.ci-title:hover{{color:var(--t1)}}
.ci-kw{{font-size:10px;color:var(--t5);margin-top:5px;font-family:var(--mono)}}
.more-hint{{font-size:10px;color:var(--t4);margin-top:10px;font-style:italic}}

/* ── Table Controls ────────────────────────────────────────── */
.tc{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}}
.tc-search{{background:var(--s1);border:1px solid var(--bd);border-radius:6px;color:var(--t2);
  font-size:12px;padding:9px 16px;outline:none;width:260px;font-family:var(--mono);transition:border .2s}}
.tc-search:focus{{border-color:var(--t5)}}
.tc-btn{{background:transparent;border:1px solid var(--bd);border-radius:6px;color:var(--t4);
  font-size:11px;padding:8px 16px;cursor:pointer;font-family:var(--mono);letter-spacing:.3px;transition:all .2s}}
.tc-btn:hover,.tc-btn.on{{border-color:var(--t5);color:var(--t2);background:var(--bd2)}}
.tc-count{{margin-left:auto;font-size:10px;color:var(--t5);font-family:var(--mono)}}

/* ── Table ─────────────────────────────────────────────────── */
.tw{{background:var(--s1);border:1px solid var(--bd);border-radius:var(--r);overflow:hidden}}
.tw-scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
thead{{background:var(--s2);position:sticky;top:72px;z-index:50}}
th{{padding:14px 18px;text-align:left;font-size:9px;font-family:var(--mono);font-weight:600;
  letter-spacing:1.2px;color:var(--t5);cursor:pointer;border-bottom:1px solid var(--bd2);
  text-transform:uppercase;white-space:nowrap;transition:color .2s;user-select:none}}
th:hover,th.sorted{{color:var(--t3)}}
td{{padding:12px 18px;vertical-align:middle;border-bottom:1px solid #151518}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#14141A}}
tr.hidden{{display:none!important}}
/* Status */
.s-tag{{font-size:9px;font-weight:600;padding:3px 10px;border-radius:3px;font-family:var(--mono);
  letter-spacing:.4px;white-space:nowrap}}
.s-yes{{background:#C8553D12;color:var(--red);border:1px solid #C8553D28}}
.s-no{{background:var(--bd2);color:var(--t5);border:1px solid #222226}}
/* Company chip */
.co-tag{{font-size:9px;font-family:var(--mono);font-weight:600;padding:3px 8px;border-radius:3px;
  letter-spacing:.5px;white-space:nowrap}}
.ex-tag{{font-size:8px;font-family:var(--mono);padding:2px 5px;border-radius:2px;margin-left:4px;
  background:rgba(126,100,168,.1);color:var(--violet);border:1px solid rgba(126,100,168,.2)}}
/* Title */
.t-link{{color:var(--t2);text-decoration:none;font-size:12px;line-height:1.5;font-weight:500;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.t-link:hover{{color:var(--t1)}}
/* Keywords */
.kw-t{{font-size:9px;font-family:var(--mono);padding:2px 7px;border-radius:3px;
  background:var(--bd2);color:var(--t4);border:1px solid #222226;white-space:nowrap;
  display:inline-block;margin:1px 2px}}
/* Score bar */
.sb{{display:flex;align-items:center;gap:10px}}
.sb-track{{flex:1;height:3px;background:var(--bd2);border-radius:2px;overflow:hidden;min-width:50px}}
.sb-fill{{height:100%;border-radius:2px;transition:width .6s cubic-bezier(.23,1,.32,1)}}
.sb-val{{font-family:var(--mono);font-size:10px;min-width:32px;text-align:right;font-weight:500}}
/* Category / Date */
.cat-td{{color:var(--t5);font-size:10px;font-family:var(--mono);max-width:140px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.date-td{{font-family:var(--mono);font-size:10px;color:var(--t5);white-space:nowrap}}

/* ── Footer ────────────────────────────────────────────────── */
.ft{{margin-top:56px;padding-top:24px;border-top:1px solid var(--bd2);
  text-align:center;line-height:2;font-family:var(--mono);font-size:10px;color:var(--t6)}}
.ft-copy{{margin-top:8px;color:var(--t5)}}

/* ── Responsive ────────────────────────────────────────────── */
@media(max-width:900px){{
  .stats{{grid-template-columns:repeat(2,1fr)}}
  .cg{{grid-template-columns:1fr}}
  .main{{padding:20px 16px 60px}}
  .mast{{padding:0 16px}}
  .mast-meta{{display:none}}
}}
</style>
</head>
<body>

<header class="mast">
  <div class="mast-inner">
    <div class="mast-brand">
      <div class="mast-logo">🏛️</div>
      <div>
        <div class="mast-title">Mevzuat Radarı <span class="mast-v">v10</span></div>
        <div class="mast-sub">İNCİ HOLDİNG · RESMİ GAZETE · GÜNLÜK ANALİZ</div>
      </div>
    </div>
    <div class="mast-meta">
      <div class="mast-date">{date_long}, {date_time}</div>
      <div>Eşik: {HYBRID_THRESHOLD} · Çoklu Eşleşme · KW %{KW_WEIGHT*100:.0f} / Sem %{SEM_WEIGHT*100:.0f}</div>
    </div>
  </div>
</header>

<main class="main">
  <!-- Stats -->
  <div class="stats">
    <div class="st" style="--st-c:var(--slate)">
      <div class="st-label">TARANAN</div>
      <div class="st-val">{total}</div>
      <div class="st-hint">Bugünkü Resmi Gazete</div>
    </div>
    <div class="st" style="--st-c:var(--red)">
      <div class="st-label">İLGİLİ</div>
      <div class="st-val">{relevant}</div>
      <div class="st-hint">Eşiği geçen ≥ {HYBRID_THRESHOLD}</div>
    </div>
    <div class="st" style="--st-c:var(--gold)">
      <div class="st-label">AKTİF ŞİRKET</div>
      <div class="st-val">{active_co}</div>
      <div class="st-hint">Eşleşme olan grup</div>
    </div>
    <div class="st" style="--st-c:var(--blue)">
      <div class="st-label">İSABET</div>
      <div class="st-val">{hit_pct}%</div>
      <div class="st-hint">İlgili / Toplam</div>
    </div>
  </div>

  <!-- Company Cards -->
  <div class="sec"><span class="sec-t">ŞİRKET BAZINDA ÖZET</span><div class="sec-line"></div></div>
  <div class="cg">{company_cards_html}</div>

  <!-- Table -->
  <div class="sec"><span class="sec-t">TÜM KARARLAR</span><div class="sec-line"></div></div>
  <div class="tc">
    <input type="text" class="tc-search" id="searchBox" placeholder="Ara...">
    <button class="tc-btn on" onclick="setFilter(this,'all')">Tümü</button>
    <button class="tc-btn"    onclick="setFilter(this,'yes')">İlgili</button>
    <button class="tc-btn"    onclick="setFilter(this,'no')">İlgisiz</button>
    <span class="tc-count" id="rowCount">{total} sonuç</span>
  </div>
  <div class="tw"><div class="tw-scroll">
    <table>
      <thead><tr>
        <th onclick="sortT(0)">Durum</th>
        <th onclick="sortT(1)">Şirket</th>
        <th onclick="sortT(2)" style="min-width:360px">Başlık</th>
        <th onclick="sortT(3)">Kategori</th>
        <th onclick="sortT(4)">Anahtar Kelime</th>
        <th onclick="sortT(5)">Skor</th>
        <th onclick="sortT(6)">Tarih</th>
      </tr></thead>
      <tbody id="tb"></tbody>
    </table>
  </div></div>

  <div class="ft">
    <div>T.C. Resmi Gazete otomatik tarama ile oluşturulmuştur.</div>
    <div>Hukuki değerlendirme için ilgili uzmanlarla görüşünüz.</div>
    <div class="ft-copy">© {today.year} İnci Holding — Mevzuat Radar v10</div>
  </div>
</main>

<script>
const R={rows_json_str};
const CC={{{colors_js}}};
const tb=document.getElementById('tb');
const rc=document.getElementById('rowCount');

function scoreC(s){{return s>=65?'#E8A838':s>=45?'#7B9EBC':'#5A6272'}}

function render(d){{
  tb.innerHTML='';
  d.forEach(r=>{{
    const tr=document.createElement('tr');
    tr.dataset.p=r.potential?'yes':'no';
    tr.dataset.t=(r.title+' '+r.kw+' '+r.company+' '+(r.extra||[]).join(' ')).toLowerCase();
    const sc=r.hybrid;const c=scoreC(sc);
    const co=CC[r.company]||'#555';
    const kw=r.kw&&r.kw!=='—'
      ?r.kw.split(',').map(k=>`<span class="kw-t">${{k.trim()}}</span>`).join('')
      :'<span style="color:#2A2A2E">—</span>';
    const ex=(r.extra||[]).map(e=>`<span class="ex-tag">${{e}}</span>`).join('');
    tr.innerHTML=`
      <td><span class="s-tag ${{r.potential?'s-yes':'s-no'}}">${{r.potential?'İLGİLİ':'İLGİSİZ'}}</span></td>
      <td style="white-space:nowrap"><span class="co-tag" style="background:${{co}}12;color:${{co}};border:1px solid ${{co}}28">${{r.short}}</span>${{ex}}</td>
      <td style="min-width:360px"><a href="${{r.url}}" target="_blank" class="t-link">${{r.title}}</a></td>
      <td class="cat-td" title="${{r.category}}">${{r.category}}</td>
      <td>${{kw}}</td>
      <td style="min-width:120px"><div class="sb">
        <div class="sb-track"><div class="sb-fill" style="width:${{Math.min(100,sc)}}%;background:${{c}}"></div></div>
        <span class="sb-val" style="color:${{c}}">${{sc.toFixed(1)}}</span>
      </div></td>
      <td class="date-td">${{r.date}}</td>`;
    tb.appendChild(tr);
  }});
}}
render(R);

let filt='all',qry='';
function apply(){{
  let vis=0;
  tb.querySelectorAll('tr').forEach(tr=>{{
    const fOk=filt==='all'||tr.dataset.p===filt;
    const sOk=!qry||tr.dataset.t.includes(qry);
    const show=fOk&&sOk;
    tr.classList.toggle('hidden',!show);
    if(show)vis++;
  }});
  rc.textContent=vis+' sonuç';
}}
function setFilter(btn,f){{
  filt=f;
  document.querySelectorAll('.tc-btn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  apply();
}}
document.getElementById('searchBox').addEventListener('input',e=>{{
  qry=e.target.value.toLowerCase();
  apply();
}});

let sd=Array(7).fill(1);
function sortT(col){{
  const rows=[...tb.querySelectorAll('tr')];
  rows.sort((a,b)=>{{
    const av=a.cells[col]?.textContent?.trim()||'';
    const bv=b.cells[col]?.textContent?.trim()||'';
    const an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn))return(an-bn)*sd[col];
    return av.localeCompare(bv,'tr')*sd[col];
  }});
  sd[col]*=-1;
  rows.forEach(r=>tb.appendChild(r));
  apply();
}}
</script>
</body>
</html>"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_out)
        return filepath


# ═══════════════════════════════════════════════════════════════════════════════
#  ANA PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════
def run_pipeline():
    logger = setup_logging()
    today  = datetime.now()

    print("=" * 70)
    print("🏛️  İNCİ HOLDİNG — RESMİ GAZETE MEVZUAT RADARI v10.2")
    print(f"    Tarih         : {today.strftime('%d.%m.%Y %H:%M')}")
    print(f"    Mimari        : 3 Katmanlı Filtre (Blacklist → Whitelist → NLP)")
    print(f"    Blacklist     : {len(_BLACKLIST)} kategori (atama, kamulaştırma, askeri...)")
    print(f"    Whitelist     : {len(_WHITELIST)} kategori (gümrük, enerji, otomotiv...)")
    print(f"    NLP Eşik      : Hybrid ≥ {HYBRID_THRESHOLD} (tüm kararlar aynı eşik)")
    print(f"    Ağırlık       : Keyword %{KW_WEIGHT*100:.0f} / Semantic %{SEM_WEIGHT*100:.0f}")
    print(f"    Retry         : {MAX_RETRIES}× deneme, {RETRY_BACKOFF}s backoff")
    print("=" * 70)

    print("\n🕵️  Resmi Gazete taranıyor...")
    scraper = ResmiGazeteScraper(logger)
    items   = scraper.fetch_today()

    if not items:
        print("\n❌ Bugün için taranacak veri bulunamadı.")
        return

    print(f"\n📥 {len(items)} karar/madde → 3 katmanlı filtre + AI analizi...\n")

    engine = RG_AIEngine(logger)
    final  = engine.analyze_all(items)
    pots   = [i for i in final if i.get("has_potential")]

    # Filtre istatistikleri
    rejected  = sum(1 for i in final if i.get("_filter_action") == "reject")
    accepted  = sum(1 for i in final if i.get("_filter_action") == "accept")
    analyzed  = sum(1 for i in final if i.get("_filter_action") == "analyze")

    print("─── SKOR TABLOSU ───────────────────────────────────────────────────────")
    for i in sorted(final, key=lambda x: x.get("hybrid_score", 0), reverse=True):
        flag   = "✅" if i.get("has_potential") else "  "
        bm     = i.get("best_match", {})
        action = i.get("_filter_action", "?")
        reason = i.get("_filter_reason", "")[:20]
        extras = [m.get("short", "") for m in i.get("matches", [])[1:]]
        extra_str = f" +{','.join(extras)}" if extras else ""

        if action == "reject":
            print(f"   ❌BL [{reason:>20s}] {i.get('title','')[:55]}")
        else:
            act_icon = "✅WL" if action == "accept" else "🔍GB"
            print(
                f"{flag} {act_icon} [{bm.get('short','???'):3s}{extra_str:>8s}] "
                f"H:{i.get('hybrid_score',0):5.1f} "
                f"S:{i.get('semantic_score',0):5.1f} │ "
                f"KW: {bm.get('matched_keywords','—')[:28]:<28} │ "
                f"{i.get('title','')[:42]}"
            )
    print("────────────────────────────────────────────────────────────────────────")
    print(f"\n✓ Sonuç: {len(pots)} ilgili / {len(final)} toplam")
    print(f"  └─ Blacklist eledi: {rejected} | Whitelist geçirdi: {accepted} | Gri bölge (NLP): {analyzed}")

    # Çoklu eşleşme istatistiği
    multi = sum(1 for i in pots if len(i.get("matches", [])) > 1)
    if multi:
        print(f"✓ Çoklu eşleşme: {multi} karar birden fazla şirketi ilgilendiriyor")

    fname    = f"inci_rg_rapor_v10_{today.strftime('%Y%m%d_%H%M')}.html"
    out_path = str(EXPORTS_DIR / fname)
    HTMLExporter().export(final, out_path)
    print(f"🌐 HTML: {out_path}")

    # ── GitHub Pages çıktısı ──────────────────────────────────────────
    docs_dir = BASE_DIR / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    import shutil
    shutil.copy2(out_path, str(docs_dir / "index.html"))
    shutil.copy2(out_path, str(docs_dir / f"rapor_{today.strftime('%Y%m%d')}.html"))

    # Arşiv sayfası
    import glob
    arsiv_files = sorted(glob.glob(str(docs_dir / "rapor_*.html")), reverse=True)
    arsiv_rows = ""
    for af in arsiv_files:
        import re as _re
        nm = os.path.basename(af)
        m = _re.search(r"rapor_(\d{8})", nm)
        if m:
            d = datetime.strptime(m.group(1), "%Y%m%d")
            label = d.strftime("%d.%m.%Y")
            gunler = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
            weekday = gunler[d.weekday()]
        else:
            label, weekday = nm, ""
        arsiv_rows += f'<tr><td style="color:#5A5750;font-size:12px;width:100px">{weekday}</td><td><a href="{nm}" style="color:#C8553D">{label}</a></td></tr>\n'

    arsiv_html = f"""<!DOCTYPE html><html lang="tr"><head><meta charset="UTF-8"><title>Arşiv</title>
<style>body{{font-family:system-ui;background:#0C0C0E;color:#B0ACA4;max-width:700px;margin:40px auto;padding:0 20px}}
h1{{color:#EEEAE2;font-size:22px;border-bottom:1px solid #1E1E22;padding-bottom:12px}}
a{{color:#C8553D;text-decoration:none}}table{{width:100%;border-collapse:collapse}}
td{{padding:10px 16px;border-bottom:1px solid #1A1A1E}}</style></head>
<body><a href="index.html" style="color:#7B9EBC;font-size:13px">← Güncel Rapor</a>
<h1>🏛️ Arşiv</h1><p style="color:#5A5750;font-size:12px">{len(arsiv_files)} rapor</p>
<table>{arsiv_rows}</table></body></html>"""
    with open(str(docs_dir / "arsiv.html"), "w", encoding="utf-8") as af:
        af.write(arsiv_html)
    print(f"📂 Arşiv: {docs_dir / 'arsiv.html'} ({len(arsiv_files)} rapor)")

    # Summary JSON
    summary = {
        "date": today.strftime("%Y-%m-%d"),
        "total": len(final),
        "relevant": len(pots),
        "blacklisted": rejected,
        "whitelisted": accepted,
        "gray_zone": analyzed,
        "report_file": fname,
    }
    summary_path = str(EXPORTS_DIR / "summary.json")
    with open(summary_path, "w", encoding="utf-8") as sf:
        json.dump(summary, sf, ensure_ascii=False, indent=2)

    print("\n✅ TAMAMLANDI\n")


if __name__ == "__main__":
    run_pipeline()

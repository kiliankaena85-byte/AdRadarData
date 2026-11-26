"""
Deterministic Validators for INN, OGRN, erid Tokens, Surrogate Labels, and 168-FZ.
Mathematical checksum validation under Federal Tax Service (FNS) algorithms,
exact regex parsers, and surrogate label detection.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple


AFFILIATE_REGEX = re.compile(
    r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|r|utm_medium=affiliate)=|/(?:r|promo|aff|partner)/|start=(?:ref|w\d+))"
)

PROMO_CODE_REGEX = re.compile(
    r"(?i)(?:промокод[:\s]+[A-Za-z0-9_-]+|скидк\w*\s+по\s+промокоду|бонус\s+по\s+ссылке)"
)


# =====================================================================
# 1. FNS MATHEMATICAL CHECKSUMS (ИНН 10/12, ОГРН 13, ОГРНИП 15)
# =====================================================================

INN10_WEIGHTS = [2, 4, 10, 3, 5, 9, 4, 6, 8, 0]
INN12_WEIGHTS_1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0]
INN12_WEIGHTS_2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0]


def validate_inn_checksum(inn_str: str) -> bool:
    """
    Validate Russian Taxpayer Identification Number (ИНН) by FNS checksum algorithm.
    - 10 digits: Legal entities (Юридические лица)
    - 12 digits: Individuals and Sole Proprietors (ФЛ и ИП)
    """
    digits_only = re.sub(r"\D", "", inn_str)
    n = len(digits_only)
    if n not in (10, 12):
        return False
    if set(digits_only) == {"0"}:
        return False

    digits = [int(c) for c in digits_only]

    if n == 10:
        c10 = sum(digits[i] * INN10_WEIGHTS[i] for i in range(10)) % 11 % 10
        return digits[9] == c10
    else:  # n == 12
        c11 = sum(digits[i] * INN12_WEIGHTS_1[i] for i in range(11)) % 11 % 10
        c12 = sum(digits[i] * INN12_WEIGHTS_2[i] for i in range(12)) % 11 % 10
        return digits[10] == c11 and digits[11] == c12


def validate_ogrn_checksum(ogrn_str: str) -> bool:
    """
    Validate Russian Primary State Registration Number (ОГРН / ОГРНИП).
    - 13 digits: Legal entities (ОГРН, % 11 % 10)
    - 15 digits: Individual entrepreneurs (ОГРНИП, % 13 % 10)
    """
    digits_only = re.sub(r"\D", "", ogrn_str)
    n = len(digits_only)
    if n not in (13, 15):
        return False
    if set(digits_only) == {"0"}:
        return False

    if n == 13:
        num = int(digits_only[:12])
        check = (num % 11) % 10
        return check == int(digits_only[12])
    else:  # n == 15
        num = int(digits_only[:14])
        check = (num % 13) % 10
        return check == int(digits_only[14])


def find_valid_inns_in_text(text: str) -> List[Tuple[str, int]]:
    """
    Extract all candidate 10 or 12 digit numbers and return those with valid FNS checksums.
    Returns list of (inn_string, length).
    """
    matches = re.finditer(r"\b(\d{10}|\d{12})\b", text)
    valid_inns = []
    for m in matches:
        candidate = m.group(1)
        if validate_inn_checksum(candidate):
            valid_inns.append((candidate, len(candidate)))
    return valid_inns


def find_valid_ogrns_in_text(text: str) -> List[Tuple[str, int]]:
    """Extract and validate 13 or 15 digit OGRN/OGRNIP numbers."""
    matches = re.finditer(r"\b(\d{13}|\d{15})\b", text)
    valid_ogrns = []
    for m in matches:
        candidate = m.group(1)
        if validate_ogrn_checksum(candidate):
            valid_ogrns.append((candidate, len(candidate)))
    return valid_ogrns


# =====================================================================
# 2. ERID TOKEN PARSING AND INTEGRITY (ORD MARKING)
# =====================================================================

ERID_TEXT_REGEXES = [
    re.compile(r"(?i)\berid\s*[:=]\s*([a-zA-Z0-9_\-]{8,128})\b"),
    re.compile(r"(?i)\berid\s+([a-zA-Z0-9_\-]{8,128})\b"),
    re.compile(r"(?i)\bтокен\s+(?:маркировки|орд|ерир|рекламы)\s*[:=\s]\s*([a-zA-Z0-9_\-]{8,128})\b"),
]

ERID_URL_REGEX = re.compile(r"[?&](?:erid|erid_token|ord_token)=([a-zA-Z0-9_\-%]+)", re.IGNORECASE)


def validate_erid_token_format(token: str) -> bool:
    """
    Check if a candidate erid string matches ORD token format.
    Must be 8..128 chars, alphanumeric/underscore/hyphen, non-trivial entropy,
    and strictly contain BOTH at least one digit and at least one ASCII letter
    (excludes plain English words like 'loopback' or 'redirect' and pure numbers).
    """
    if not (8 <= len(token) <= 128):
        return False
    if not re.match(r"^[a-zA-Z0-9_\-]+$", token):
        return False
    if not any(ch.isdigit() for ch in token) or not any(ch.isalpha() for ch in token):
        return False
    # Check that it is not all identical characters or trivial test strings
    if len(set(token.lower())) < 4:
        return False
    if token.lower() in {"12345678", "abcdefgh", "testtoken", "xxxxxxxx", "00000000"}:
        return False
    return True


def extract_erid_tokens(
    text: str,
    page_url: str = "",
    platform_ad_meta: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """
    Deterministically extract erid tokens from:
    1. Clean text body (strict 'erid' or ORD marking token keys)
    2. Page / Target URLs ('erid', 'erid_token', 'ord_token' query params)
    3. Platform metadata (e.g. ad_data JSON)
    """
    found_tokens: Set[str] = set()

    # 1. Text extraction
    for rgx in ERID_TEXT_REGEXES:
        for m in rgx.finditer(text or ""):
            tok = m.group(1).strip()
            if validate_erid_token_format(tok):
                found_tokens.add(tok)

    # 2. Page URL or embedded URL query params
    if page_url:
        for m in ERID_URL_REGEX.finditer(page_url):
            clean_val = urllib.parse.unquote(m.group(1)).strip()
            if validate_erid_token_format(clean_val):
                found_tokens.add(clean_val)
    if text:
        for m in ERID_URL_REGEX.finditer(text):
            clean_val = urllib.parse.unquote(m.group(1)).strip()
            if validate_erid_token_format(clean_val):
                found_tokens.add(clean_val)

    # 3. Platform ad metadata (JSON dictionary)
    if platform_ad_meta:
        def _scan_meta(obj: Any):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in ("erid", "erid_token", "token", "ord_token", "marking_token") and isinstance(v, str):
                        if validate_erid_token_format(v.strip()):
                            found_tokens.add(v.strip())
                    else:
                        _scan_meta(v)
            elif isinstance(obj, list):
                for item in obj:
                    _scan_meta(item)

        _scan_meta(platform_ad_meta)

    return sorted(list(found_tokens))


# =====================================================================
# 3. SURROGATE AD LABELS VS STRICT "РЕКЛАМА" (ч. 16 ст. 18.1 38-ФЗ)
# =====================================================================

STRICT_AD_LABEL_HASHTAG = re.compile(r"(?i)(?:^|[\s,(])#реклам(?:а|ный_?материал)(?=$|[\s,.;:!?)»\"])")
STRICT_AD_LABEL_CAPITAL = re.compile(
    r"(?:^|[\n.!?»\"(\[])\s*Реклам(?:а|ный\s+материал)\s*(?:[:.!—\-]|$)"
    r"|\bРеклам(?:а|ный\s+материал)\s+(?:ООО|ИП|АО|ПАО|ЗАО|НАО|НКО|[«\"][А-ЯA-Z])"
)
STRICT_AD_CONTEXT_MARKER = re.compile(
    r"(?i)(?:\berid\b|\bинн\b|\bогрн\b|\b(?:0|6|12|16|18)\+)"
)
STRICT_AD_WORD = re.compile(r"(?i)(?<![а-яёa-z0-9])реклам(?:а|ный\s+материал)(?![а-яёa-z0-9])")

SURROGATE_LABELS = [
    "партнерский материал",
    "партнерский пост",
    "партнерский проект",
    "промо",
    "спецпроект",
    "при поддержке",
    "информационный партнер",
    "спонсорский контент",
    "спонсорский материал",
    "на правах рекламы",
    "рекламная интеграция",
    "коллаборация",
    "совместный проект",
]


def analyze_ad_label_status(text: str) -> Dict[str, Any]:
    """
    Verify whether a genuine 38-FZ advertising label ('#реклама', 'Реклама.',
    'Реклама:', 'Реклама ООО/ИП...', or 'Реклама' paired with ИНН/ОГРН/erid/16+)
    is present, ignoring ordinary prose mentions like 'реклама курсов' or
    'рекламный бюджет'.
    """
    raw_text = text or ""
    has_strict_ad_label = bool(
        STRICT_AD_LABEL_HASHTAG.search(raw_text)
        or STRICT_AD_LABEL_CAPITAL.search(raw_text)
    )

    if not has_strict_ad_label:
        for m in STRICT_AD_WORD.finditer(raw_text):
            start = max(0, m.start() - 140)
            end = min(len(raw_text), m.end() + 140)
            window = raw_text[start:end]
            if STRICT_AD_CONTEXT_MARKER.search(window):
                has_strict_ad_label = True
                break

    text_lower = raw_text.lower()
    found_surrogates = []
    for surr in SURROGATE_LABELS:
        if re.search(rf"(?<![а-яёa-z0-9]){re.escape(surr)}(?![а-яёa-z0-9])", text_lower):
            found_surrogates.append(surr)

    # Illegal surrogate: surrogate label is used, but exact statutory label «Реклама» is missing
    is_illegal_surrogate = (len(found_surrogates) > 0) and not has_strict_ad_label

    return {
        "has_strict_ad_label": has_strict_ad_label,
        "found_surrogates": found_surrogates,
        "is_illegal_surrogate": is_illegal_surrogate,
        "status_code": (
            1.0 if has_strict_ad_label else (-1.0 if is_illegal_surrogate else 0.0)
        ),
    }


# =====================================================================
# 3B. COMMERCIAL TARGETS & HIDDEN CHANNEL EXTRACTOR
# =====================================================================

URL_IN_TEXT_REGEX = re.compile(r"https?://[^\s<>\"'\]]+", re.IGNORECASE)
MESSENGER_CHANNEL_REGEX = re.compile(
    r"(?i)(?:https?://)?\b(t\.me|telegram\.me|max\.ru|vk\.com)/([a-zA-Z0-9_.\-/]{3,80})"
)
SHORTENER_HOSTS: Set[str] = {
    "clktrk.ru", "clck.ru", "goo.su", "vk.cc", "bit.ly", "t.co", "wuun.ru", "ya.cc", "u.to",
}
NEUTRAL_PLATFORM_HOSTS: Set[str] = {
    "vc.ru", "t.me", "telegram.me", "telegram.org", "vk.com", "max.ru",
    "youtube.com", "youtu.be", "dzen.ru", "habr.com", "rbc.ru", "rb.ru",
    "kommersant.ru", "vedomosti.ru", "forbes.ru", "tass.ru", "ria.ru", "interfax.ru",
    "lenta.ru", "gazeta.ru", "iz.ru", "rg.ru", "cnews.ru", "tadviser.ru",
    "forklog.com", "cointelegraph.com", "coindesk.com", "cryptoglobe.com", "crypto.news", "beincrypto.com", "getmonero.org", "investing.com", "tradingview.com",
    "github.com", "gitlab.com", "huggingface.co", "pypi.org", "npmjs.com", "stackoverflow.com",
    "arxiv.org", "doi.org", "ncbi.nlm.nih.gov", "nature.com", "sciencedirect.com", "ieee.org",
    "wikipedia.org", "wikimedia.org", "w3.org", "mozilla.org", "statista.com",
    "blogspot.com", "medium.com", "substack.com", "wordpress.com",
    "google.com", "yandex.ru", "ya.ru", "cbr.ru", "fas.gov.ru", "rkn.gov.ru", "nalog.gov.ru",
    "stats.hh.ru", "hh.ru", "consultant.ru", "garant.ru", "gosuslugi.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "apple.com", "android.com",
    "play.google.com", "apps.apple.com", "spacex.com", "gmail.com", "mail.ru",
    "disk.yandex.ru", "docs.yandex.ru", "docs.google.com", "drive.google.com",
    "sheets.google.com", "notion.so", "figma.com", "miro.com", "telegra.ph",
    "help.openai.com", "docs.openai.com", "platform.openai.com",
    "developer.mozilla.org", "developers.google.com",
    "hubspotusercontent-na1.net", "orangelogic.com",
}


EDITORIAL_NEWS_URL_PATH_REGEX = re.compile(
    r"/(?:news|square/post|blog|article|announcement|press|research|wiki|help)/",
    re.IGNORECASE,
)
CUSTOM_CAMPAIGN_OR_SERVICE_PATH_REGEX = re.compile(
    r"(?i)(?:[?&](?:article|from|source|campaign|sub|click_id)=|"
    r"/(?:services|pricing|tariffs|demo|product|products|teams|apply|order|buy|checkout|shop|catalog|course|courses)(?:/|$|\?))"
)


def is_editorial_crypto_news_url(url: str) -> bool:
    """Returns True when URL points to an editorial/news/blog/help path rather than exchange onboarding."""
    if not url:
        return False
    try:
        path = urllib.parse.urlparse(url).path or ""
    except Exception:
        path = str(url)
    return bool(EDITORIAL_NEWS_URL_PATH_REGEX.search(path))


def has_custom_campaign_or_service_path(url: str) -> bool:
    """Returns True when URL carries custom campaign query keys or explicit commercial product/service paths."""
    if not url:
        return False
    return bool(CUSTOM_CAMPAIGN_OR_SERVICE_PATH_REGEX.search(url))


def _is_host_in_set(host: str, domain_set: Set[str]) -> bool:
    """Matches exact domain or any subdomain (e.g. ru.wikipedia.org -> wikipedia.org)."""
    h = (host or "").lower().strip().split(":")[0].removeprefix("www.")
    if not h:
        return False
    if h in domain_set:
        return True
    return any(h.endswith("." + base_d) for base_d in domain_set)


STRICT_AFFILIATE_PARAM_REGEX = re.compile(
    r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|invite|r)=|/(?:r|promo|aff|partner)/|start=(?:ref|w\d+))"
)
COMMERCIAL_URL_PARAM_REGEX = re.compile(
    r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|r|utm_source|utm_medium|utm_campaign)=|/(?:r|promo|aff|partner)/|start=(?:ref|w\d+))"
)


def has_explicit_commercial_tracking(url: str, page_url: str = "") -> bool:
    """
    Returns True for unambiguous affiliate/referral links, or external non-neutral URLs
    carrying commercial UTM tracking (utm_medium=referral or utm_source + utm_campaign/utm_medium).
    """
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return False
    if not host:
        return False

    has_strict_aff = bool(STRICT_AFFILIATE_PARAM_REGEX.search(url))
    if has_strict_aff:
        return True

    page_host = ""
    if page_url:
        try:
            page_host = urllib.parse.urlparse(page_url).netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            page_host = ""

    is_same_page_host = bool(page_host and (host == page_host or host.endswith("." + page_host)))
    is_neutral = _is_host_in_set(host, NEUTRAL_PLATFORM_HOSTS) or host.endswith(".gov.ru") or host.endswith(".edu")
    if is_neutral or is_same_page_host:
        return False

    lower_u = url.lower()
    if "utm_medium=referral" in lower_u:
        return True
    if "utm_source=" in lower_u and ("utm_campaign=" in lower_u or "utm_medium=" in lower_u):
        return True
    return host in SHORTENER_HOSTS and ("?" in url or len((parsed.path or "").strip("/")) >= 3)
AT_USERNAME_REGEX = re.compile(r"(?<![a-zA-Z0-9._%+\-])@([a-zA-Z][a-zA-Z0-9_]{3,31})\b")
BARE_DOMAIN_REGEX = re.compile(
    r"(?<![a-zA-Z0-9._%+\-/@])([a-zA-Z0-9][a-zA-Z0-9\-]{1,40}\.(?:ru|su|рф|ai|io|com|net|org|store|shop|online|pro|dev|app|kz|by))\b",
    re.IGNORECASE,
)
PHONE_OR_TEL_REGEX = re.compile(
    r"(?i)(?:\btel:\s*\+?\d[\d\-\s()]{7,16}\d|"
    r"(?<!\d)\+7[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)|"
    r"(?<!\d)8(?:[\s\-\(]+\d{3}[\s\-\)]+|\d{3}[\s\-]+)\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d))"
)
CODE_FILE_OR_EXT_TLDS = {"node.js", "next.js", "vue.js", "react.js", "express.js"}


def extract_commercial_targets(
    text: str,
    raw_links: Optional[List[str]] = None,
    extra_exclude_hosts: Optional[Set[str]] = None,
    page_url: str = "",
) -> Dict[str, Any]:
    """
    Extracts explicit AND hidden commercial channels/contacts from text and links:
    - <a href> and plain-text https:// URLs
    - URL shorteners (clktrk.ru, goo.su, wuun.ru, clck.ru, etc.)
    - Concrete messenger channels/bots (t.me/SovoKino_bot, max.ru/..., vk.com/...)
    - Telegram/social @username handles (excluding email addresses)
    - Bare text domains (e.g. svetville.ru)
    - Phone numbers / tel: links
    """
    raw_text = text or ""
    excluded = set(NEUTRAL_PLATFORM_HOSTS)
    if extra_exclude_hosts:
        excluded |= {h.lower().strip() for h in extra_exclude_hosts}

    page_host = ""
    if page_url:
        try:
            page_host = urllib.parse.urlparse(page_url).netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            page_host = ""

    candidate_urls: List[str] = []
    for u in (raw_links or []):
        if isinstance(u, str) and u.strip().startswith(("http://", "https://")):
            clean_u = u.strip().rstrip(".,;:!?)")
            if clean_u not in candidate_urls:
                candidate_urls.append(clean_u)
    for m in URL_IN_TEXT_REGEX.finditer(raw_text):
        clean_u = m.group(0).strip().rstrip(".,;:!?)")
        if clean_u not in candidate_urls:
            candidate_urls.append(clean_u)

    commercial_urls: List[str] = []
    website_urls: List[str] = []
    messenger_links: List[str] = []

    for u in candidate_urls:
        try:
            parsed = urllib.parse.urlparse(u)
            host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
            if not host:
                continue
            path_clean = (parsed.path or "").strip("/")
            has_comm_params = bool(COMMERCIAL_URL_PARAM_REGEX.search(u))
            has_strict_aff = bool(STRICT_AFFILIATE_PARAM_REGEX.search(u))

            # Skip self links on vc.ru / editorial platform unless explicitly affiliate/referral
            if page_host and (host == page_host or host.endswith("." + page_host)) and not has_strict_aff:
                continue

            if host in ("t.me", "telegram.me", "max.ru", "vk.com"):
                # Specific bot/channel/community links are valid commercial targets
                if path_clean and path_clean.lower() not in ("share", "iv", "login", "feed", "im", "joinchat"):
                    if u not in messenger_links:
                        messenger_links.append(u)
                    if u not in commercial_urls:
                        commercial_urls.append(u)
                continue

            is_cdn_pdf = (parsed.path or "").lower().endswith(".pdf") and any(
                k in host for k in ("cdn", "hubspotusercontent", "orangelogic")
            )
            if is_cdn_pdf and not has_comm_params:
                continue

            is_neutral = _is_host_in_set(host, excluded) or host.endswith(".gov.ru") or host.endswith(".edu")
            if host in SHORTENER_HOSTS or (not is_neutral) or has_strict_aff:
                if u not in commercial_urls:
                    commercial_urls.append(u)
                if host not in SHORTENER_HOSTS and not is_neutral:
                    if u not in website_urls:
                        website_urls.append(u)
        except Exception:
            continue

    # Bare messenger URLs without scheme (e.g. t.me/SovoKino_bot, max.ru/...)
    for m in MESSENGER_CHANNEL_REGEX.finditer(raw_text):
        m_host = m.group(1).lower()
        m_slug = m.group(2).rstrip(".,;:!?)")
        if m_slug and m_slug.lower() not in ("share", "iv", "login", "feed", "im"):
            full_m = f"https://{m_host}/{m_slug}"
            if full_m not in messenger_links and not any(full_m in x for x in messenger_links):
                messenger_links.append(full_m)
                if full_m not in commercial_urls:
                    commercial_urls.append(full_m)

    # Telegram/social @username handles (ignore placeholder annotations or tech mentions)
    at_handles: List[str] = []
    ignored_at = {"everyone", "channel", "here", "author", "username"}
    for m in AT_USERNAME_REGEX.finditer(raw_text):
        handle = "@" + m.group(1)
        if m.group(1).lower() in ignored_at:
            continue
        if handle not in at_handles:
            at_handles.append(handle)

    # Bare domains in text (e.g. svetville.ru)
    text_domains: List[str] = []
    for m in BARE_DOMAIN_REGEX.finditer(raw_text):
        dom = m.group(1).lower()
        if dom in CODE_FILE_OR_EXT_TLDS or _is_host_in_set(dom, excluded) or dom.endswith(".gov.ru") or dom.endswith(".edu"):
            continue
        if page_host and (dom == page_host or dom.endswith("." + page_host)):
            continue
        if dom not in text_domains:
            text_domains.append(dom)

    # Phones and tel: links
    phones: List[str] = []
    for m in PHONE_OR_TEL_REGEX.finditer(raw_text):
        ph = m.group(0).strip()
        if ph not in phones:
            phones.append(ph)

    all_targets: List[str] = []
    for item in commercial_urls + messenger_links + at_handles + text_domains + phones:
        if item not in all_targets:
            all_targets.append(item)

    return {
        "commercial_urls": commercial_urls,
        "website_urls": website_urls,
        "messenger_links": messenger_links,
        "at_handles": at_handles,
        "text_domains": text_domains,
        "phones": phones,
        "all_targets": all_targets,
    }


# =====================================================================
# 4. 168-FZ & 38-FZ ч. 5 ст. 5: UNTRANSLATED FOREIGN WORDS IN COMMERCIAL BLOCKS
# =====================================================================

COMMON_COMMERCIAL_LATIN_WORDS = {
    "sale", "black friday", "discount", "discounts", "best price",
    "super sale", "cashback", "cash back", "special offer", "free",
    "new collection", "buy now", "order now", "shop now", "exclusive",
    "mega sale", "clearance", "hot price", "limited edition", "promo",
}

WHITELIST_TRADEMARKS_AND_TECH = {
    "rub", "usd", "eur", "cny", "http", "https", "www", "com", "ru",
    "org", "net", "tg", "vk", "youtube", "telegram", "whatsapp",
    "iphone", "android", "samsung", "xiaomi", "ai", "b2b", "b2c",
    "seo", "smm", "crm", "erp", "saas", "api", "vpn", "wifi", "wi-fi",
    "bluetooth", "usb", "iso", "sla", "oem", "gost", "hd", "fullhd",
}


def detect_untranslated_foreign_words_168fz(text: str) -> Dict[str, Any]:
    """
    Detect untranslated foreign words in promotional context under 168-FZ and 38-FZ Art. 5(5).
    Excludes technical terms and common registered brand/platform names.
    """
    # Strip URLs so URL query parameters like ?promo=29 or /free/ never trigger 168-FZ prose checks
    text_no_urls = URL_IN_TEXT_REGEX.sub(" ", text)
    text_lower = text_no_urls.lower()
    flagged_phrases = []

    for phrase in COMMON_COMMERCIAL_LATIN_WORDS:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, text_lower):
            flagged_phrases.append(phrase)

    # Also detect isolated Latin words in proximity to currency/percentages
    latin_words = re.findall(r"\b[a-zA-Z]{3,}\b", text_lower)
    unusual_latin = []
    for w in latin_words:
        if w not in WHITELIST_TRADEMARKS_AND_TECH and w not in COMMON_COMMERCIAL_LATIN_WORDS:
            # Check if this word is near numbers or discount symbols
            unusual_latin.append(w)

    has_violation_signals = len(flagged_phrases) > 0
    severity = min(1.0, len(flagged_phrases) * 0.35 + (0.2 if len(unusual_latin) > 3 else 0.0))

    return {
        "has_violation_signals": has_violation_signals,
        "flagged_commercial_phrases": flagged_phrases,
        "unusual_latin_words_count": len(unusual_latin),
        "severity_score": severity,
    }

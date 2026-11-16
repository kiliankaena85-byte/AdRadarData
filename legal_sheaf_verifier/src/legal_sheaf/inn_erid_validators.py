"""
Deterministic Validators for INN, OGRN, erid Tokens, Surrogate Labels, and 168-FZ.
Mathematical checksum validation under Federal Tax Service (FNS) algorithms,
exact regex parsers, and surrogate label detection.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple


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
    re.compile(r"(?i)\berid\s*[:=\s]\s*([a-zA-Z0-9_\-]{8,64})\b"),
    re.compile(r"(?i)\bтокен\s*(?:маркировки)?\s*[:=\s]\s*([a-zA-Z0-9_\-]{8,64})\b"),
    re.compile(r"(?i)\bмаркировка\s*[:=\s]\s*([a-zA-Z0-9_\-]{8,64})\b"),
]

ERID_URL_REGEX = re.compile(r"[?&](?:erid|token)=([a-zA-Z0-9_\-%]+)", re.IGNORECASE)


def validate_erid_token_format(token: str) -> bool:
    """
    Check if a candidate erid string matches ORD token format.
    Typically Base58 / Base64 / alphanumeric string of length 8..64 with non-trivial entropy.
    """
    if not (8 <= len(token) <= 64):
        return False
    if not re.match(r"^[a-zA-Z0-9_\-]+$", token):
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
    1. Clean text body
    2. Page / Target URLs
    3. Platform metadata (e.g. ad_data JSON)
    """
    found_tokens: Set[str] = set()

    # 1. Text extraction
    for rgx in ERID_TEXT_REGEXES:
        for m in rgx.finditer(text):
            tok = m.group(1).strip()
            if validate_erid_token_format(tok):
                found_tokens.add(tok)

    # 2. Page URL query params
    if page_url:
        try:
            parsed = urllib.parse.urlparse(page_url)
            queries = urllib.parse.parse_qs(parsed.query)
            for key in ("erid", "token", "ord_token"):
                for val in queries.get(key, []):
                    clean_val = urllib.parse.unquote(val).strip()
                    if validate_erid_token_format(clean_val):
                        found_tokens.add(clean_val)
        except Exception:
            pass

    # 3. Platform ad metadata (JSON dictionary)
    if platform_ad_meta:
        def _scan_meta(obj: Any):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in ("erid", "token", "ord_token", "marking_token") and isinstance(v, str):
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

STRICT_AD_LABEL_REGEX = re.compile(
    r"(?i)(?:^|[\s,.:;«\"'\(])(реклама|рекламный\s+материал)(?:$|[\s,.:;»\"'\)])"
)

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
    Verify whether the mandatory literal word «Реклама» is present,
    or if illegal surrogate labels are used without the word «Реклама».
    Returns status dictionary.
    """
    text_lower = text.lower()
    has_strict_ad_label = bool(STRICT_AD_LABEL_REGEX.search(text_lower))

    found_surrogates = []
    for surr in SURROGATE_LABELS:
        if surr in text_lower:
            found_surrogates.append(surr)

    # Illegal surrogate: surrogate label is used, but exact word «Реклама» is missing!
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
    text_lower = text.lower()
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

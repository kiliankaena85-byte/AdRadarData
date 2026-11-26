"""
Dual-Tier Audit Engine (DualAuditor).
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and actual Russian regulatory practice.

Provides unified two-tier audit:
1. Article Audit (audit_article):
   - context_mode="EXTERNAL_PUBLICATION"
   - Audits publications on VC.ru, Habr, media, blogs for 38-FZ violations:
     * UNMARKED_AD_NO_ERID (covert ad, affiliate links, promo codes without erid)
     * INCOMPLETE_MARKING_LABEL_OR_INN (surrogate labels without erid or INN)
     * OVER_MARKED_NON_AD (overmarking clean analytics, losing reach)
     * CLEAN_MARKED_AD / CLEAN_NON_AD
   - Generates locked findings via TripleEvidenceLock.

2. Multi-Page Site Audit (audit_site):
   - context_mode="OWN_WEBSITE"
   - Crawls up to 6 key pages (/, /contacts, /policy, /oferta, /promo, /blog)
   - Own catalog / prices / buy buttons are strictly non-ad (Part 2-3 Art. 2 38-FZ)
   - Checks superlatives ("№1", "лучший") without footnote with objective criteria -> RED fine
   - Audits 152-FZ web forms, unbundled consents (156-FZ), GTM Deep-Inspect
   - Audits foreign authorization (Google, Apple, Telegram widget -> Art. 13.55 KoAP moratorium until 2029)
   - Audits Mintsifry IT-accreditation disclosure (Order 511)
   - Audits 168-FZ B2C foreign language use
   - If /blog or /news contains external commercial/affiliate links without erid -> RED fine
   - Company Identification: extracts 10/12-digit INN, detects 9909... prefix as FOREIGN_KIO_ENTITY
   - Live query to GIR BO FNS (bo.nalog.gov.ru) for official revenue
   - Live query to RKN Operator Registry (pd.rkn.gov.ru). If collecting PD but not in registry -> Part 10 Art. 13.11 RED fine
   - Calculates non-multiplied penalties via KoAPSanctionsCalculator (Part 5 Art. 4.4 KoAP).

3. VC.ru Unified Hunter (hunt_vcru):
   - Scans N articles from VC.ru
   - Identifies advertiser domains and groups multiple articles for the SAME domain into ONE consolidated company report
   - Produces structured leads and reports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import ADRADAR_DATA_DIR, ARTIFACTS_DIR, MODELS_CACHE_DIR
from legal_sheaf.registry_cache import (
    get_cached_registry,
    save_cached_registry,
    enforce_fns_rate_limit_async,
)
from legal_sheaf.net_safety import safe_fetch_with_redirects, is_safe_url, SSRFBlockedError
from legal_sheaf.batch_checkpoints import get_checkpoint_store
from legal_sheaf.enforcement_reality import (
    EnforcementTier,
    EvidenceLockViolation,
    EvidenceWitness,
    TripleEvidenceLock,
    classify_finding,
    split_findings_by_traffic_light,
)
from legal_sheaf.inn_erid_validators import (
    AFFILIATE_REGEX,
    NEUTRAL_PLATFORM_HOSTS,
    PROMO_CODE_REGEX,
    SHORTENER_HOSTS,
    _is_host_in_set,
    analyze_ad_label_status,
    detect_untranslated_foreign_words_168fz,
    extract_commercial_targets,
    extract_erid_tokens,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    has_explicit_commercial_tracking,
    validate_inn_checksum,
)
from legal_sheaf.factual_verifiers import (
    verify_article_links_live,
    verify_per_link_erid_coverage,
)
from legal_sheaf.forensics_v3 import (
    detect_instant_protocol_triggers,
    detect_missing_seller_requisites,
    detect_policy_domain_and_entity_paradox,
    detect_regalia_and_pdf_chronology_mismatch,
    discover_perimeter_hosts,
    discover_related_subdomains,
    extract_root_domain,
    inspect_dns_mx_and_host_geo,
    inspect_external_js_endpoints,
    inspect_infrastructure_geo_and_mx,
    meta_extremist_vs_blocked_rule,
    policy_and_terms_forensics,
    resolve_phoenix_clones_in_fns,
)
from legal_sheaf.chronotope import TemporalValidityGate
from legal_sheaf.it_accreditation import ITAccreditationAuditor
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.link_features import detect_banned_or_special_context, extract_link_context_windows
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput, LegalRegime, OffenseContinuity
from legal_sheaf.vega_grey_zone import evaluate_grey_zone_with_vega
from legal_sheaf.vendor_chain import KNOWN_INFRA_VENDORS, STATIC_CDNS, audit_vendor_supply_chain

logger = logging.getLogger("legal_sheaf.dual_auditor")

COMMON_PLATFORMS: Set[str] = {
    "vc.ru", "t.me", "telegram.org", "telegram.me", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru", "github.com",
    "wikipedia.org", "google.com", "yandex.ru", "ya.ru", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "rutube.ru", "tenchat.ru", "pikabu.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com", "spacex.com"
}

# B2B Outreach ICP exclusion list for hunt_vcru (keeps feature extractor NEUTRAL_PLATFORM_HOSTS intact)
HUNT_NON_ICP_HOSTS: Set[str] = NEUTRAL_PLATFORM_HOSTS | COMMON_PLATFORMS | {
    "eaeunion.org", "europa.eu", "pravo.by", "zan.kz", "kremlin.ru", "duma.gov.ru", "gov.ru",
    "kodeks.ru", "teletype.in", "discord.gg", "reddit.com", "twitch.tv", "tiktok.com",
    "wa.me", "whatsapp.com", "viber.com",
    "microsoft.com", "steampowered.com", "steamcommunity.com", "claude.com", "claude.ai",
    "anthropic.com", "openai.com", "deepseek.com", "github.io", "github.blog",
    "vercel.com", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "r2.dev",
    "cloudflare.com", "cloudflareinsights.com", "google.dev", "googleapis.com",
    "blog.google", "googleblog.com",
    "vk.ru", "vkvideo.ru", "ok.ru", "ya.ru", "yandex.com",
    "example.ru", "example.com", "example.org", "example.net", "localhost",
}
HUNT_NON_ICP_SUFFIXES = (".gov.ru", ".gov.by", ".gov.kz", ".gov.ua", ".gov", ".mil", ".edu")

SUPERLATIVE_REGEX = re.compile(
    r"(?i)\b(?:№\s*1|номер\s+(?:один|1)|лучш(?:ий|ая|ее|ие|ую|им|их)|лидер\s+рынка|абсолютный\s+лидер|первый\s+в\s+россии|самый\s+(?:надежный|популярный|выгодный|быстрый|удобный))\b"
)

LABEL_REKLAMA_REGEX = re.compile(
    r"(?:(?i:#реклам(?:а|ный_?материал)\b)|(?:^|[\n.!?»\"(\[])\s*Реклам(?:а|ный\s+материал)\s*(?:[:.!—\-]|$)|\bРеклам(?:а|ный\s+материал)\s+(?:ООО|ИП|АО|ПАО|ЗАО|НАО|НКО|[«\"][А-ЯA-Z])|(?i:\b(?:партнерский\s+материал|на\s+правах\s+рекламы)\b))"
)

ERID_REGEX = re.compile(
    r"(?i)\berid\s*[:=]\s*(?=[a-zA-Z0-9_\-]*\d)(?=[a-zA-Z0-9_\-]*[a-zA-Z])([a-zA-Z0-9_\-]{8,128})\b"
)


def resolve_redirect_url(url: str) -> str:
    """Resolves redirect parameters into clean target destination."""
    try:
        parsed = urllib.parse.urlparse(url)
        if ("vc.ru" in parsed.netloc or "clck.ru" in parsed.netloc) and "redirect" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("to", [None])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return url


def is_affiliate_link(url: str) -> bool:
    """Checks if link has referral or affiliate parameters."""
    return bool(AFFILIATE_REGEX.search(url))


_EMAIL_EXTRACT_REGEX = re.compile(r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b")
_TG_HANDLE_EXTRACT_REGEX = re.compile(r"(?i)https?://(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,32})(?:[/?#\s\"'<>]|$)")
_CEO_TITLE_REGEX = re.compile(
    r"(Генеральный\s+директор|Исполнительный\s+директор|Директор|Руководитель|Управляющий)\s*[:\s—\-]+\s*([А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{2,25})"
)
_IP_FIO_REGEX = re.compile(
    r"(?:Индивидуальный\s+предприниматель|ИП)\s+([А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{1,25}\s+[А-ЯЁ][а-яё]{2,25})"
)
_PATRONYMIC_CHECK_REGEX = re.compile(r"(?i)^[А-ЯЁ][а-яё]+(?:ович|евич|ич|овна|евна|ична)$")
_JUNK_EMAIL_MARKERS = (
    "noreply", "no-reply", "donotreply", "example", "sentry", "wixpress",
    "yourdomain", "domain.com", "domain.ru", "support@vc.ru",
)
_JUNK_EMAIL_EXTS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".css", ".js", ".woff", ".ttf", ".ico")
_TG_RESERVED_SLUGS = {"share", "iv", "joinchat", "addstickers", "proxy", "socks", "login", "s", "botfather"}


def _extract_site_contacts_and_ceo(
    inspected_pages: List[Tuple[str, str, str]],
    clean_domain: str,
) -> Dict[str, Any]:
    """
    Deterministically extracts corporate emails, Telegram handles, and director/IP FIO
    from crawled site pages in strict priority order: contacts -> policy -> oferta -> home.
    """
    prio_map = {"contacts": 0, "policy": 1, "oferta": 2, "home": 3}
    ordered_pages = sorted(inspected_pages, key=lambda p: prio_map.get(p[0], 4))
    root_dom = extract_root_domain(clean_domain) or clean_domain.lower()

    raw_emails: List[str] = []
    seen_emails_lower: Set[str] = set()
    tg_handles: List[str] = []
    seen_tg_lower: Set[str] = set()
    management_name: Optional[str] = None
    management_post: Optional[str] = None

    def _add_email(cand: str) -> None:
        e = cand.strip().rstrip(".,;:!?)\"'")
        el = e.lower()
        if not el or el in seen_emails_lower or "@" not in el:
            return
        local_p, dom_p = el.split("@", 1)
        if local_p in ("test", "user", "name", "email", "info@example"):
            return
        if any(mk in el for mk in _JUNK_EMAIL_MARKERS) or el.endswith(_JUNK_EMAIL_EXTS):
            return
        seen_emails_lower.add(el)
        raw_emails.append(e.lower())

    def _add_tg(slug: str) -> None:
        sl = slug.strip()
        if not sl or sl.lower() in _TG_RESERVED_SLUGS or sl.lower() in seen_tg_lower:
            return
        seen_tg_lower.add(sl.lower())
        tg_handles.append(f"@{sl}")

    for p_type, _, p_html in ordered_pages:
        if not p_html:
            continue
        soup = BeautifulSoup(p_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                _add_email(href[7:].split("?")[0])
            else:
                m_tg = _TG_HANDLE_EXTRACT_REGEX.search(href)
                if m_tg:
                    _add_tg(m_tg.group(1))

        page_text = soup.get_text(separator=" ", strip=True)
        for m_em in _EMAIL_EXTRACT_REGEX.finditer(page_text):
            _add_email(m_em.group(1))
        for m_tg in _TG_HANDLE_EXTRACT_REGEX.finditer(page_text):
            _add_tg(m_tg.group(1))

        if not management_name and p_type in ("contacts", "policy", "oferta", "home"):
            m_ceo = _CEO_TITLE_REGEX.search(page_text)
            if m_ceo:
                post_cand = m_ceo.group(1).strip()
                fio_cand = m_ceo.group(2).strip()
                toks = fio_cand.split()
                if len(toks) == 3 and _PATRONYMIC_CHECK_REGEX.match(toks[2]):
                    management_name = fio_cand
                    management_post = post_cand
            if not management_name:
                m_ip = _IP_FIO_REGEX.search(page_text)
                if m_ip:
                    fio_cand = m_ip.group(1).strip()
                    toks = fio_cand.split()
                    if len(toks) == 3 and _PATRONYMIC_CHECK_REGEX.match(toks[2]):
                        management_name = fio_cand
                        management_post = "Индивидуальный предприниматель"

    # Sort emails: own domain addresses first, then remaining corporate addresses
    own_emails = [e for e in raw_emails if root_dom and (e.endswith("@" + root_dom) or e.endswith("." + root_dom))]
    other_emails = [e for e in raw_emails if e not in own_emails]
    corporate_emails = (own_emails + other_emails)[:5]

    return {
        "corporate_emails": corporate_emails,
        "telegram_contacts": tg_handles[:3],
        "management_name": management_name,
        "management_post": management_post,
    }


def _strip_markup(raw: Any) -> str:
    """Removes inline HTML highlight tags (e.g. from GIR BO responses) and trims whitespace."""
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(raw))).strip()


_KNOWN_VENDOR_INN_OWNERS: Dict[str, Set[str]] = {
    "7801451618": {"beget.com", "beget.ru", "beget.tech"},      # ООО "БЕГЕТ"
    "7842100083": {"timeweb.com", "timeweb.ru"},                # ООО "ТаймВэб"
    "7733568767": {"reg.ru", "reg.com"},                        # ООО "Рег.ру"
    "7810962785": {"selectel.ru", "selectel.com"},              # АО "Селектел"
    "7750005725": {"yoomoney.ru", "yookassa.ru"},               # НКО "ЮМани"
    "7707083893": {"sberbank.ru", "sber.ru"},                   # ПАО Сбербанк
    "7710140679": {"tbank.ru", "tinkoff.ru"},                   # АО "ТБанк"
    "7708806062": {"cloudpayments.ru"},                         # ООО "КлаудПэйментс"
    "7728168971": {"alfabank.ru"},                              # АО "Альфа-Банк"
    "7736207543": {"yandex.ru", "ya.ru", "yandex.com"},         # ООО "ЯНДЕКС"
    "7743001840": {"vk.com", "vk.ru", "mail.ru"},               # ООО "ВК"
}
for _v_host, _v_meta in KNOWN_INFRA_VENDORS.items():
    _v_inn = _v_meta.get("inn")
    if _v_inn:
        _v_root = extract_root_domain(_v_host) or _v_host.lower()
        _KNOWN_VENDOR_INN_OWNERS.setdefault(str(_v_inn), set()).add(_v_root)

_KNOWN_VENDOR_OGRN_OWNERS: Dict[str, Set[str]] = {
    "1027700229193": {"yandex.ru", "ya.ru", "yandex.com"},
    "1077847645590": {"beget.com", "beget.ru", "beget.tech"},
    "1027739850962": {"vk.com", "vk.ru", "mail.ru"},
}

_THIRD_PARTY_INN_CONTEXT_RE = re.compile(
    r"(?i)(?:яндекс[.\s]*метрик|ооо\s*[«\"]яндекс[»\"]|хостинг|хостер|бегет|таймвеб|reg\.ru|селектел|"
    r"selectel|timeweb|beget|субобработчик|третьим\s+лицам|третьих\s+лиц|поручен|платежн|эквайринг|"
    r"оператор\s+фискальных|\bофд\b|tilda|тильда|живой\s+сайт|jivo|робокасс|юкасс|юмани)"
)
_OWNER_INN_CONTEXT_RE = re.compile(
    r"(?i)(?:оператор(?:ом)?\s+персональных\s+данных|продавец|исполнитель|владелец\s+сайта|"
    r"реквизиты\s+(?:оператора|исполнителя|продавца|компании|организации)|наименование\s+организации)"
)
_IP_OWNER_SIGNAL_RE = re.compile(
    r"(?i)(?:индивидуальн\w+\s+предпринимател|\bогрнип\b|\bип\s+[А-ЯЁ][а-яё]+)"
)


def _extract_own_company_inns(
    inspected_pages: List[Tuple[str, str, str]],
    clean_domain: str,
) -> Dict[str, Any]:
    """
    Extracts the site owner's INN/OGRN/KIO with strict priority contacts -> oferta -> home -> policy,
    filtering out third-party infra/hoster/payment subprocessors (Yandex, Beget, Timeweb, etc.).
    """
    prio_map = {"contacts": 0, "oferta": 1, "home": 2, "promo": 3, "blog": 4, "policy": 5}
    ordered_pages = sorted(inspected_pages, key=lambda p: prio_map.get(p[0], 6))
    root_dom = (extract_root_domain(clean_domain) or clean_domain).lower()

    page_plains: List[Tuple[str, str]] = []
    for p_type, _, p_html in ordered_pages:
        txt = BeautifulSoup(p_html, "html.parser").get_text(separator="\n", strip=True) if p_html else ""
        page_plains.append((p_type, txt))

    non_policy_plain = re.sub(r"\s+", " ", " ".join(txt for pt, txt in page_plains if pt != "policy")).strip()
    combined_text = re.sub(r"\s+", " ", " ".join(txt for _, txt in page_plains)).strip()

    def _is_own_occurrence(val: str, page_text: str) -> bool:
        pos_hits = list(re.finditer(rf"(?<!\d){re.escape(val)}(?!\d)", page_text))
        if not pos_hits:
            return False
        for m in pos_hits:
            raw_before = page_text[max(0, m.start() - 120) : m.start()]
            raw_after = page_text[m.end() : min(len(page_text), m.end() + 45)]
            clause_before = re.split(r"[\r\n;!?]|\.\s+[А-ЯЁA-Z]", raw_before)[-1]
            clause_after = re.split(r"[\r\n;!?]|\.\s+[А-ЯЁA-Z]", raw_after)[0]
            ctx_clause = f"{clause_before} {clause_after}"
            ctx_near = clause_before[-90:]
            if _THIRD_PARTY_INN_CONTEXT_RE.search(ctx_clause) and not _OWNER_INN_CONTEXT_RE.search(ctx_near):
                continue
            return True
        return False

    non_policy_inns: List[Tuple[str, int]] = []
    policy_inns: List[Tuple[str, int]] = []
    seen_inn: Set[str] = set()

    non_policy_ogrns: List[Tuple[str, int]] = []
    policy_ogrns: List[Tuple[str, int]] = []
    seen_ogrn: Set[str] = set()

    for p_type, p_txt in page_plains:
        if not p_txt:
            continue
        for inn_val, inn_len in find_valid_inns_in_text(p_txt):
            if inn_val in seen_inn:
                continue
            owner_domains = _KNOWN_VENDOR_INN_OWNERS.get(inn_val)
            if owner_domains and root_dom not in owner_domains:
                continue
            if not _is_own_occurrence(inn_val, p_txt):
                continue
            seen_inn.add(inn_val)
            if p_type == "policy":
                policy_inns.append((inn_val, inn_len))
            else:
                non_policy_inns.append((inn_val, inn_len))

        for ogrn_val, ogrn_len in find_valid_ogrns_in_text(p_txt):
            if ogrn_val in seen_ogrn:
                continue
            owner_domains_o = _KNOWN_VENDOR_OGRN_OWNERS.get(ogrn_val)
            if owner_domains_o and root_dom not in owner_domains_o:
                continue
            if not _is_own_occurrence(ogrn_val, p_txt):
                continue
            seen_ogrn.add(ogrn_val)
            if p_type == "policy":
                policy_ogrns.append((ogrn_val, ogrn_len))
            else:
                non_policy_ogrns.append((ogrn_val, ogrn_len))

    found_inns = non_policy_inns if non_policy_inns else policy_inns
    found_ogrns = non_policy_ogrns if non_policy_ogrns else policy_ogrns

    has_ip_signal = any(l == 15 for _, l in found_ogrns) or bool(
        _IP_OWNER_SIGNAL_RE.search(non_policy_plain or combined_text)
    )

    def _pick_inn(candidates: List[Tuple[str, int]]) -> Optional[str]:
        i10 = next((inn for inn, l in candidates if l == 10 and validate_inn_checksum(inn)), None)
        i12 = next((inn for inn, l in candidates if l == 12 and validate_inn_checksum(inn)), None)
        if has_ip_signal and i12:
            return i12
        return i10 or i12

    valid_inn = _pick_inn(non_policy_inns) or _pick_inn(policy_inns)

    # Check 9909... KIO foreign entity pattern on non-policy pages first
    policy_plain = " ".join(txt for pt, txt in page_plains if pt == "policy")
    is_foreign_kio = False
    kio_candidates = re.findall(r"\b(9909\d{6})\b", non_policy_plain) if non_policy_plain else []
    if not kio_candidates and not non_policy_inns and policy_plain:
        kio_candidates = re.findall(r"\b(9909\d{6})\b", policy_plain)
    if kio_candidates:
        valid_inn = kio_candidates[0]
        is_foreign_kio = True

    valid_ogrn = found_ogrns[0][0] if found_ogrns else None
    return {
        "valid_inn": valid_inn,
        "valid_ogrn": valid_ogrn,
        "is_foreign_kio": is_foreign_kio,
        "found_inns": found_inns,
        "found_ogrns": found_ogrns,
        "combined_text": combined_text,
        "non_policy_plain": non_policy_plain,
    }


_GENERIC_VC_SUBSITE_SLUGS: Set[str] = {
    "u", "marketing", "services", "tech", "finance", "design", "ai", "hr",
    "trade", "legal", "life", "media", "tribuna", "offtop", "flood", "dev",
    "crypto", "travel", "food", "auto", "realty", "education", "seo", "smm",
    "pr", "ads", "money", "invest", "career", "story", "news", "claim",
}


def _timestamp_to_iso_date(raw_val: Any) -> Optional[str]:
    """Converts Unix timestamp or ISO-like string to YYYY-MM-DD."""
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, (int, float)):
        ts = float(raw_val)
        if ts <= 0:
            return None
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None
    if isinstance(raw_val, str):
        s = raw_val.strip()
        if not s:
            return None
        if s.isdigit():
            return _timestamp_to_iso_date(int(s))
        m_iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", s)
        if m_iso:
            return m_iso.group(1)
    return None


def _extract_vcru_block_captions_and_links(blocks: List[Dict[str, Any]]) -> Tuple[List[str], List[str], str]:
    """
    Extracts plain text, media/image captions, and resolved links from VC.ru JSON blocks.
    Captures erid/ИНН/Реклама placements hidden in image captions or link cards.
    """
    text_parts: List[str] = []
    links: List[str] = []
    seen_links: Set[str] = set()
    first_header = ""

    def _add_link(raw_href: str) -> None:
        clean_h = resolve_redirect_url((raw_href or "").strip())
        if clean_h and clean_h not in seen_links:
            seen_links.add(clean_h)
            links.append(clean_h)

    def _ingest_html_or_text(raw_val: Any, is_header: bool = False) -> None:
        nonlocal first_header
        if not isinstance(raw_val, str) or not raw_val.strip():
            return
        soup = BeautifulSoup(raw_val, "html.parser")
        plain = soup.get_text(separator=" ", strip=True)
        if plain:
            text_parts.append(plain)
            if not first_header and (is_header or soup.find(["h1", "h2"])):
                first_header = plain[:160]
        for a in soup.find_all("a", href=True):
            _add_link(a["href"])

    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        btype = str(b.get("type") or "").lower()
        bdata = b.get("data", {}) or {}
        if not isinstance(bdata, dict):
            continue

        _ingest_html_or_text(bdata.get("text", ""), is_header=(btype == "header"))
        _ingest_html_or_text(bdata.get("caption", ""))
        _ingest_html_or_text(bdata.get("title", ""), is_header=(btype == "header"))
        _ingest_html_or_text(bdata.get("description", ""))

        items = bdata.get("items") or []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                _ingest_html_or_text(it.get("title", ""))
                _ingest_html_or_text(it.get("caption", ""))
                img_obj = it.get("image")
                if isinstance(img_obj, dict):
                    img_data = img_obj.get("data")
                    if isinstance(img_data, dict):
                        _ingest_html_or_text(img_data.get("caption", ""))

        link_meta = bdata.get("link")
        if isinstance(link_meta, dict):
            _add_link(link_meta.get("url") or "")
            _ingest_html_or_text(link_meta.get("title", ""))
            _ingest_html_or_text(link_meta.get("description", ""))
        elif isinstance(bdata.get("url"), str):
            _add_link(bdata["url"])

    return text_parts, links, first_header


def _detect_own_corporate_blog(
    url: str,
    author: str,
    subsite: str,
    commercial_urls: List[str],
) -> Dict[str, Any]:
    """
    Detects whether an article is published on the advertiser's own corporate blog (Part 2-3 Art. 2 38-FZ)
    where all commercial links point solely to the author's/subsite's own domain.
    """
    target_roots: List[str] = []
    for u in commercial_urls or []:
        try:
            netloc = urllib.parse.urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
            if not netloc:
                continue
            if _is_host_in_set(netloc, NEUTRAL_PLATFORM_HOSTS) or _is_host_in_set(netloc, SHORTENER_HOSTS):
                continue
            root = extract_root_domain(netloc) or netloc
            if root and root not in target_roots:
                target_roots.append(root)
        except Exception:
            continue

    author_norm = re.sub(r"[^a-zа-яё0-9]+", "", (author or "").lower())
    subsite_norm = re.sub(r"[^a-zа-яё0-9]+", "", (subsite or "").lower())

    url_subsite_norm = ""
    try:
        path_parts = [p for p in urllib.parse.urlparse(url).path.lower().split("/") if p]
        if path_parts and not path_parts[0].isdigit() and path_parts[0] not in _GENERIC_VC_SUBSITE_SLUGS:
            url_subsite_norm = re.sub(r"[^a-z0-9]+", "", path_parts[0])
    except Exception:
        pass

    matched_roots: List[str] = []
    match_source = ""
    for root in target_roots:
        stem = re.sub(r"[^a-z0-9]+", "", root.split(".")[0].lower())
        if len(stem) < 4:
            continue
        if author_norm and author_norm != "неуказан" and stem in author_norm:
            matched_roots.append(root)
            match_source = match_source or "author"
        elif subsite_norm and stem in subsite_norm:
            matched_roots.append(root)
            match_source = match_source or "subsite"
        elif url_subsite_norm and stem == url_subsite_norm:
            matched_roots.append(root)
            match_source = match_source or "url_slug"

    is_single_own_target = len(target_roots) == 1 and len(matched_roots) == 1
    return {
        "is_own_blog": is_single_own_target,
        "matched_domain": matched_roots[0] if is_single_own_target else None,
        "match_source": match_source if is_single_own_target else None,
        "target_roots": target_roots,
    }


class DualAuditor:
    """
    Unified Dual-Tier Compliance Auditor for Articles and Corporate Websites.
    """

    def __init__(
        self,
        bundle: Optional[AdRadarBrainBundle] = None,
        koap_calculator: Optional[KoAPSanctionsCalculator] = None,
        scanner_152: Optional[Compliance152FZScanner] = None,
        vega_sensor: Optional[Any] = None,
        enable_vega_grey_zone: bool = False,
        enable_live_link_verifier: bool = False,
    ):
        if bundle is not None:
            self.bundle = bundle
        else:
            bundle_path = MODELS_CACHE_DIR / "champion_v3.bundle"
            if not bundle_path.exists() and (ARTIFACTS_DIR / "champion_v3.bundle").exists():
                bundle_path = ARTIFACTS_DIR / "champion_v3.bundle"
            if bundle_path.exists():
                self.bundle = AdRadarBrainBundle.load(bundle_path)
            else:
                self.bundle = AdRadarBrainBundle.create_from_artifacts()

        self.koap_calculator = koap_calculator or KoAPSanctionsCalculator()
        self.scanner_152 = scanner_152 or Compliance152FZScanner()
        self.vega_sensor = vega_sensor
        self.enable_vega_grey_zone = enable_vega_grey_zone
        self.enable_live_link_verifier = enable_live_link_verifier

    # -------------------------------------------------------------------------
    # TIER 1: ARTICLE AUDIT (EXTERNAL_PUBLICATION)
    # -------------------------------------------------------------------------
    async def audit_article(
        self,
        url: str,
        text: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        raw_links: Optional[List[str]] = None,
        title_hint: Optional[str] = None,
        author_hint: Optional[str] = None,
        platform_ad_meta_hint: Optional[Dict[str, Any]] = None,
        verify_links_live: Optional[bool] = None,
        link_probe_cache: Optional[Dict[str, Dict[str, Any]]] = None,
        published_at_hint: Optional[str] = None,
        subsite_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Audits an article or media publication under 38-FZ (EXTERNAL_PUBLICATION mode).
        Extracts erid, referral links, promo codes, advertising labels, and applies neural sheaf model.
        """
        close_client = False
        if client is None:
            client = httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
                },
                timeout=12.0,
                follow_redirects=True,
            )
            close_client = True

        try:
            raw_title = (title_hint or "").strip()
            title = "" if raw_title == "Без заголовка" else raw_title
            author = (author_hint or "").strip() or "Не указан"
            subsite_name = (subsite_hint or "").strip()
            published_at_iso = _timestamp_to_iso_date(published_at_hint)
            article_text = text or ""
            platform_ad_meta: Dict[str, Any] = (
                dict(platform_ad_meta_hint) if isinstance(platform_ad_meta_hint, dict) else {}
            )
            is_promo_platform_flag = bool(platform_ad_meta)
            commercial_links: List[str] = [
                resolve_redirect_url(u.strip())
                for u in (raw_links or [])
                if isinstance(u, str) and u.strip()
            ]

            # 1. Fetch content if text is not provided
            if not article_text:
                # Check if VC.ru URL with article ID
                m_vc = re.search(r"vc\.ru/(?:[a-zA-Z0-9_\-]+/)?(\d+)", url)
                if m_vc:
                    post_id = m_vc.group(1)
                    api_url = f"https://api.vc.ru/v2.8/content?id={post_id}"
                    try:
                        resp = await client.get(api_url, timeout=8.0)
                        if resp.status_code == 200:
                            data = resp.json().get("result", {})
                            entry = data.get("entry") or data
                            title = str(entry.get("title") or "").strip()
                            subsite_obj = entry.get("subsite") if isinstance(entry.get("subsite"), dict) else {}
                            subsite_name = str(subsite_obj.get("name") or subsite_name or "").strip()
                            author_obj = entry.get("author") if isinstance(entry.get("author"), dict) else {}
                            author = str(author_obj.get("name") or subsite_name or "Не указан").strip()
                            if not published_at_iso:
                                published_at_iso = _timestamp_to_iso_date(entry.get("date"))
                            ad_raw = entry.get("ad_data") or entry.get("ad") or {}
                            if isinstance(ad_raw, dict):
                                platform_ad_meta = ad_raw
                            is_promo_platform_flag = bool(entry.get("isPromo") or entry.get("is_promo") or ad_raw or entry.get("isFlash"))
                            blocks = entry.get("blocks", [])
                            text_parts, block_links, first_header = _extract_vcru_block_captions_and_links(blocks)
                            commercial_links.extend(block_links)
                            article_text = " ".join(text_parts).strip()
                            if not title:
                                title = first_header
                    except Exception as e:
                        logger.warning(f"Failed to fetch VC.ru API v2.8 for post {post_id}: {e}")

                # Fallback to direct HTML fetching
                if not article_text:
                    resp = await client.get(url, timeout=8.0)
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        h1 = soup.find("h1")
                        if h1:
                            title = h1.get_text().strip()
                        if not published_at_iso:
                            meta_pub = (
                                soup.find("meta", attrs={"property": "article:published_time"})
                                or soup.find("meta", attrs={"name": "publish-date"})
                                or soup.find("time", attrs={"datetime": True})
                            )
                            if meta_pub:
                                raw_dt = meta_pub.get("content") or meta_pub.get("datetime") or meta_pub.get_text(strip=True)
                                published_at_iso = _timestamp_to_iso_date(raw_dt)
                        # Extract article body text
                        article_tag = soup.find("article") or soup.find("main") or soup.body
                        if article_tag:
                            article_text = article_tag.get_text(separator=" ", strip=True)
                            for a in article_tag.find_all("a", href=True):
                                commercial_links.append(resolve_redirect_url(a["href"].strip()))
                        else:
                            article_text = soup.get_text(separator=" ", strip=True)

            if not title and article_text:
                first_sent = re.split(r"(?<=[.!?])\s+", article_text.strip())[0]
                title = (first_sent[:120] + "...") if len(first_sent) > 120 else first_sent

            # Filter external commercial links and hidden contacts (t.me bots, max.ru, @handles, domains)
            extracted_targets = extract_commercial_targets(
                article_text,
                raw_links=commercial_links,
                page_url=url,
            )
            filtered_comm_links = list(extracted_targets["commercial_urls"])
            hidden_contacts = (
                extracted_targets["at_handles"]
                + [f"https://{d}" for d in extracted_targets["text_domains"]]
                + extracted_targets["phones"]
            )

            # 2. Extract markers (strict ORD erid & strict ad-label detection)
            erid_tokens = extract_erid_tokens(
                article_text,
                page_url=" ".join([url] + commercial_links),
                platform_ad_meta=platform_ad_meta or None,
            )
            has_erid = len(erid_tokens) > 0
            erid_token = erid_tokens[0] if erid_tokens else None

            has_affiliate = any(
                is_affiliate_link(lnk) or has_explicit_commercial_tracking(lnk, page_url=url)
                for lnk in filtered_comm_links
            ) or bool(AFFILIATE_REGEX.search(article_text))
            strict_affiliate = any(
                is_affiliate_link(lnk)
                for lnk in filtered_comm_links
            ) or bool(AFFILIATE_REGEX.search(article_text))
            has_promo_phrase = bool(PROMO_CODE_REGEX.search(article_text))
            ad_label_info = analyze_ad_label_status(article_text)
            has_label_reklama = bool(ad_label_info["has_strict_ad_label"] or ad_label_info["is_illegal_surrogate"])
            found_inns = find_valid_inns_in_text(article_text)
            has_inn = len(found_inns) > 0

            # 2B. Factual verifiers: Anti-SSRF Redirect/Landing inspection + Per-Link erid Coverage
            do_live_verify = self.enable_live_link_verifier if verify_links_live is None else bool(verify_links_live)
            link_landing_verifications: List[Dict[str, Any]] = []
            revealed_erids_map: Dict[str, str] = {}
            resolved_website_urls: List[str] = list(extracted_targets["website_urls"])
            revealed_redirect_evidence: Optional[str] = None

            if do_live_verify and filtered_comm_links:
                link_landing_verifications = await verify_article_links_live(
                    filtered_comm_links,
                    client=client,
                    article_url=url,
                    max_links=5,
                    timeout=5.0,
                    probe_cache=link_probe_cache,
                )
                for lv in link_landing_verifications:
                    rev_erid = lv.get("hidden_erid_revealed")
                    if rev_erid:
                        revealed_erids_map[lv["original_url"]] = str(rev_erid)
                        if rev_erid not in erid_tokens:
                            erid_tokens.append(str(rev_erid))
                    fin_u = str(lv.get("final_url") or "").strip()
                    fin_h = str(lv.get("final_host") or "").strip().lower()
                    if (
                        fin_u.startswith(("http://", "https://"))
                        and fin_h
                        and fin_h not in SHORTENER_HOSTS
                        and not _is_host_in_set(fin_h, NEUTRAL_PLATFORM_HOSTS)
                        and not fin_h.endswith((".gov.ru", ".edu"))
                        and fin_u not in resolved_website_urls
                    ):
                        resolved_website_urls.append(fin_u)
                has_erid = len(erid_tokens) > 0
                erid_token = erid_tokens[0] if erid_tokens else None

                for lv in link_landing_verifications:
                    if (
                        lv.get("hidden_affiliate_network_detected")
                        and not lv.get("hidden_erid_revealed")
                        and not has_erid
                    ):
                        strict_affiliate = True
                    if (
                        (lv.get("hidden_affiliate_network_detected") or lv.get("hidden_tracking_revealed"))
                        and not lv.get("hidden_erid_revealed")
                        and not has_erid
                    ):
                        has_affiliate = True
                        if not revealed_redirect_evidence:
                            revealed_redirect_evidence = f"{lv['original_url']} -> {lv['final_url']}"

            per_link_erid_audit = verify_per_link_erid_coverage(
                text=article_text,
                commercial_urls=filtered_comm_links,
                raw_links=commercial_links,
                page_url=url,
                platform_ad_meta=platform_ad_meta or None,
                extra_url_erids=revealed_erids_map,
            )

            # 3. Neural sheaf model prediction in EXTERNAL_PUBLICATION mode
            author_domain = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
            if len(article_text) <= 8000:
                doc_clean_text = article_text
            else:
                doc_clean_text = f"{article_text[:3500]}\n...\n{article_text[-4000:]}"
            # Ensure anchor-extracted commercial URLs are visible to feature_builder
            missing_hrefs = [u for u in filtered_comm_links[:8] if u not in doc_clean_text]
            if missing_hrefs:
                doc_clean_text = f"{doc_clean_text}\n[Ссылки: {' '.join(missing_hrefs)}]"
            doc_in = DocumentInput(
                clean_text=doc_clean_text,
                page_url=url,
                author_domain=author_domain or "vc.ru",
                platform_ad_meta=platform_ad_meta,
                published_at=published_at_iso,
                context_mode="EXTERNAL_PUBLICATION",
            )
            sheaf_report = self.bundle.verify_document(doc_in)

            # 4. Strict 38-FZ Scenario Classification (with Tier 2 Vega-U Grey-Zone Judge & ABSTAIN_REVIEW Guard)
            has_any_comm_channel = bool(filtered_comm_links or hidden_contacts)
            banned_info = detect_banned_or_special_context(doc_clean_text, extracted_targets, is_own_website=False)
            has_banned_special = bool(
                banned_info["has_banned_or_special"]
                or sheaf_report.regime == LegalRegime.BANNED_OR_SPECIAL_REGIME
            )
            vega_grey_zone_resolved = False
            vega_facts: Optional[Dict[str, bool]] = None
            factual_landing_resolved = False
            confirmed_landing_obj: Optional[Dict[str, Any]] = next(
                (lv for lv in link_landing_verifications if lv.get("is_confirmed_commercial_landing")),
                None,
            )

            is_abstain_grey = (
                sheaf_report.conformal_verdict == "ABSTAIN_REVIEW"
                and not has_erid
                and not has_affiliate
                and not has_promo_phrase
                and not has_label_reklama
                and not is_promo_platform_flag
                and not has_banned_special
            )
            if has_banned_special and not (has_erid and ad_label_info["has_strict_ad_label"]):
                scenario = "BANNED_OR_SPECIAL_REGIME"
                is_violation_38 = True
                verdict = "Продвижение запрещенного объекта (VPN, криптообмен/криптобиржа, казино) или специального режима без обязательных предупреждений (ст. 7, 24, 28 38-ФЗ)"
                primary_sig = "banned_or_special_regime"
            elif (has_label_reklama or is_promo_platform_flag) and (not has_erid or not has_inn):
                scenario = "INCOMPLETE_MARKING_LABEL_OR_INN"
                is_violation_38 = True
                verdict = "Суррогатная пометка: указано 'Реклама' / 'Партнерский материал', но отсутствует токен erid или ИНН рекламодателя (Приказ РКН № 68)"
                primary_sig = "incomplete_marking_label_or_inn"
            elif per_link_erid_audit.get("has_partial_erid_gap"):
                scenario = "PARTIAL_ERID_MARKING_GAP"
                is_violation_38 = True
                gap_sample = (
                    per_link_erid_audit["unmarked_links"]
                    or per_link_erid_audit["invalid_erid_links"]
                    or [""]
                )[0]
                verdict = (
                    f"Частичная маркировка 38-ФЗ: из {per_link_erid_audit['total_commercial_links']} "
                    f"коммерческих ссылок промаркировано {per_link_erid_audit['marked_links_count']}, "
                    f"без валидного erid осталось "
                    f"{per_link_erid_audit['unmarked_links_count'] + per_link_erid_audit['invalid_erid_links_count']} "
                    f"(например: {gap_sample[:90]}; ч. 16 ст. 14.3 КоАП РФ, ч. 17 ст. 18.1 38-ФЗ)"
                )
                primary_sig = "partial_erid_missing_on_link"
            elif (has_affiliate or has_promo_phrase) and not has_erid:
                scenario = "UNMARKED_AD_NO_ERID"
                is_violation_38 = True
                if revealed_redirect_evidence:
                    verdict = (
                        f"Скрытая партнерская реф-ссылка / редирект через трекер без маркировки erid "
                        f"({revealed_redirect_evidence[:120]}; ч. 16 ст. 14.3 КоАП РФ)"
                    )
                else:
                    verdict = "Скрытая интернет-реклама / партнерская реф-ссылка без маркировки токеном erid (ч. 16 ст. 14.3 КоАП РФ)"
                primary_sig = "unmarked_ad_no_erid"
            elif is_abstain_grey and has_any_comm_channel:
                scenario = "ABSTAIN_REVIEW"
                is_violation_38 = False
                verdict = "Пограничный материал (серая зона п. 2-3 ч. 2 ст. 2 38-ФЗ): требуется углубленный разбор контекста ссылки перед квалификацией"
                primary_sig = "abstain_grey_zone"

                if confirmed_landing_obj is not None:
                    factual_landing_resolved = True
                    scenario = "UNMARKED_AD_NO_ERID"
                    is_violation_38 = True
                    quotes_str = "; ".join((confirmed_landing_obj.get("commercial_evidence_quotes") or [])[:2])
                    verdict = (
                        f"Скрытая интернет-реклама без токена erid: целевая посадочная страница "
                        f"{confirmed_landing_obj.get('final_url')} подтверждена как коммерческая "
                        f"({quotes_str or 'цены и кнопка заказа'}; ч. 16 ст. 14.3 КоАП РФ, ст. 18.1 38-ФЗ)"
                    )
                    primary_sig = "unmarked_ad_no_erid"
                elif self.vega_sensor is not None or self.enable_vega_grey_zone:
                    link_windows = list(getattr(sheaf_report, "extracted_link_windows", None) or [])
                    if not link_windows:
                        _, _, link_windows, _ = extract_link_context_windows(doc_clean_text, extracted_targets)
                    tier0_flags = {
                        "has_erid": has_erid,
                        "has_strict_ad_label": ad_label_info["has_strict_ad_label"],
                        "is_promo": is_promo_platform_flag,
                        "has_affiliate_link": has_affiliate,
                        "has_promo_code": has_promo_phrase,
                        "commercial_targets": extracted_targets["all_targets"],
                        "external_links": filtered_comm_links,
                        "has_banned_special": has_banned_special,
                    }
                    vega_res = evaluate_grey_zone_with_vega(
                        sensor=self.vega_sensor,
                        title=title,
                        url=url,
                        external_links=filtered_comm_links,
                        link_windows=link_windows,
                        tier0_flags=tier0_flags,
                    )
                    if vega_res.get("resolved"):
                        vega_grey_zone_resolved = True
                        vega_facts = vega_res.get("facts")
                        v_reg = vega_res["regime"]
                        scenario = v_reg.name
                        is_violation_38 = bool(vega_res["is_violation"])
                        if v_reg == LegalRegime.BANNED_OR_SPECIAL_REGIME:
                            verdict = "Продвижение запрещенного объекта или специального режима без обязательных предупреждений (ст. 7, 24, 28 38-ФЗ)"
                            primary_sig = "banned_or_special_regime"
                        elif v_reg == LegalRegime.UNMARKED_AD_NO_ERID:
                            verdict = "Скрытая интернет-реклама без маркировки токеном erid (подтверждено семантическим анализом окна ссылки, ч. 16 ст. 14.3 КоАП РФ)"
                            primary_sig = "unmarked_ad_no_erid"
                        elif v_reg == LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN:
                            verdict = "Неполная маркировка интернет-рекламы (Приказ РКН № 68)"
                            primary_sig = "incomplete_marking_label_or_inn"
                        else:
                            verdict = "Редакторский / аналитический материал или личный блог без рекламного предложения (п. 2-3 ч. 2 ст. 2 38-ФЗ)"
                            primary_sig = "clean_non_ad"
            elif has_any_comm_channel and sheaf_report.is_violation and not has_erid:
                scenario = "UNMARKED_AD_NO_ERID"
                is_violation_38 = True
                verdict = "Скрытая интернет-реклама / партнерская реф-ссылка без маркировки токеном erid (ч. 16 ст. 14.3 КоАП РФ)"
                primary_sig = "unmarked_ad_no_erid"
            elif has_erid and not has_any_comm_channel and not sheaf_report.is_violation:
                scenario = "OVER_MARKED_NON_AD"
                is_violation_38 = False
                verdict = "Перемаркировка: токен erid размещен на чистом аналитическом посте без объекта рекламирования (потеря охватов)"
                primary_sig = "over_marked_non_ad"
            elif has_erid and has_label_reklama and has_inn:
                scenario = "CLEAN_MARKED_AD"
                is_violation_38 = False
                verdict = "Рекламный материал надлежаще маркирован по ст. 18.1 38-ФЗ"
                primary_sig = "clean_marked_ad"
            else:
                scenario = "CLEAN_NON_AD"
                is_violation_38 = False
                verdict = "Редакторский / аналитический материал (не является рекламой по п. 2-3 ч. 2 ст. 2 38-ФЗ)"
                primary_sig = "clean_non_ad"

            # Own Corporate Blog Ownership Guard (Part 2-3 Art. 2 38-FZ)
            ownership_check = _detect_own_corporate_blog(
                url=url,
                author=author,
                subsite=subsite_name,
                commercial_urls=filtered_comm_links,
            )
            if (
                ownership_check.get("is_own_blog")
                and scenario in ("UNMARKED_AD_NO_ERID", "PARTIAL_ERID_MARKING_GAP", "ABSTAIN_REVIEW")
                and not has_promo_phrase
                and not strict_affiliate
                and not has_erid
                and not has_label_reklama
                and not is_promo_platform_flag
                and not has_banned_special
            ):
                scenario = "OWN_BLOG_INFORMING_GREY"
                is_violation_38 = False
                verdict = (
                    f"Собственный корпоративный блог ({ownership_check.get('matched_domain')}): "
                    f"информирование о собственных товарах и услугах без признаков третьих лиц, "
                    f"промокодов и реферальных параметров (п. 2-3 ч. 2 ст. 2 38-ФЗ — требует ручной проверки на сквозной баннер)"
                )
                primary_sig = "clean_non_ad"

            # Temporal Statute-of-Limitations Guard (Art. 4.5 / Art. 1.7 KoAP RF, 365 days)
            temporal_decision = None
            statute_expired_365d = False
            if published_at_iso:
                temporal_decision = TemporalValidityGate().evaluate(
                    published_at=published_at_iso,
                    offense_type="ad_marking",
                    continuity=OffenseContinuity.COMPLETED,
                )
                if temporal_decision is not None and not temporal_decision.is_punishable:
                    statute_expired_365d = True
                    if is_violation_38:
                        scenario = "STATUTE_EXPIRED_4_5_KOAP"
                        is_violation_38 = False
                        verdict = temporal_decision.rationale
                        primary_sig = "statute_expired_4_5_koap"

            # 5. Form evidence witness and lock findings
            findings_raw = []
            if is_violation_38:
                # Find quote snippet
                quote_text = ""
                quote_start = 0
                quote_end = 0
                if primary_sig == "partial_erid_missing_on_link":
                    gap_list = (
                        per_link_erid_audit.get("unmarked_links")
                        or per_link_erid_audit.get("invalid_erid_links")
                        or filtered_comm_links
                    )
                    quote_text = gap_list[0] if gap_list else url
                elif has_affiliate:
                    quote_text = next((lnk for lnk in filtered_comm_links if is_affiliate_link(lnk)), filtered_comm_links[0] if filtered_comm_links else url)
                elif has_promo_phrase:
                    m_p = PROMO_CODE_REGEX.search(article_text)
                    if m_p:
                        quote_text = m_p.group(0)
                        quote_start = m_p.start()
                        quote_end = m_p.end()
                elif filtered_comm_links:
                    quote_text = filtered_comm_links[0]
                else:
                    quote_text = title[:120]

                extra_snippet_parts = [f"URL: {url}", f"Link: {quote_text[:160]}"]
                if revealed_redirect_evidence:
                    extra_snippet_parts.append(f"Redirect: {revealed_redirect_evidence[:140]}")
                if confirmed_landing_obj is not None:
                    landing_q = "; ".join((confirmed_landing_obj.get("commercial_evidence_quotes") or [])[:2])
                    if landing_q:
                        extra_snippet_parts.append(f"Landing: {landing_q[:160]}")

                witness = EvidenceWitness(
                    source_url=url,
                    line_number=1,
                    raw_snippet=" | ".join(extra_snippet_parts),
                    quote=quote_text,
                    quote_start=quote_start,
                    quote_end=quote_end,
                    notes=verdict,
                )
                classified = classify_finding(primary_sig, context_mode="EXTERNAL_PUBLICATION", witness=witness)
                findings_raw.append(classified)
            elif scenario == "OVER_MARKED_NON_AD":
                witness = EvidenceWitness(
                    source_url=url,
                    line_number=1,
                    raw_snippet=f"erid={erid_token}",
                    quote=erid_token or "erid",
                    notes="Перемаркировка",
                )
                classified = classify_finding(primary_sig, context_mode="EXTERNAL_PUBLICATION", witness=witness)
                findings_raw.append(classified)

            split_tiers = split_findings_by_traffic_light(findings_raw)

            # 6. Sanctions calculation (Part 5 Art. 4.4 KoAP non-multiplication)
            active_signatures = [f["signature"] for f in split_tiers["RED_REAL_FINE"]]
            if not active_signatures and split_tiers["ORANGE_REGULATORY_DEMAND"]:
                active_signatures = [f["signature"] for f in split_tiers["ORANGE_REGULATORY_DEMAND"]]

            is_ip = False
            if found_inns:
                inn_str, inn_len = found_inns[0]
                if inn_len == 12:
                    is_ip = True
            if "caseguru" in author.lower() or any("caseguru" in lk.lower() for lk in filtered_comm_links):
                is_ip = True

            penalties = self.koap_calculator.calculate_penalties(
                active_signatures,
                is_sme=True,
                first_offense=True,
                is_ip=is_ip,
            )

            return {
                "audit_type": "ARTICLE_AUDIT",
                "context_mode": "EXTERNAL_PUBLICATION",
                "article_url": url,
                "title": title,
                "author": author,
                "subsite": subsite_name,
                "published_at": published_at_iso,
                "statute_expired_365d": statute_expired_365d,
                "ownership_check": ownership_check,
                "is_promo_platform_flag": is_promo_platform_flag,
                "has_erid": has_erid,
                "erid_token": erid_token,
                "scenario_38fz": scenario,
                "is_violation_38fz": is_violation_38,
                "verdict_38fz": verdict,
                "commercial_links": filtered_comm_links,
                "website_urls": resolved_website_urls,
                "commercial_targets": extracted_targets["all_targets"],
                "per_link_erid_audit": per_link_erid_audit,
                "link_landing_verifications": link_landing_verifications,
                "factual_landing_resolved": factual_landing_resolved,
                "traffic_light": split_tiers,
                "penalties": penalties,
                "sheaf_energy": sheaf_report.total_sheaf_energy,
                "conformal_verdict": sheaf_report.conformal_verdict,
                "violation_probability": sheaf_report.violation_probability,
                "vega_grey_zone_resolved": vega_grey_zone_resolved,
                "vega_facts": vega_facts,
                "feature_attributions": getattr(sheaf_report, "top_feature_attributions", []),
            }

        finally:
            if close_client:
                await client.aclose()

    # -------------------------------------------------------------------------
    # TIER 2: MULTI-PAGE SITE AUDIT (OWN_WEBSITE)
    # -------------------------------------------------------------------------
    async def audit_site(
        self,
        domain: str,
        client: Optional[httpx.AsyncClient] = None,
        max_pages: int = 6,
        deep: bool = True,
        gear: str = "auto",
    ) -> Dict[str, Any]:
        """
        Audits a company website using a 3-tier cascade:
        - Gear 1 (L0 Fast Triage — <= 0.25s, 0 external registry calls): checks 5 surface markers;
        - Gear 2 (L1 Qualifier — ~0.7s): reads /policy & /contacts, verifies FNS & RKN;
        - Gear 3 (L2 Deep Forensics & Vendor Chain): full cross-forensics and supply-chain graph investigation.
        """
        raw_target = domain.strip().lower()
        raw_target = re.sub(r"^https?://", "", raw_target)
        if "/" in raw_target:
            parts = raw_target.split("/", 1)
            clean_domain = parts[0]
            initial_path = "/" + parts[1].rstrip("/")
            if initial_path == "/":
                initial_path = ""
        else:
            clean_domain = raw_target
            initial_path = ""
        clean_domain = clean_domain.split(":")[0]
        if clean_domain.startswith("www."):
            clean_domain = clean_domain[4:]

        close_client = False
        if client is None:
            client = httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
                },
                timeout=8.0,
                follow_redirects=True,
            )
            close_client = True

        try:
            # 1. Fetch Home Page (HTTPS fallback to HTTP)
            base_url = f"https://{clean_domain}{initial_path}/" if initial_path else f"https://{clean_domain}"
            main_html = None
            final_main_url = base_url

            try:
                resp, final_main_url, _ = await safe_fetch_with_redirects(client, base_url, timeout=6.0)
                if resp and resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                    main_html = resp.text
            except Exception:
                try:
                    fallback_url = f"http://{clean_domain}{initial_path}/" if initial_path else f"http://{clean_domain}"
                    resp, final_main_url, _ = await safe_fetch_with_redirects(client, fallback_url, timeout=6.0)
                    if resp and resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                        main_html = resp.text
                except Exception as e:
                    logger.error(f"Failed to connect to domain {clean_domain}: {e}")
                    return {
                        "audit_type": "SITE_AUDIT",
                        "domain": clean_domain,
                        "status": "CONNECTION_FAILED",
                        "error": str(e),
                        "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                        "penalties": {"total_min_rubles": 0, "total_max_rubles": 0, "regulatory_outcome": "Недоступен"},
                    }

            if not main_html:
                return {
                    "audit_type": "SITE_AUDIT",
                    "domain": clean_domain,
                    "status": "NON_HTML_RESPONSE",
                    "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                    "penalties": {"total_min_rubles": 0, "total_max_rubles": 0, "regulatory_outcome": "Недоступен"},
                }

            # Language detection & auto-switch to Russian version if opened in English
            def _calc_cyrillic_ratio(html_txt: str) -> float:
                clean = re.sub(r"(?is)<(?:script|style|svg)[^>]*>.*?</(?:script|style|svg)>", " ", html_txt[:60000])
                clean = re.sub(r"<[^>]+>", " ", clean)
                cyr = len(re.findall(r"[\u0400-\u04FF]", clean))
                lat = len(re.findall(r"[A-Za-z]", clean))
                tot = cyr + lat
                return (cyr / tot) if tot > 0 else 0.0

            cyr_ratio = _calc_cyrillic_ratio(main_html)
            html_lang_match = re.search(r'<html[^>]*lang=["\']([^"\']+)["\']', main_html[:2000], re.I)
            html_lang = html_lang_match.group(1).lower() if html_lang_match else ""
            is_russian_page = html_lang.startswith("ru") or "/ru" in final_main_url or cyr_ratio >= 0.2
            detected_lang = "ru" if is_russian_page else "en"

            # Auto-switch: if English detected and not already on /ru path, check for Russian alternate/links
            if not is_russian_page and "/ru" not in final_main_url:
                ru_candidates = []
                for alt_match in re.finditer(r'<link[^>]+rel=["\']alternate["\'][^>]+hreflang=["\']ru(?:-[a-z]+)?["\'][^>]+href=["\']([^"\']+)["\']', main_html, re.I):
                    ru_candidates.append(alt_match.group(1))
                for alt_match in re.finditer(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']alternate["\'][^>]+hreflang=["\']ru(?:-[a-z]+)?["\']', main_html, re.I):
                    ru_candidates.append(alt_match.group(1))
                for a_match in re.finditer(r'<a[^>]+href=["\']([^"\']*(?:/ru/|/ru\b)[^"\']*)["\']', main_html, re.I):
                    ru_candidates.append(a_match.group(1))
                ru_candidates.append("/ru/")

                for cand in ru_candidates:
                    try:
                        cand_url = urllib.parse.urljoin(final_main_url, cand)
                        cand_resp = await client.get(cand_url, timeout=5.0)
                        if cand_resp.status_code == 200 and "text/html" in cand_resp.headers.get("content-type", "").lower():
                            cand_cyr = _calc_cyrillic_ratio(cand_resp.text)
                            cand_lang_match = re.search(r'<html[^>]*lang=["\']([^"\']+)["\']', cand_resp.text[:2000], re.I)
                            cand_lang = cand_lang_match.group(1).lower() if cand_lang_match else ""
                            if cand_lang.startswith("ru") or cand_cyr >= 0.2 or cand_cyr > cyr_ratio:
                                logger.info(f"Auto-switched from English landing {final_main_url} to Russian version {cand_url}")
                                main_html = cand_resp.text
                                final_main_url = str(cand_resp.url)
                                detected_lang = "ru"
                                cyr_ratio = cand_cyr
                                p_fin = urllib.parse.urlparse(final_main_url)
                                if p_fin.path and p_fin.path.rstrip("/"):
                                    initial_path = p_fin.path.rstrip("/")
                                break
                    except Exception:
                        pass

            # =================================================================
            # GEAR 1: L0 SURFACE TRIAGE (<= 0.25s, 0 external registry queries)
            # =================================================================
            main_html_256k = main_html[:256 * 1024]
            root_d = extract_root_domain(clean_domain)

            # Marker 1: Commercial site with hidden INN on storefront
            has_commercial_signals = bool(re.search(r"(?i)(?:8\s*800\s*\d{3}|\+7\s*\(?\d{3}\)?|\bзаказать\b|\bкупить\b|\bцена\b|\bруб\b|\bстоимость\b)", main_html_256k))
            inns_on_home = find_valid_inns_in_text(main_html_256k)
            marker_hidden_inn = has_commercial_signals and len(inns_on_home) == 0

            # Marker 2: Non-CDN external scripts or widgets
            ext_script_hosts = set()
            for s_match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']|<iframe[^>]+src=["\']([^"\']+)["\']', main_html_256k, re.I):
                src_val = s_match.group(1) or s_match.group(2) or ""
                if src_val.startswith("http"):
                    try:
                        h_val = urllib.parse.urlparse(src_val).netloc.lower().split(":")[0]
                        if h_val.startswith("www."):
                            h_val = h_val[4:]
                        if h_val and not h_val.endswith("." + root_d) and h_val != root_d and h_val not in STATIC_CDNS:
                            ext_script_hosts.add(h_val)
                    except Exception:
                        pass
            marker_external_scripts = len(ext_script_hosts) > 0

            # Marker 3: Bare forms collecting PD without checkbox or policy link
            has_form = "<form" in main_html_256k.lower()
            has_policy_link = bool(re.search(r"(?i)href=[\"'][^\"']*(?:policy|privacy|конфиденциальн)", main_html_256k))
            has_checkbox = bool(re.search(r'(?i)<input[^>]+type=["\']checkbox["\']', main_html_256k))
            marker_bare_forms = has_form and (not has_policy_link or not has_checkbox)

            # Marker 4: Superlatives or credit without PSK
            marker_superlatives_or_credit = bool(SUPERLATIVE_REGEX.search(main_html_256k) or re.search(r"(?i)(?:рассрочк[а-я]*\s*0\s*%|в\s+кредит\s+от|кредит\s+без\s+переплат)", main_html_256k))

            # Marker 5: External affiliate / promo links
            marker_external_promo_links = bool(AFFILIATE_REGEX.search(main_html_256k) or PROMO_CODE_REGEX.search(main_html_256k))

            l0_markers_count = sum([
                marker_hidden_inn,
                marker_external_scripts,
                marker_bare_forms,
                marker_superlatives_or_credit,
                marker_external_promo_links,
            ])

            # Fast Clean Pass exit (0 external registry queries, instant response)
            if gear == "fast_l0" or (gear == "auto" and not deep and l0_markers_count == 0):
                if l0_markers_count == 0:
                    return {
                        "audit_type": "SITE_AUDIT",
                        "context_mode": "OWN_WEBSITE",
                        "domain": clean_domain,
                        "main_url": final_main_url,
                        "final_url": final_main_url,
                        "detected_lang": detected_lang,
                        "cyrillic_ratio": round(cyr_ratio, 3),
                        "cascade_stage": "L0_SURFACE_TRIAGE",
                        "triage_verdict": "FAST_CLEAN_PASS",
                        "l0_markers_count": 0,
                        "pages_inspected_count": 1,
                        "pages_inspected": [final_main_url],
                        "company_profile": {
                            "inn": inns_on_home[0][0] if inns_on_home else None,
                            "legal_name": f"Компания ({clean_domain})",
                            "entity_type": "LEGAL_ENTITY" if inns_on_home else "UNKNOWN",
                            "is_sme": True,
                        },
                        "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                        "penalties": {
                            "total_min_rubles": 0,
                            "total_max_rubles": 0,
                            "first_contact_outcome": "COMPLIANT",
                            "regulatory_outcome": "Чисто (L0 Fast Pass)",
                        },
                    }
                elif gear == "fast_l0":
                    return {
                        "audit_type": "SITE_AUDIT",
                        "context_mode": "OWN_WEBSITE",
                        "domain": clean_domain,
                        "main_url": final_main_url,
                        "final_url": final_main_url,
                        "detected_lang": detected_lang,
                        "cyrillic_ratio": round(cyr_ratio, 3),
                        "cascade_stage": "L0_SURFACE_TRIAGE",
                        "triage_verdict": "ESCALATE_TO_L1",
                        "l0_markers_count": l0_markers_count,
                        "markers": {
                            "hidden_inn": marker_hidden_inn,
                            "external_scripts": marker_external_scripts,
                            "bare_forms": marker_bare_forms,
                            "superlatives_or_credit": marker_superlatives_or_credit,
                            "external_promo_links": marker_external_promo_links,
                        },
                        "pages_inspected_count": 1,
                        "pages_inspected": [final_main_url],
                        "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                        "penalties": {"total_min_rubles": 0, "total_max_rubles": 0, "regulatory_outcome": "Требует L1"},
                    }

            # 2. Discover up to 5 additional subpages (total <= max_pages): contacts, policy, oferta, promo, blog
            soup_main = BeautifulSoup(main_html, "html.parser")
            subpage_targets: Dict[str, Optional[str]] = {
                "contacts": None,
                "policy": None,
                "oferta": None,
                "promo": None,
                "blog": None,
            }

            footer_anchors = []
            other_anchors = []
            for a in soup_main.find_all("a", href=True):
                if a.find_parent("footer") or "footer" in str(a.get("class", "")) or "footer" in str(a.parent.get("class", "")):
                    footer_anchors.append(a)
                else:
                    other_anchors.append(a)

            for a in (footer_anchors + other_anchors):
                h = a["href"].strip()
                anchor_text = a.get_text().strip().lower()
                combo = f"{h} {anchor_text}".lower()

                full_sub = urllib.parse.urljoin(final_main_url, h)
                clean_sub, _ = urllib.parse.urldefrag(full_sub)
                p_sub = urllib.parse.urlparse(clean_sub)
                if p_sub.netloc.lower() != urllib.parse.urlparse(final_main_url).netloc.lower():
                    continue
                if clean_sub.rstrip("/") == final_main_url.rstrip("/"):
                    continue

                if not subpage_targets["contacts"] and any(k in combo for k in ["контакт", "contact", "рекв", "req", "about", "о нас"]):
                    subpage_targets["contacts"] = clean_sub
                elif not subpage_targets["policy"] and any(k in combo for k in ["политик", "policy", "privacy", "персонал", "согласи"]):
                    subpage_targets["policy"] = clean_sub
                elif not subpage_targets["oferta"] and any(k in combo for k in ["оферт", "oferta", "terms", "договор", "rules"]):
                    subpage_targets["oferta"] = clean_sub
                elif not subpage_targets["promo"] and any(k in combo for k in ["акци", "promo", "скидк", "actions", "спецпредлож"]):
                    subpage_targets["promo"] = clean_sub
                elif not subpage_targets["blog"] and any(k in combo for k in ["блог", "blog", "новости", "news", "статьи", "articles"]):
                    subpage_targets["blog"] = clean_sub

            # Proactive localized probing for missing key legal pages (checking both with and without trailing slash)
            aux_probes = [
                ("policy", ["/policy/", "/policy", "/privacy-policy/", "/privacy-policy", "/privacy/", "/privacy", "/politika/", "/politika"]),
                ("oferta", ["/oferta/", "/oferta", "/terms/", "/terms", "/rules/", "/rules", "/dogovor/", "/dogovor"]),
                ("contacts", ["/contacts/", "/contacts", "/contact/", "/contact", "/requisites/", "/requisites", "/about/", "/about", "/index.php/o-kompanii"]),
            ]
            for target_key, candidate_paths in aux_probes:
                if not subpage_targets[target_key]:
                    for c_path in candidate_paths:
                        trial_urls = []
                        if initial_path:
                            trial_urls.append(urllib.parse.urljoin(final_main_url, f"{initial_path}{c_path}"))
                        trial_urls.append(urllib.parse.urljoin(final_main_url, c_path))

                        found = False
                        for t_url in trial_urls:
                            clean_t_url, _ = urllib.parse.urldefrag(t_url)
                            if clean_t_url.rstrip("/") == final_main_url.rstrip("/"):
                                continue
                            try:
                                resp_probe, final_probe_url, _ = await safe_fetch_with_redirects(client, clean_t_url, timeout=4.0)
                                if resp_probe and resp_probe.status_code == 200 and "text/html" in resp_probe.headers.get("content-type", "").lower():
                                    subpage_targets[target_key] = urllib.parse.urldefrag(final_probe_url)[0]
                                    found = True
                                    break
                            except Exception:
                                pass
                        if found:
                            break

            inspected_pages: List[Tuple[str, str, str]] = [("home", final_main_url, main_html)]
            seen_page_urls: Set[str] = {urllib.parse.urldefrag(final_main_url)[0].rstrip("/")}
            for p_type, sub_url in subpage_targets.items():
                if not sub_url:
                    continue
                clean_target_url, _ = urllib.parse.urldefrag(sub_url)
                if clean_target_url.rstrip("/") in seen_page_urls:
                    continue
                if len(inspected_pages) < max_pages:
                    try:
                        resp_sub, final_resp_url_raw, _ = await safe_fetch_with_redirects(client, clean_target_url, timeout=5.0)
                        if resp_sub and resp_sub.status_code == 200 and "text/html" in resp_sub.headers.get("content-type", "").lower():
                            final_resp_url, _ = urllib.parse.urldefrag(final_resp_url_raw)
                            if final_resp_url.rstrip("/") not in seen_page_urls:
                                seen_page_urls.add(final_resp_url.rstrip("/"))
                                inspected_pages.append((p_type, final_resp_url, resp_sub.text))
                    except Exception:
                        pass

            # 3. Company Identification (owner priority: contacts -> oferta -> home -> policy, filtering infra/hoster INNs)
            inn_res = _extract_own_company_inns(inspected_pages, clean_domain)
            combined_text = inn_res["combined_text"]
            non_policy_plain = inn_res["non_policy_plain"]
            found_inns = inn_res["found_inns"]
            found_ogrns = inn_res["found_ogrns"]
            valid_inn = inn_res["valid_inn"]
            valid_ogrn = inn_res["valid_ogrn"]
            is_foreign_kio = inn_res["is_foreign_kio"]

            # 4. GIR BO FNS Query (bo.nalog.gov.ru)
            revenue_rub = None
            revenue_year = None
            legal_name = None
            if valid_inn and len(valid_inn) == 10:
                cached_gir = get_cached_registry("GIR_BO", valid_inn)
                if cached_gir is not None:
                    revenue_rub = cached_gir.get("revenue_rub")
                    revenue_year = cached_gir.get("revenue_year")
                    legal_name = _strip_markup(cached_gir.get("gir_bo_name")) or None
                else:
                    try:
                        await enforce_fns_rate_limit_async()
                        gir_url = f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={valid_inn}"
                        gir_resp = await client.get(
                            gir_url,
                            headers={"Referer": "https://bo.nalog.gov.ru/"},
                            timeout=5.0,
                        )
                        if gir_resp.status_code == 200:
                            content = gir_resp.json().get("content", [])
                            if content:
                                org = content[0]
                                bfo = org.get("bfo") or {}
                                gain_sum = bfo.get("gainSum")
                                if gain_sum is not None:
                                    revenue_rub = int(gain_sum * 1000)
                                revenue_year = str(bfo.get("period")) if bfo.get("period") else None
                                legal_name = _strip_markup(org.get("shortName")) or None
                                if revenue_rub is not None:
                                    save_cached_registry(
                                        "GIR_BO",
                                        valid_inn,
                                        {
                                            "revenue_rub": revenue_rub,
                                            "revenue_year": revenue_year,
                                            "gir_bo_name": legal_name,
                                            "okved": org.get("okved2"),
                                        },
                                    )
                    except Exception as e:
                        logger.warning(f"GIR BO query failed for INN {valid_inn}: {e}")

            if not legal_name and is_foreign_kio:
                legal_name = f"Иностранная организация (КИО {valid_inn})"

            # 4B. Extract corporate emails, Telegram contacts, and director/IP FIO (+ optional DaData Party)
            site_contacts = _extract_site_contacts_and_ceo(inspected_pages, clean_domain)
            corporate_emails = site_contacts["corporate_emails"]
            telegram_contacts = site_contacts["telegram_contacts"]
            management_name = site_contacts["management_name"]
            management_post = site_contacts["management_post"]

            dadata_key = os.environ.get("DADATA_API_KEY", "").strip()
            if not management_name and valid_inn and not is_foreign_kio and len(valid_inn) in (10, 12) and dadata_key:
                cached_dd = get_cached_registry("DADATA_PARTY", valid_inn)
                if cached_dd is not None:
                    management_name = cached_dd.get("management_name") or management_name
                    management_post = cached_dd.get("management_post") or management_post
                    if not legal_name and cached_dd.get("legal_name"):
                        legal_name = cached_dd.get("legal_name")
                else:
                    try:
                        dd_resp = await client.post(
                            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party",
                            headers={
                                "Authorization": f"Token {dadata_key}",
                                "Content-Type": "application/json",
                                "Accept": "application/json",
                            },
                            json={"query": valid_inn},
                            timeout=4.0,
                        )
                        if dd_resp.status_code == 200:
                            sugs = dd_resp.json().get("suggestions", [])
                            if sugs:
                                p_data = sugs[0].get("data", {}) or {}
                                mgmt = p_data.get("management", {}) or {}
                                dd_mgmt_name = mgmt.get("name")
                                dd_mgmt_post = mgmt.get("post") or ("Руководитель" if dd_mgmt_name else None)
                                dd_legal_name = sugs[0].get("value") or (p_data.get("name") or {}).get("short_with_opf")
                                if dd_mgmt_name:
                                    management_name = dd_mgmt_name
                                    management_post = dd_mgmt_post
                                if not legal_name and dd_legal_name:
                                    legal_name = dd_legal_name
                                save_cached_registry(
                                    "DADATA_PARTY",
                                    valid_inn,
                                    {
                                        "management_name": dd_mgmt_name,
                                        "management_post": dd_mgmt_post,
                                        "legal_name": dd_legal_name,
                                    },
                                )
                    except Exception as e:
                        logger.warning(f"DaData Party query failed for INN {valid_inn}: {e}")

            # 5. RKN Operator Registry Query (pd.rkn.gov.ru)
            rkn_data = None
            is_registered_in_rkn = None
            if valid_inn:
                rkn_data = await check_rkn_registry_async(client, valid_inn, timeout=6.0)
                is_registered_in_rkn = rkn_data.get("is_registered_in_rkn")

            # 6. Audit Violations across pages with TripleEvidenceLock
            raw_findings: List[Dict[str, Any]] = []
            has_gtm = False
            has_foreign_trackers = False
            gtm_id = None
            total_pd_forms_found = 0

            site_has_valid_policy = bool(subpage_targets.get("policy")) or any(
                p[0] == "policy" or "privacy" in p[1].lower() or "politika" in p[1].lower() for p in inspected_pages
            )
            fetched_policy_html = next(
                (p[2] for p in inspected_pages if p[0] == "policy" or "privacy" in p[1].lower() or "politika" in p[1].lower()),
                "",
            )
            fetched_policy_plain = (
                BeautifulSoup(fetched_policy_html, "html.parser").get_text(" ", strip=True)
                if fetched_policy_html
                else ""
            )
            effective_policy_text = fetched_policy_plain if len(fetched_policy_plain) >= 200 else None

            for page_type, p_url, p_html in inspected_pages:
                # 152-FZ Scan with policy presence & disclosure awareness
                report_152 = self.scanner_152.scan_html(
                    p_html,
                    page_url=p_url,
                    policy_text=effective_policy_text,
                    site_has_valid_privacy_policy=site_has_valid_policy,
                )

                # Track foreign transfers
                for v in report_152.foreign_transfer_violations:
                    if v.signature == "google-tag-manager":
                        has_gtm = True
                        m_g = re.search(r"GTM-[A-Z0-9]{4,12}", p_html)
                        if m_g:
                            gtm_id = m_g.group(0)
                    elif v.signature in (
                        "google-analytics",
                        "google-recaptcha",
                        "foreign-tracker-meta-hotjar",
                        "cloudflare-turnstile",
                        "foreign-chat-crm-widget",
                        "foreign-ad-pixel-tiktok-linkedin",
                    ):
                        has_foreign_trackers = True

                    witness = EvidenceWitness(
                        source_url=p_url,
                        line_number=v.line_number or 1,
                        raw_snippet=v.snippet[:160],
                        quote=v.snippet[:60],
                        notes=v.remediation_note,
                    )
                    raw_findings.append(classify_finding(v.signature, context_mode="OWN_WEBSITE", witness=witness))

                # Check forms
                for f in report_152.forms:
                    if f.has_personal_data_inputs:
                        total_pd_forms_found += 1
                        if not f.is_compliant:
                            for fv in f.violations:
                                witness = EvidenceWitness(
                                    source_url=p_url,
                                    line_number=fv.line_number or f.line_number,
                                    raw_snippet=fv.snippet[:160] if fv.snippet else f"<form id='{f.form_id}'>",
                                    quote=f.form_id,
                                    notes=fv.remediation_note,
                                )
                                raw_findings.append(classify_finding(fv.signature, context_mode="OWN_WEBSITE", witness=witness))

                # Superlatives without footnote check on visible non-editorial commercial text only (Part 1, 3 Art. 5 38-FZ)
                url_path_lower = urllib.parse.urlparse(p_url).path.lower()
                is_editorial_path = any(
                    seg in url_path_lower
                    for seg in ("/blog", "/news", "/media", "/articles", "/article", "/journal", "/research", "/guides", "/academy")
                )
                if page_type not in ("policy", "oferta", "blog") and not is_editorial_path:
                    soup_sup = BeautifulSoup(p_html, "html.parser")
                    for hidden_tag in soup_sup(["script", "style", "noscript", "svg", "template", "head", "meta", "link", "blockquote"]):
                        hidden_tag.decompose()
                    review_container_re = re.compile(
                        r"(?i)(?:^|[\s_-])(?:reviews?|testimonials?|feedbacks?|otzyv\w*|comments?|user-quote|client-quote)(?:$|[\s_-])"
                    )
                    review_tags_to_remove = []
                    for tag in soup_sup.find_all(True):
                        if tag.attrs is None or tag.name in ("html", "body", "main"):
                            continue
                        cls_val = " ".join(tag.get("class") or []) if isinstance(tag.get("class"), list) else str(tag.get("class") or "")
                        id_val = str(tag.get("id") or "")
                        if review_container_re.search(f"{cls_val} {id_val}"):
                            review_tags_to_remove.append(tag)
                    for r_tag in review_tags_to_remove:
                        if r_tag.attrs is not None:
                            r_tag.decompose()
                    visible_sup_text = soup_sup.get_text(separator="\n", strip=True)
                    benign_superlative_phrases = (
                        "защита аккаунта",
                        "защита вашего аккаунта",
                        "лучший способ",
                        "в лучшем случае",
                        "с наилучшими",
                        "лучшая практика",
                        "лучшие практики",
                        "лучшее время",
                        "в лучшую сторону",
                        "как лучший выбор",
                        "советует его как лучший",
                        "списки «лучших»",
                        'списки "лучших"',
                        "список «лучших»",
                    )
                    review_text_guard_re = re.compile(
                        r"(?i)(?:\bпользуюсь\b|\bиспользую\b|\bпокупал[аи]?\b|\bзаказывал[аи]?\b|\bпроходил[аи]?\b|"
                        r"\bвидела?\b|\bпопробовал[аи]?\b|\bрекомендую\b|\bсоветую\b|\bчестно\s+говорю\b|"
                        r"\bиз\s+ru\s*store\b|\bapp\s*store\b|\bgoogle\s*play\b|\bотзыв\w*\b|\bспасибо\b|\bблагодар\w*\b)"
                    )
                    for m_sup in SUPERLATIVE_REGEX.finditer(visible_sup_text):
                        claim_text = m_sup.group(0)
                        pos = m_sup.start()
                        ctx_local = visible_sup_text[max(0, pos - 30) : min(len(visible_sup_text), m_sup.end() + 50)].lower()
                        if any(bp in ctx_local for bp in benign_superlative_phrases):
                            continue
                        ctx_review = visible_sup_text[max(0, pos - 120) : min(len(visible_sup_text), m_sup.end() + 120)].lower()
                        if review_text_guard_re.search(ctx_review):
                            continue
                        window = visible_sup_text[pos : min(len(visible_sup_text), pos + 350)]
                        has_asterisk = "*" in window or "сноска" in window.lower()
                        has_criteria = any(c in window.lower() for c in ["по версии", "согласно", "исследовани", "данным"])
                        if not (has_asterisk and has_criteria):
                            clean_win = re.sub(
                                r"\s+",
                                " ",
                                visible_sup_text[max(0, pos - 50) : min(len(visible_sup_text), m_sup.end() + 70)],
                            ).strip()
                            witness = EvidenceWitness(
                                source_url=p_url,
                                line_number=1,
                                raw_snippet=clean_win[:160],
                                quote=claim_text,
                                notes="Превосходная степень без сноски со звездочкой и объективного критерия",
                            )
                            raw_findings.append(classify_finding("superlative_without_asterisk", context_mode="OWN_WEBSITE", witness=witness))

                # Foreign Authorization check (Part 2 Art. 13.55 KoAP, 199-FZ moratorium)
                if re.search(r"(?i)(?:accounts\.google\.com|appleid\.apple\.com|telegram-widget\.js|oauth\.telegram\.org)", p_html):
                    witness = EvidenceWitness(
                        source_url=p_url,
                        line_number=1,
                        raw_snippet="Foreign Auth Widget (Google / Apple / Telegram)",
                        quote="auth-widget",
                        notes="Мораторий на штрафы до 01.01.2029 (требование 248-ФЗ)",
                    )
                    raw_findings.append(classify_finding("foreign-auth-google-apple", context_mode="OWN_WEBSITE", witness=witness))

                # Check /blog or /news for external commercial links without erid
                if page_type == "blog":
                    soup_blog = BeautifulSoup(p_html, "html.parser")
                    for a_blog in soup_blog.find_all("a", href=True):
                        href_b = resolve_redirect_url(a_blog["href"].strip())
                        try:
                            p_b = urllib.parse.urlparse(href_b)
                            h_b = p_b.netloc.lower().split(":")[0]
                            if h_b.startswith("www."):
                                h_b = h_b[4:]
                            if h_b and h_b != clean_domain and h_b not in COMMON_PLATFORMS:
                                # External third-party commercial link on blog
                                if is_affiliate_link(href_b) and not ERID_REGEX.search(p_html):
                                    witness = EvidenceWitness(
                                        source_url=p_url,
                                        line_number=1,
                                        raw_snippet=f"Blog external promo link: {href_b[:120]}",
                                        quote=href_b,
                                        notes="Внешняя реферальная ссылка в блоге без токена erid",
                                    )
                                    raw_findings.append(classify_finding("unmarked_ad_no_erid", context_mode="OWN_WEBSITE", witness=witness))
                        except Exception:
                            pass

            # 7. GTM Deep-Inspect live query
            if has_gtm and gtm_id and not has_foreign_trackers:
                try:
                    gtm_resp = await client.get(f"https://www.googletagmanager.com/gtm.js?id={gtm_id}", timeout=4.0)
                    if gtm_resp.status_code == 200:
                        analysis = Compliance152FZScanner.inspect_gtm_container_content(gtm_resp.text)
                        if analysis.get("is_benign"):
                            for f in raw_findings:
                                if f["signature"] == "google-tag-manager":
                                    f["tier"] = EnforcementTier.GRAY_HYGIENE_BONUS.value
                                    f["is_real_fine"] = False
                                    f["is_hygiene_bonus"] = True
                                    f["description"] = "Контейнер GTM проверен: содержит только российские счетчики (состав нарушения отсутствует)"
                except Exception:
                    pass

            # 8. RKN Unregistered Operator & Undeclared Cross-Border Transfer checks
            if total_pd_forms_found > 0 and is_registered_in_rkn is False:
                witness = EvidenceWitness(
                    source_url=final_main_url,
                    line_number=1,
                    raw_snippet=f"INN: {valid_inn} is NOT found in pd.rkn.gov.ru registry while collecting personal data",
                    quote=f"INN {valid_inn}",
                    rkn_registry_response=rkn_data,
                    notes="Сбор ПД на сайте без регистрации в реестре операторов РКН (ч. 10 ст. 13.11 КоАП РФ)",
                )
                raw_findings.append(classify_finding("rkn_unregistered_operator", context_mode="OWN_WEBSITE", witness=witness))
            elif (
                is_registered_in_rkn is True
                and isinstance(rkn_data, dict)
                and rkn_data.get("has_cross_border_declared") is False
                and has_foreign_trackers
            ):
                witness = EvidenceWitness(
                    source_url=final_main_url,
                    line_number=1,
                    raw_snippet=f"INN: {valid_inn} declared cross_border=False in pd.rkn.gov.ru while active foreign trackers transmit visitor data",
                    quote=f"INN {valid_inn}",
                    rkn_registry_response=rkn_data,
                    notes="В реестре РКН указано отсутствие трансграничной передачи, однако на сайте активны зарубежные трекеры/виджеты (ст. 12 152-ФЗ)",
                )
                raw_findings.append(classify_finding("rkn_undeclared_cross_border_transfer", context_mode="OWN_WEBSITE", witness=witness))

            # 9. 168-FZ State Language check
            res_168 = detect_untranslated_foreign_words_168fz(combined_text)
            flagged_168 = res_168.get("flagged_commercial_phrases", [])
            if flagged_168:
                witness = EvidenceWitness(
                    source_url=final_main_url,
                    line_number=1,
                    raw_snippet=", ".join(flagged_168[:3]),
                    quote=flagged_168[0],
                    notes="Публичная информация на иностранном языке без перевода (168-ФЗ)",
                )
                raw_findings.append(classify_finding("commercial_b2c_foreign_words_168fz", context_mode="OWN_WEBSITE", witness=witness))

            # 10. IT-Accreditation check (Order 511)
            it_audit = ITAccreditationAuditor.audit_it_company_website(main_html, expected_inn=valid_inn)
            if not it_audit["is_compliant"] and any(k in combined_text.lower() for k in ["ит-аккредитац", "аккредитованная ит", "реестр минцифры"]):
                witness = EvidenceWitness(
                    source_url=final_main_url,
                    line_number=1,
                    raw_snippet="; ".join(it_audit["missing_requirements"]),
                    quote="ИТ-аккредитация",
                    notes="Приказ Минцифры РФ № 511",
                )
                raw_findings.append(classify_finding("mintsifry_it_accreditation_risk", context_mode="OWN_WEBSITE", witness=witness))

            # 10.1 Forensics v3.0 Deep Cross-Forensics & Vendor Supply Chain (Gear 3: L2)
            forensics_dossier: Dict[str, Any] = {}
            run_l2 = deep or gear == "deep_l2" or (gear == "auto" and (marker_hidden_inn or marker_external_scripts or l0_markers_count >= 1))
            if run_l2:
                try:
                    # A. Missing seller requisites on storefront (ст. 9 ЗоЗПП, п. 19 ПП РФ № 2463)
                    contacts_html = next((p[2] for p in inspected_pages if p[0] == "contacts"), "")
                    miss_req = detect_missing_seller_requisites(main_html, contacts_html, found_inns, found_ogrns)
                    if miss_req:
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet="Коммерческий сайт: в подвале и на странице контактов скрыты ИНН и ОГРН",
                            quote=clean_domain,
                            notes="ст. 9 Закона РФ № 2300-1 (ЗоЗПП), п. 19 ПП РФ № 2463",
                        )
                        raw_findings.append(classify_finding("missing_seller_requisites_zozpp", context_mode="OWN_WEBSITE", witness=witness))

                    # B. Perimeter discovery (related subdomains + DNS/MX/Host Geo)
                    policy_page_html = next((p[2] for p in inspected_pages if p[0] == "policy"), "")
                    oferta_page_html = next((p[2] for p in inspected_pages if p[0] == "oferta"), "")
                    all_htmls = [p[2] for p in inspected_pages]
                    discovered_subs = await discover_perimeter_hosts(clean_domain, all_htmls, policy_html=policy_page_html, oferta_html=oferta_page_html, client=client)
                    # Ensure primary target domain is strictly first for DNS/Host Geo resolution
                    sorted_subs_for_geo = [clean_domain] + [s for s in discovered_subs if s != clean_domain]
                    dns_geo = await inspect_infrastructure_geo_and_mx(sorted_subs_for_geo, client=client)
                    for f_geo in dns_geo.get("findings", []):
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet=f"Host: {f_geo.get('host', clean_domain)} | IP: {f_geo.get('ip')} | MX: {dns_geo.get('mx_records')}",
                            quote=clean_domain,
                            notes=f_geo.get("description"),
                        )
                        raw_findings.append(classify_finding(f_geo["signature"], context_mode="OWN_WEBSITE", witness=witness))

                    # C. Inspect external JS scripts and endpoints across subdomains
                    subs_html_dict = {clean_domain: main_html}
                    # Prioritize subdomains with promo, chat, widget, app, or root
                    ordered_subs = sorted(
                        [s for s in discovered_subs if s != clean_domain],
                        key=lambda s: 0 if any(k in s for k in ["promo", "chat", "widget", "app"]) else (1 if s == root_d else 2)
                    )
                    for s_dom in ordered_subs:
                        if len(subs_html_dict) < 6:
                            try:
                                r_sub = await client.get(f"https://{s_dom}", timeout=4.0)
                                if r_sub.status_code == 200:
                                    subs_html_dict[s_dom] = r_sub.text
                            except Exception:
                                pass
                    js_leaks = await inspect_external_js_endpoints(subs_html_dict, client=client)
                    for leak in js_leaks:
                        witness = EvidenceWitness(
                            source_url=f"https://{leak.get('subdomain', clean_domain)}",
                            line_number=1,
                            raw_snippet=f"Script: {leak.get('script_src')} -> EndPoint: {leak.get('target_host')} ({leak.get('target_ip')})",
                            quote=leak.get("target_host") or "foreign_widget",
                            notes=leak.get("description"),
                        )
                        raw_findings.append(classify_finding("foreign_widget_data_leak", context_mode="OWN_WEBSITE", witness=witness))

                    # D. Resolve Phoenix Clones in FNS (bo.nalog.gov.ru)
                    org_candidates = []
                    if legal_name:
                        org_candidates.append(legal_name)
                    found_orgs = re.findall(r'(?i)(?:ООО|ОАО|ЗАО|Общество с ограниченной ответственностью)\s*[«"][^"»]+[»"]', non_policy_plain)
                    for fo in found_orgs:
                        if fo not in org_candidates:
                            org_candidates.append(fo)
                    site_inn_set = {valid_inn} if valid_inn else set()
                    phoenix_list = await resolve_phoenix_clones_in_fns(
                        org_candidates,
                        client=client,
                        site_inns=site_inn_set,
                    )
                    phoenix_match = phoenix_list[0] if phoenix_list else None
                    if phoenix_match:
                        # Enrich profile with active clone only when site INN was empty or matched the predecessor
                        if not valid_inn or valid_inn == phoenix_match['predecessor']['inn']:
                            valid_inn = phoenix_match['successor']['inn']
                            valid_ogrn = phoenix_match['successor']['ogrn']
                            legal_name = _strip_markup(phoenix_match['successor']['name']) or legal_name
                            revenue_rub = phoenix_match['successor'].get('revenue_rub') or revenue_rub

                    # E. Policy and B2C Oferta Text Forensics (6 defect detectors)
                    combined_site_html = " ".join(all_htmls)
                    policy_defects = policy_and_terms_forensics(
                        clean_domain,
                        policy_html=policy_page_html,
                        oferta_html=oferta_page_html,
                        site_html=combined_site_html,
                    )
                    for pdef in policy_defects:
                        src_ptype = pdef.get("source_page_type", "policy")
                        if src_ptype == "oferta":
                            def_url = subpage_targets.get("oferta") or final_main_url
                        elif src_ptype == "policy":
                            def_url = subpage_targets.get("policy") or final_main_url
                        else:
                            def_url = final_main_url
                        witness = EvidenceWitness(
                            source_url=def_url,
                            line_number=1,
                            raw_snippet=(pdef.get("raw_snippet") or pdef.get("description") or "")[:160],
                            quote=pdef.get("quote") or clean_domain,
                            notes=pdef.get("description"),
                        )
                        raw_findings.append(classify_finding(pdef["signature"], context_mode="OWN_WEBSITE", witness=witness))

                    # F. Regalia and PDF chronology mismatch
                    pdf_urls = [
                        urllib.parse.urljoin(final_main_url, href)
                        for href in re.findall(r'href=["\']([^"\']+\.pdf)["\']', combined_text, re.I)
                    ]
                    inact_inn = phoenix_match['predecessor']['inn'] if phoenix_match else None
                    reg_mismatches = await detect_regalia_and_pdf_chronology_mismatch(
                        valid_ogrn, combined_text, pdf_urls, liquidated_inn=inact_inn, client=client
                    )

                    # Consolidated handling: Phoenix clone and regalia chronology mismatch share
                    # the exact same legal norm (ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ).
                    # If both trigger, merge into 1 consolidated RED finding to prevent duplicate norms.
                    if phoenix_match and reg_mismatches:
                        combined_notes = (
                            f"{phoenix_match['description']} Совместно с хронологическим разрывом регалий: "
                            f"{reg_mismatches[0].get('description')}"
                        )
                        combined_snippet = (
                            f"Ликвидирован: ИНН {phoenix_match['predecessor']['inn']} | "
                            f"Клон: ИНН {phoenix_match['successor']['inn']} (ОГРН {phoenix_match['successor']['ogrn']}, год {reg_mismatches[0].get('ogrn_year')}) "
                            f"vs исторические регалии/патенты"
                        )
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet=combined_snippet[:160],
                            quote=_strip_markup(phoenix_match['successor']['name']),
                            notes=combined_notes,
                        )
                        raw_findings.append(classify_finding("phoenix_clone_detected", context_mode="OWN_WEBSITE", witness=witness))
                    elif phoenix_match:
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet=(
                                f"Ликвидирован: ИНН {phoenix_match['predecessor']['inn']} ({phoenix_match['predecessor']['status_date']}) | "
                                f"Клон: ИНН {phoenix_match['successor']['inn']} ({phoenix_match['successor']['ogrn']})"
                            ),
                            quote=_strip_markup(phoenix_match['successor']['name']),
                            notes=phoenix_match['description'],
                        )
                        raw_findings.append(classify_finding("phoenix_clone_detected", context_mode="OWN_WEBSITE", witness=witness))
                    elif reg_mismatches:
                        for rm in reg_mismatches:
                            witness = EvidenceWitness(
                                source_url=final_main_url,
                                line_number=1,
                                raw_snippet=(rm.get("raw_snippet") or f"ОГРН {valid_ogrn} (год {rm.get('ogrn_year')})")[:160],
                                quote=rm.get("quote") or str(rm.get("ogrn_year")),
                                notes=rm.get("description"),
                            )
                            raw_findings.append(classify_finding("regalia_chronology_mismatch", context_mode="OWN_WEBSITE", witness=witness))

                    # G. Instant protocol triggers (Extremist Meta links, installments without PSK)
                    inst_triggers = detect_instant_protocol_triggers(all_htmls)
                    for itg in inst_triggers:
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet=itg.get("raw_snippet") or itg.get("quote") or "Instant Trigger",
                            quote=itg.get("quote") or "trigger",
                            notes=itg.get("description"),
                        )
                        raw_findings.append(classify_finding(itg["signature"], context_mode="OWN_WEBSITE", witness=witness))

                    # H. Vendor Supply Chain Audit (Hop 0 -> Hop 1 Graph Investigation)
                    vendor_chain_pages = list(subs_html_dict.values()) + all_htmls
                    vendor_findings, vendor_dossiers = await audit_vendor_supply_chain(
                        client_domain=clean_domain,
                        all_html_pages=vendor_chain_pages,
                        policy_html=policy_page_html,
                        client=client,
                        max_unknown_vendors=5,
                    )
                    for vf in vendor_findings:
                        witness = EvidenceWitness(
                            source_url=final_main_url,
                            line_number=1,
                            raw_snippet=f"Vendor: {vf.get('vendor_root', vf.get('vendor', ''))} | Host: {vf.get('host') or vf.get('vendor_host', '')}",
                            quote=vf.get("vendor_root") or vf.get("vendor") or "vendor_widget",
                            notes=vf.get("description"),
                        )
                        raw_findings.append(classify_finding(vf["signature"], context_mode="OWN_WEBSITE", witness=witness))

                    forensics_dossier = {
                        "discovered_subdomains": discovered_subs,
                        "dns_geo": dns_geo,
                        "js_leaks": js_leaks,
                        "phoenix_dossier": phoenix_match,
                        "vendor_dossiers": vendor_dossiers,
                    }
                except Exception as e:
                    logger.error(f"Forensics v3 execution failed on {clean_domain}: {e}", exc_info=True)

            # 10.2 Deduplicate vendor widget and foreign JS leak in RED
            # If toxic_supply_chain_widget already covers vendor_root (e.g. athenachat.ai),
            # merge any corresponding foreign_widget_data_leak into the toxic widget record and suppress duplicate RED.
            toxic_roots = set()
            for f in raw_findings:
                if f.get("signature") == "toxic_supply_chain_widget":
                    w = f.get("witness")
                    if isinstance(w, dict):
                        quote_txt = str(w.get("quote", "")).lower()
                        raw_txt = str(w.get("raw_snippet", "")).lower()
                        notes_txt = str(w.get("notes", "")).lower()
                    elif w is not None:
                        quote_txt = str(getattr(w, "quote", "") or "").lower()
                        raw_txt = str(getattr(w, "raw_snippet", "") or "").lower()
                        notes_txt = str(getattr(w, "notes", "") or "").lower()
                    else:
                        quote_txt = raw_txt = notes_txt = ""

                    desc_txt = str(f.get("description", "")).lower()
                    combined_src = f"{quote_txt} {raw_txt} {notes_txt} {desc_txt}"
                    for d_match in re.findall(r"\b([a-zA-Z0-9-]+\.[a-zA-Z]{2,})\b", combined_src):
                        d_root = extract_root_domain(d_match)
                        if d_root and "." in d_root:
                            toxic_roots.add(d_root)

            if toxic_roots:
                filtered_raw = []
                for f in raw_findings:
                    if f.get("signature") == "foreign_widget_data_leak":
                        w = f.get("witness")
                        if isinstance(w, dict):
                            quote_txt = str(w.get("quote", "")).lower()
                            raw_txt = str(w.get("raw_snippet", "")).lower()
                            notes_txt = str(w.get("notes", "")).lower()
                        elif w is not None:
                            quote_txt = str(getattr(w, "quote", "") or "").lower()
                            raw_txt = str(getattr(w, "raw_snippet", "") or "").lower()
                            notes_txt = str(getattr(w, "notes", "") or "").lower()
                        else:
                            quote_txt = raw_txt = notes_txt = ""

                        desc_txt = str(f.get("description", "")).lower()
                        combined_leak = f"{quote_txt} {raw_txt} {notes_txt} {desc_txt}"
                        matched_root = None
                        for r in toxic_roots:
                            if r in combined_leak:
                                matched_root = r
                                break
                        if matched_root:
                            logger.info(f"Deduplicating foreign_widget_data_leak into toxic_supply_chain_widget for {matched_root}")
                            continue  # Suppress duplicate RED entry
                    filtered_raw.append(f)
                raw_findings = filtered_raw

            # 11. Deduplicate cross-page findings (identical violations from multiple pages of the same site)
            deduped_findings_map: Dict[str, Dict[str, Any]] = {}
            for f in raw_findings:
                sig = f["signature"]
                page = f.get("witness", {}).get("source_url") if f.get("witness") else None
                if sig not in deduped_findings_map:
                    deduped_findings_map[sig] = f
                    deduped_findings_map[sig]["inspected_pages"] = [page] if page else []
                else:
                    if page and page not in deduped_findings_map[sig]["inspected_pages"]:
                        deduped_findings_map[sig]["inspected_pages"].append(page)

            deduped_findings = []
            for sig, f in deduped_findings_map.items():
                p_list = f.get("inspected_pages", [])
                if len(p_list) > 1:
                    pages_abbr = [p.split("/")[-1] or "/" for p in p_list]
                    f["description"] = f"{f['description']} (сквозной элемент на {len(p_list)} страницах: {', '.join(pages_abbr)})"
                deduped_findings.append(f)

            split_tiers = split_findings_by_traffic_light(deduped_findings)
            active_red_sigs = [f["signature"] for f in split_tiers["RED_REAL_FINE"]]
            active_orange_sigs = [f["signature"] for f in split_tiers["ORANGE_REGULATORY_DEMAND"]]
            # Include actionable violations into KoAP penalty evaluation
            eval_sigs = active_red_sigs if active_red_sigs else [s for s in active_orange_sigs if s != "foreign-auth-google-apple"]

            is_sme = True
            if revenue_rub and revenue_rub > 800_000_000:
                is_sme = False

            is_ip = bool(valid_inn and len(valid_inn) == 12)
            penalties = self.koap_calculator.calculate_penalties(
                eval_sigs,
                is_sme=is_sme,
                first_offense=True,
                is_ip=is_ip,
            )

            return {
                "audit_type": "SITE_AUDIT",
                "context_mode": "OWN_WEBSITE",
                "domain": clean_domain,
                "main_url": final_main_url,
                "final_url": final_main_url,
                "detected_lang": detected_lang,
                "cyrillic_ratio": round(cyr_ratio, 3),
                "pages_inspected_count": len(inspected_pages),
                "pages_inspected": [p[1] for p in inspected_pages],
                "company_profile": {
                    "inn": valid_inn,
                    "ogrn": valid_ogrn,
                    "legal_name": legal_name or f"Компания ({clean_domain})",
                    "corporate_emails": corporate_emails,
                    "telegram_contacts": telegram_contacts,
                    "management_name": management_name,
                    "management_post": management_post,
                    "revenue_rub": revenue_rub,
                    "revenue_year": revenue_year,
                    "entity_type": "FOREIGN_KIO_ENTITY" if is_foreign_kio else ("LEGAL_ENTITY" if valid_inn and len(valid_inn) == 10 else "INDIVIDUAL_OR_SME"),
                    "is_foreign_kio": is_foreign_kio,
                    "is_sme": is_sme,
                    "phoenix_dossier": forensics_dossier.get("phoenix_dossier"),
                },
                "forensics_v3": forensics_dossier,
                "rkn_registry": rkn_data,
                "it_accreditation": it_audit,
                "language_168fz": res_168,
                "traffic_light": split_tiers,
                "penalties": penalties,
            }

        finally:
            if close_client:
                await client.aclose()

    # -------------------------------------------------------------------------
    # TIER 3: UNIFIED VC.RU HUNTER WITH ADVERTISER GROUPING
    # -------------------------------------------------------------------------
    async def harvest_vc_timeline(
        self,
        client: httpx.AsyncClient,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Harvests live publications from api.vc.ru timeline feeds."""
        unique_articles: Dict[int, Dict[str, Any]] = {}
        feeds = [("date", "Свежее"), ("popular", "Популярное"), ("week", "Тренды недели")]

        for sort_key, feed_title in feeds:
            if len(unique_articles) >= limit:
                break
            last_id = None
            last_val = None
            for page in range(4):
                if len(unique_articles) >= limit:
                    break
                url = f"https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting={sort_key}"
                if last_id and last_val:
                    url += f"&lastId={last_id}&lastSortingValue={last_val}"
                try:
                    resp = await client.get(url, timeout=10.0)
                    if resp.status_code != 200:
                        break
                    data = resp.json().get("result", {})
                    items = data.get("items", [])
                    if not items:
                        break
                    last_id = data.get("lastId")
                    last_val = data.get("lastSortingValue")

                    for it in items:
                        d = it.get("data", {})
                        pid = d.get("id")
                        if not pid or pid in unique_articles:
                            continue

                        title = d.get("title") or "Без заголовка"
                        subsite_obj = d.get("subsite") if isinstance(d.get("subsite"), dict) else {}
                        subsite_name = str(subsite_obj.get("name") or "").strip()
                        author_obj = d.get("author") if isinstance(d.get("author"), dict) else {}
                        author = str(author_obj.get("name") or subsite_name or "Не указан").strip()
                        published_at = _timestamp_to_iso_date(d.get("date"))
                        post_url = d.get("url") or f"https://vc.ru/{pid}"
                        blocks = d.get("blocks", [])
                        text_parts, extracted_links, first_header = _extract_vcru_block_captions_and_links(blocks)

                        full_text = " ".join(text_parts).strip()
                        if not full_text:
                            full_text = title
                        if title == "Без заголовка" and first_header:
                            title = first_header

                        ad_raw = d.get("ad_data") or d.get("ad") or {}
                        if not isinstance(ad_raw, dict):
                            ad_raw = {}
                        is_promo = bool(d.get("isPromo") or d.get("is_promo") or ad_raw or d.get("isFlash"))

                        unique_articles[pid] = {
                            "id": pid,
                            "title": title,
                            "author": author,
                            "subsite": subsite_name,
                            "published_at": published_at,
                            "post_url": post_url,
                            "full_text": full_text,
                            "commercial_links": extracted_links,
                            "ad_data": ad_raw,
                            "is_promo": is_promo,
                        }
                        if len(unique_articles) >= limit:
                            break
                    await asyncio.sleep(0.08)
                except Exception:
                    break

        return list(unique_articles.values())[:limit]

    async def hunt_vcru(
        self,
        limit: int = 50,
        output_dir: Optional[Path] = None,
        client: Optional[httpx.AsyncClient] = None,
        ledger: Optional[Any] = None,
        packager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Unified hunt on VC.ru timeline:
        1) Harvests VC.ru publications & audits articles under 38-FZ.
        2) Groups advertiser links by domain and checks OutreachLedger (90-day cooldown / OPT_OUT).
        3) Runs multi-page site audit on new advertisers.
        4) Packages qualified Russian commercial leads via LeadPackager into ADRADAR_DATA_DIR/reports/vcru_leads_v2/
           and registers them in OutreachLedger with status NEW.
        """
        from ingestion.factory_crawler import CompanyProfile, SiteAuditResult
        from legal_sheaf.lead_packager import LeadPackager
        from legal_sheaf.outreach_ledger import OutreachLedger
        from legal_sheaf.scanner_152fz import Report152FZ, Violation152FZ
        from legal_sheaf.schema import LegalRegime, VerificationReport

        close_client = False
        if client is None:
            client = httpx.AsyncClient(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
                },
                timeout=12.0,
                follow_redirects=True,
            )
            close_client = True

        env_data_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
        active_data_dir = Path(env_data_dir).resolve() if env_data_dir else Path(ADRADAR_DATA_DIR).resolve()
        out_path = Path(output_dir) if output_dir else (active_data_dir / "reports" / "vcru_hunt_grouped")
        out_path.mkdir(parents=True, exist_ok=True)
        leads_out_dir = active_data_dir / "reports" / "vcru_leads_v2"
        leads_out_dir.mkdir(parents=True, exist_ok=True)

        active_ledger = ledger or OutreachLedger(db_path=active_data_dir / "db" / "outreach_ledger.sqlite")
        active_packager = packager or LeadPackager(output_root=leads_out_dir)

        try:
            articles_to_process = await self.harvest_vc_timeline(client, limit=limit)

            article_audits: List[Dict[str, Any]] = []
            grouped_by_domain: Dict[str, List[Dict[str, Any]]] = {}
            link_probe_cache: Dict[str, Dict[str, Any]] = {}

            print(f"[*] Аудит {len(articles_to_process)} статей VC.ru...", flush=True)
            for art in articles_to_process:
                p_url = art["post_url"]
                res_art = await self.audit_article(
                    p_url,
                    text=art.get("full_text"),
                    client=client,
                    raw_links=art.get("commercial_links"),
                    title_hint=art.get("title"),
                    author_hint=art.get("author"),
                    platform_ad_meta_hint=art.get("ad_data"),
                    link_probe_cache=link_probe_cache,
                    published_at_hint=art.get("published_at"),
                    subsite_hint=art.get("subsite"),
                )
                if (
                    (not res_art.get("title") or res_art.get("title") == "...")
                    and art.get("title")
                    and art.get("title") != "Без заголовка"
                ):
                    res_art["title"] = art.get("title")
                article_audits.append(res_art)

                # Use cleaned website_urls (excludes messengers, neutral platforms, shorteners)
                if "website_urls" in res_art:
                    candidate_links: List[str] = list(res_art.get("website_urls") or [])
                else:
                    candidate_links = list(res_art.get("commercial_links") or art.get("commercial_links") or [])

                dom_links_map: Dict[str, List[str]] = {}
                for c_link in candidate_links:
                    try:
                        p = urllib.parse.urlparse(c_link)
                        raw_dom = p.netloc.lower().split(":")[0].removeprefix("www.")
                        if not raw_dom or "." not in raw_dom:
                            continue
                        if (
                            _is_host_in_set(raw_dom, HUNT_NON_ICP_HOSTS)
                            or raw_dom.endswith(HUNT_NON_ICP_SUFFIXES)
                        ):
                            continue
                        root_dom = extract_root_domain(raw_dom) or raw_dom
                        if (
                            _is_host_in_set(root_dom, HUNT_NON_ICP_HOSTS)
                            or root_dom.endswith(HUNT_NON_ICP_SUFFIXES)
                        ):
                            continue
                        dom_links_map.setdefault(root_dom, []).append(c_link)
                    except Exception:
                        pass

                selected_doms = list(dom_links_map.keys())
                if len(selected_doms) > 3:
                    promo_doms = [
                        d for d, lnks in dom_links_map.items()
                        if any(
                            AFFILIATE_REGEX.search(u) or re.search(r"(?i)[?&]utm_(?:source|medium|campaign)=", u)
                            for u in lnks
                        )
                    ]
                    if promo_doms:
                        selected_doms = promo_doms
                    elif len(selected_doms) >= 5:
                        # Skip non-promo digest/overview articles with 5+ unrelated domains
                        selected_doms = []

                for dom in selected_doms:
                    grouped_by_domain.setdefault(dom, []).append(res_art)

            print(f"[+] Обнаружено {len(grouped_by_domain)} уникальных рекламодателей среди {len(article_audits)} статей.", flush=True)

            consolidated_company_reports: List[Dict[str, Any]] = []
            packaged_leads: List[Dict[str, Any]] = []
            skipped_by_ledger = 0

            domains_to_audit: List[Tuple[str, List[Dict[str, Any]]]] = []
            for dom, related_articles in grouped_by_domain.items():
                # 1. Domain-level OutreachLedger gate (90-day cooldown / OPT_OUT)
                if not active_ledger.is_touch_allowed(None, dom, cooldown_days=90):
                    skipped_by_ledger += 1
                    print(f"  [SKIP-LEDGER] {dom}: заблокирован кулдауном 90 дней или OPT_OUT.", flush=True)
                    continue
                domains_to_audit.append((dom, related_articles))

            sem = asyncio.Semaphore(4)

            async def _audit_domain_two_stage(
                dom: str, related_articles: List[Dict[str, Any]]
            ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
                async with sem:
                    print(
                        f"  -> Многостраничный аудит сайта рекламодателя: {dom} (связанных статей: {len(related_articles)})...",
                        flush=True,
                    )
                    try:
                        if dom.endswith((".ru", ".su", ".рф")):
                            site_audit_res = await self.audit_site(
                                dom, client=client, max_pages=6, deep=True, gear="auto"
                            )
                        else:
                            # Stage 1: fast qualifier L1 (no crt.sh or deep L2 unless Russian INN confirmed)
                            site_audit_res = await self.audit_site(
                                dom, client=client, max_pages=4, deep=False, gear="qualifier_l1"
                            )
                            cp_l1 = site_audit_res.get("company_profile", {})
                            if cp_l1.get("inn") and not cp_l1.get("is_foreign_kio"):
                                site_audit_res = await self.audit_site(
                                    dom, client=client, max_pages=6, deep=True, gear="deep_l2"
                                )
                    except Exception as exc:
                        logger.error(f"Site audit failed for {dom}: {exc}")
                        site_audit_res = {
                            "audit_type": "SITE_AUDIT",
                            "domain": dom,
                            "status": "CONNECTION_FAILED",
                            "error": str(exc),
                            "company_profile": {},
                            "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                            "penalties": {"total_min_rubles": 0, "total_max_rubles": 0},
                        }
                    return dom, related_articles, site_audit_res

            audited_results = await asyncio.gather(
                *[_audit_domain_two_stage(dom, arts) for dom, arts in domains_to_audit]
            )

            for dom, related_articles, site_audit in audited_results:
                cp = site_audit.get("company_profile", {})
                inn = cp.get("inn")
                legal_name = cp.get("legal_name") or f"Компания ({dom})"
                is_foreign_kio = bool(cp.get("is_foreign_kio"))

                # 2. INN-level OutreachLedger gate (covers multiple domains under same INN)
                if inn and not active_ledger.is_touch_allowed(inn, dom, cooldown_days=90):
                    skipped_by_ledger += 1
                    print(f"  [SKIP-LEDGER] {dom} (ИНН {inn}): заблокирован кулдауном 90 дней или OPT_OUT.", flush=True)
                    continue

                lead_pkg_info: Optional[Dict[str, str]] = None
                ledger_status = "NOT_QUALIFIED"

                tl = site_audit.get("traffic_light", {})
                reds = tl.get("RED_REAL_FINE", [])
                oranges = tl.get("ORANGE_REGULATORY_DEMAND", [])
                has_site_risk = bool(reds or oranges)
                has_article_risk = any(bool(a.get("is_violation_38fz")) for a in related_articles)
                is_ru_commercial = (
                    not is_foreign_kio
                    and site_audit.get("status") not in ("CONNECTION_FAILED", "NON_HTML_RESPONSE")
                    and (
                        dom.endswith((".ru", ".su", ".рф"))
                        or bool(inn)
                    )
                )

                if is_ru_commercial and (has_site_risk or has_article_risk):
                    rev_rub = cp.get("revenue_rub")
                    is_sme_flag = cp.get("is_sme", True)
                    if rev_rub and rev_rub > 10_000_000:
                        tier = "TIER_A_CORP"
                    elif is_sme_flag is False:
                        tier = "TIER_A_CORP"
                    else:
                        tier = "TIER_B_SMB_IP"
                    company_prof = CompanyProfile(
                        domain=dom,
                        inn=inn,
                        ogrn=cp.get("ogrn"),
                        corporate_emails=cp.get("corporate_emails", []),
                        telegram_contacts=cp.get("telegram_contacts", []),
                        legal_name=legal_name,
                        management_name=cp.get("management_name"),
                        management_post=cp.get("management_post"),
                        lead_tier=tier,
                        revenue_rub=rev_rub,
                        revenue_year=str(cp.get("revenue_year")) if cp.get("revenue_year") else None,
                    )

                    mapped_viols: List[Violation152FZ] = []
                    for item in reds + oranges:
                        w = item.get("witness") or {}
                        sig = str(item.get("signature") or "compliance_risk")
                        is_red = bool(item.get("is_real_fine")) or "RED" in str(item.get("tier") or "")
                        mapped_viols.append(
                            Violation152FZ(
                                category="HIGH_LOCALIZATION_RISK" if is_red else "LOW_CDN_TRANSBORDER_RISK",
                                risk_level="HIGH" if is_red else "LOW",
                                signature=sig,
                                line_number=int(w.get("line_number") or 1) if isinstance(w, dict) else 1,
                                snippet=str(w.get("raw_snippet") or w.get("quote") or sig)[:160] if isinstance(w, dict) else sig,
                                legal_basis=str(item.get("legal_basis") or "152-ФЗ"),
                                koap_article=str(item.get("koap_article") or "13.11_part_1"),
                                potential_fine_rub="см. матрицу КоАП",
                                remediation_note=str(item.get("description") or ""),
                            )
                        )

                    site_result_obj = SiteAuditResult(
                        company=company_prof,
                        pages_inspected=site_audit.get("pages_inspected", []),
                        compliance_152fz=Report152FZ(
                            page_url=site_audit.get("final_url") or f"https://{dom}",
                            is_compliant=not bool(mapped_viols),
                            total_violations=len(mapped_viols),
                            high_risk_count=len(reds),
                            low_risk_count=len(oranges),
                            foreign_transfer_violations=mapped_viols,
                            forms=[],
                            summary=f"Выявлено рисков: {len(mapped_viols)}",
                        ),
                    )

                    lead_art = next((a for a in related_articles if a.get("is_violation_38fz")), related_articles[0])
                    is_viol_38 = bool(lead_art.get("is_violation_38fz"))
                    lead_scen = str(lead_art.get("scenario_38fz") or ("UNMARKED_AD_NO_ERID" if is_viol_38 else "CLEAN_NON_AD"))
                    if lead_scen in ("PARTIAL_ERID_MARKING_GAP", "INCOMPLETE_MARKING_LABEL_OR_INN"):
                        lead_reg_enum = LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN
                    elif lead_scen == "BANNED_OR_SPECIAL_REGIME":
                        lead_reg_enum = LegalRegime.BANNED_OR_SPECIAL_REGIME
                    elif is_viol_38:
                        lead_reg_enum = LegalRegime.UNMARKED_AD_NO_ERID
                    else:
                        lead_reg_enum = LegalRegime.CLEAN_NON_AD
                    sheaf_rep = VerificationReport(
                        is_violation=is_viol_38,
                        violation_probability=float(lead_art.get("violation_probability") or (0.85 if is_viol_38 else 0.15)),
                        conformal_verdict=str(lead_art.get("conformal_verdict") or ("VIOLATION" if is_viol_38 else "CLEAN")),
                        regime=lead_reg_enum,
                        regime_name=lead_scen,
                        regime_probabilities={},
                        total_sheaf_energy=float(lead_art.get("sheaf_energy") or 0.0),
                        top_edge_conflicts=[],
                        active_slots=[],
                        legal_citations=[str(lead_art.get("verdict_38fz") or "ст. 18.1 38-ФЗ")] if is_viol_38 else [],
                        mitigating_factors=[],
                        procedural_risks=[],
                        fast_standalone_mode=True,
                    )

                    post_url = str(lead_art.get("article_url") or "https://vc.ru")
                    post_title = str(lead_art.get("title") or "Публикация VC.ru")
                    dom_root = extract_root_domain(dom) or dom
                    target_lnk = next(
                        (
                            u for u in (lead_art.get("commercial_links") or [])
                            if (
                                extract_root_domain(urllib.parse.urlparse(u).netloc) == dom_root
                                or dom_root in urllib.parse.urlparse(u).netloc.lower()
                            )
                        ),
                        f"https://{dom}",
                    )
                    art_author = str(lead_art.get("author") or "").lower()
                    art_author_norm = re.sub(r"[^a-zа-яё0-9]+", "", art_author)
                    brand_stem = re.sub(r"[^a-z0-9]+", "", dom.split(".")[0].lower())
                    post_subsite = ""
                    try:
                        p_parts = [p for p in urllib.parse.urlparse(post_url).path.lower().split("/") if p]
                        if p_parts:
                            post_subsite = p_parts[0]
                    except Exception:
                        pass
                    link_roots = {
                        extract_root_domain(urllib.parse.urlparse(u).netloc)
                        for u in (lead_art.get("commercial_links") or [])
                        if urllib.parse.urlparse(u).netloc
                    }
                    has_first_person_own = (
                        len(link_roots) == 1
                        and dom_root in link_roots
                        and any(
                            fp in post_title.lower()
                            for fp in ("мы сделали", "что мы с этим сделали", "мы разработали", "наш сервис", "подключили", "наш опыт")
                        )
                    )
                    is_own_author = bool(
                        (
                            art_author
                            and art_author != "не указан"
                            and len(brand_stem) >= 4
                            and (brand_stem in art_author_norm or (post_subsite and brand_stem == re.sub(r"[^a-z0-9]+", "", post_subsite)))
                        )
                        or has_first_person_own
                    )
                    author_dom = dom if is_own_author else "vc.ru"

                    lead_pkg_info = active_packager.package_lead(
                        company=company_prof,
                        site_audit=site_result_obj,
                        sheaf_report=sheaf_rep,
                        post_url=post_url,
                        post_title=post_title,
                        target_link=target_lnk,
                        author_domain=author_dom,
                    )
                    active_ledger.record_touch(
                        domain=dom,
                        inn_or_kio=inn,
                        company_name=legal_name,
                        status="NEW",
                        notes=f"VC.ru hunt ({len(related_articles)} linked articles)",
                    )
                    ledger_status = "NEW"
                    packaged_leads.append({"domain": dom, "inn": inn, "legal_name": legal_name, **lead_pkg_info})

                consolidated = {
                    "advertiser_domain": dom,
                    "related_articles_count": len(related_articles),
                    "ledger_status": ledger_status,
                    "lead_package": lead_pkg_info,
                    "related_articles": [
                        {
                            "title": a.get("title"),
                            "url": a.get("article_url"),
                            "author": a.get("author"),
                            "subsite": a.get("subsite"),
                            "published_at": a.get("published_at"),
                            "statute_expired_365d": a.get("statute_expired_365d", False),
                            "ownership_check": a.get("ownership_check"),
                            "scenario_38fz": a.get("scenario_38fz"),
                            "is_violation_38fz": a.get("is_violation_38fz"),
                            "verdict_38fz": a.get("verdict_38fz"),
                            "violation_probability": a.get("violation_probability"),
                            "commercial_links": a.get("commercial_links", []),
                            "per_link_erid_audit": a.get("per_link_erid_audit"),
                            "link_landing_verifications": a.get("link_landing_verifications"),
                            "traffic_light": a.get("traffic_light"),
                            "penalties": a.get("penalties"),
                        }
                        for a in related_articles
                    ],
                    "site_audit": site_audit,
                }
                consolidated_company_reports.append(consolidated)

            rep_file = out_path / "hunt_vcru_consolidated_report.json"
            summary = {
                "generated_at": datetime.now().isoformat(),
                "total_articles_scanned": len(article_audits),
                "unique_advertisers_found": len(grouped_by_domain),
                "skipped_by_ledger": skipped_by_ledger,
                "packaged_leads_count": len(packaged_leads),
                "packaged_leads": packaged_leads,
                "report_path": str(rep_file),
                "consolidated_company_reports": consolidated_company_reports,
            }

            with open(rep_file, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)

            # Mirror default consolidated report to ADRADAR_DATA_DIR/reports/hunt_vcru_consolidated_report.json
            canonical_rep = active_data_dir / "reports" / "hunt_vcru_consolidated_report.json"
            if canonical_rep.resolve() != rep_file.resolve() and output_dir is None:
                canonical_rep.parent.mkdir(parents=True, exist_ok=True)
                with open(canonical_rep, "w", encoding="utf-8") as f:
                    json.dump(summary, f, ensure_ascii=False, indent=2)

            print(f"[+] Отчет сохранен: {rep_file}", flush=True)
            return summary

        finally:
            if close_client:
                await client.aclose()


async def fast_surface_triage_l0(
    domain: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Gear 1 (L0 Fast Surface Triage — <= 0.25s per site, 1 GET of home page <= 256 KB, 0 external APIs).
    Zero external queries to RKN, FNS, RIPE, or crt.sh.
    Evaluates 4 primary risk markers:
    1) Commercial signals (phone, price/tariff, <form>) with no valid INN in HTML;
    2) External <script src> / <iframe src> outside STATIC_CDNS;
    3) <form> without empty checkbox and link to Policy;
    4) Superlatives («№1/лучший/первый»), «рассрочка 0%», OAuth Google/Apple/TG, or Instagram/Facebook links.
    Returns FAST_L0_CLEAN if markers == 0, else ESCALATE_TO_L1.
    """
    t0 = time.perf_counter()
    raw_target = domain.strip().lower()
    raw_target = re.sub(r"^https?://", "", raw_target).split("/")[0].split(":")[0]
    if raw_target.startswith("www."):
        raw_target = raw_target[4:]
    clean_domain = raw_target
    root_d = extract_root_domain(clean_domain)

    close_client = False
    if client is None:
        client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
            },
            timeout=5.0,
            follow_redirects=True,
        )
        close_client = True

    try:
        main_html = ""
        final_url = f"https://{clean_domain}"
        try:
            r = await client.get(final_url, timeout=4.0)
            if r.status_code == 200:
                main_html = r.text
                final_url = str(r.url)
        except Exception:
            try:
                final_url = f"http://{clean_domain}"
                r_http = await client.get(final_url, timeout=4.0)
                if r_http.status_code == 200:
                    main_html = r_http.text
                    final_url = str(r_http.url)
            except Exception:
                pass

        if not main_html:
            elapsed = time.perf_counter() - t0
            return {
                "domain": clean_domain,
                "status": "CONNECTION_FAILED",
                "verdict": "CONNECTION_FAILED",
                "triage_verdict": "CONNECTION_FAILED",
                "is_clean_pass": False,
                "l0_markers_count": 0,
                "execution_time_seconds": elapsed,
            }

        html_256k = main_html[:256 * 1024]

        # 1. Commercial signals with hidden INN
        has_commercial = bool(re.search(
            r"(?i)(?:8\s*800\s*\d{3}|\+7\s*\(?\d{3}\)?|\bтел\b|\bкупить\b|\bцена\b|\bруб\b|\bстоимость\b|\bтариф\b|<form)",
            html_256k
        ))
        inns_on_home = find_valid_inns_in_text(html_256k)
        marker_1 = has_commercial and len(inns_on_home) == 0

        # 2. External scripts / iframes outside static CDN whitelist
        ext_scripts = set()
        for s_match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']|<iframe[^>]+src=["\']([^"\']+)["\']', html_256k, re.I):
            src_val = s_match.group(1) or s_match.group(2) or ""
            if src_val.startswith("http"):
                try:
                    h_val = urllib.parse.urlparse(src_val).netloc.lower().split(":")[0]
                    if h_val.startswith("www."):
                        h_val = h_val[4:]
                    if h_val and not h_val.endswith("." + root_d) and h_val != root_d and h_val not in STATIC_CDNS:
                        ext_scripts.add(h_val)
                except Exception:
                    pass
        marker_2 = len(ext_scripts) > 0

        # 3. Form without empty checkbox and link to Policy
        has_form = "<form" in html_256k.lower()
        has_policy = bool(re.search(r"(?i)href=[\"'][^\"']*(?:policy|privacy|конфиденциальн)", html_256k))
        has_checkbox = bool(re.search(r'(?i)<input[^>]+type=["\']checkbox["\']', html_256k))
        marker_3 = has_form and (not has_policy or not has_checkbox)

        # 4. Superlatives, credit without PSK, foreign OAuth, or Meta links
        marker_4 = bool(
            SUPERLATIVE_REGEX.search(html_256k)
            or re.search(r"(?i)(?:рассрочк[а-я]*\s*0\s*%|в\s+кредит\s+от|кредит\s+без\s+переплат)", html_256k)
            or re.search(r"(?i)(?:accounts\.google\.com|appleid\.apple\.com|telegram-widget\.js|oauth\.telegram\.org)", html_256k)
            or re.search(r"(?i)href=[\"'][^\"']*(?:instagram\.com|facebook\.com)", html_256k)
        )

        markers = {
            "commercial_signals_hidden_inn": marker_1,
            "external_scripts_or_iframes": marker_2,
            "form_without_policy_or_checkbox": marker_3,
            "superlatives_credit_or_social": marker_4,
        }
        l0_count = sum(markers.values())
        elapsed = time.perf_counter() - t0

        verdict = "FAST_L0_CLEAN" if l0_count == 0 else "ESCALATE_TO_L1"
        return {
            "domain": clean_domain,
            "main_url": final_url,
            "cascade_stage": "L0_SURFACE_TRIAGE",
            "status": verdict,
            "verdict": verdict,
            "triage_verdict": verdict,
            "is_clean_pass": l0_count == 0,
            "l0_markers_count": l0_count,
            "markers": markers,
            "execution_time_seconds": elapsed,
        }
    finally:
        if close_client:
            await client.aclose()


async def audit_domains_cascade(
    domains: List[str],
    deep: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    batch_id: Optional[str] = None,
    resume: bool = False,
) -> List[Dict[str, Any]]:
    """
    Cascaded batch audit across domains (L0 -> L1 -> L2).
    Canonical deterministic order.
    Fast exits on L0 clean pass, escalates to L1/L2 when risk markers are detected.
    Supports checkpoint resumption (resume=True) via batch_checkpoints.sqlite.
    """
    auditor = DualAuditor()
    results = []
    canonical_domains = sorted(list(set(domains)))
    checkpoint_store = get_checkpoint_store() if (batch_id and resume) else None
    completed_keys = checkpoint_store.get_completed_keys(batch_id) if checkpoint_store and batch_id else set()

    close_c = False
    if client is None:
        client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
            },
            timeout=8.0,
            follow_redirects=True,
        )
        close_c = True

    try:
        for dom in canonical_domains:
            clean_dom = dom.strip().lower()
            if resume and batch_id and clean_dom in completed_keys:
                cached_res = checkpoint_store.get_item_result(batch_id, clean_dom)
                if cached_res:
                    results.append(cached_res)
                    continue

            # 1. Fast L0 triage
            triage = await fast_surface_triage_l0(dom, client=client)
            if triage.get("is_clean_pass") and not deep:
                # Fast clean exit
                res = {
                    "audit_type": "SITE_AUDIT",
                    "context_mode": "OWN_WEBSITE",
                    "domain": dom,
                    "cascade_stage": "L0_SURFACE_TRIAGE",
                    "status": "FAST_L0_CLEAN",
                    "triage_verdict": "FAST_L0_CLEAN",
                    "l0_markers_count": 0,
                    "pages_inspected_count": 1,
                    "traffic_light": {"RED_REAL_FINE": [], "ORANGE_REGULATORY_DEMAND": [], "GRAY_HYGIENE_BONUS": []},
                    "penalties": {"total_min_rubles": 0, "total_max_rubles": 0, "regulatory_outcome": "Чисто (L0 Fast Pass)"},
                }
            else:
                # Escalate to L1 / L2
                res = await auditor.audit_site(dom, deep=deep, gear="deep_l2" if deep else "auto", client=client)

            results.append(res)
            if batch_id:
                get_checkpoint_store().mark_completed(batch_id, clean_dom, status="COMPLETED", result=res)
    finally:
        if close_c:
            await client.aclose()

    return results


async def run_cascaded_batch_audit(
    domains: List[str],
    gear: str = "auto",
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """
    High-speed cascaded batch audit for multiple domains.
    Canonical deterministic order, applying L0 -> L1 -> L2 cascade pipeline.
    """
    if gear == "auto":
        return await audit_domains_cascade(domains, deep=False, client=client)
    elif gear == "deep_l2":
        return await audit_domains_cascade(domains, deep=True, client=client)

    auditor = DualAuditor()
    results = []
    canonical_domains = sorted(list(set(domains)))
    for dom in canonical_domains:
        res = await auditor.audit_site(dom, gear=gear, client=client)
        results.append(res)
    return results

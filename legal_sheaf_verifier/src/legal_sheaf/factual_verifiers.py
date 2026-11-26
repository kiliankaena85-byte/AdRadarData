"""
Factual Verifiers for 38-FZ Advertising Compliance (Zero-Mock, Anti-SSRF).

1. Per-Link erid Coverage Verifier (`verify_per_link_erid_coverage`):
   Deterministic, zero-network audit checking that EVERY distinct commercial/affiliate
   link in a publication carries a valid ORD erid token (or is lawfully covered by a
   single-advertiser text-body erid), catching partial marking gaps and malformed erids.

2. Anti-SSRF Redirect & Landing Page Verifier (`verify_link_redirect_and_landing`,
   `verify_article_links_live`, `analyze_landing_page_html`):
   Follows live HTTP redirects strictly via `safe_fetch_with_redirects` (Anti-SSRF),
   unwraps shorteners, detects concealed CPA/affiliate networks, UTM tracking, or hidden
   destination erid tokens, and inspects the final HTML landing page for objective
   commercial checkout/tariff/lead-capture evidence.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set

import httpx
from bs4 import BeautifulSoup

from legal_sheaf.forensics_v3 import extract_root_domain
from legal_sheaf.inn_erid_validators import (
    ERID_TEXT_REGEXES,
    ERID_URL_REGEX,
    NEUTRAL_PLATFORM_HOSTS,
    SHORTENER_HOSTS,
    STRICT_AFFILIATE_PARAM_REGEX,
    URL_IN_TEXT_REGEX,
    _is_host_in_set,
    has_custom_campaign_or_service_path,
    has_explicit_commercial_tracking,
    is_editorial_crypto_news_url,
    validate_erid_token_format,
)
from legal_sheaf.link_features import strip_links_footer
from legal_sheaf.net_safety import SSRFBlockedError, is_safe_url, safe_fetch_with_redirects

logger = logging.getLogger("legal_sheaf.factual_verifiers")

MESSENGER_HOSTS: Set[str] = {"t.me", "telegram.me", "vk.com", "max.ru"}

KNOWN_AFFILIATE_NETWORK_HOSTS: Set[str] = {
    "admitad.com",
    "fas.st",
    "alitems.com",
    "advcake.ru",
    "gdeslon.ru",
    "cityads.com",
    "actionpay.net",
    "leads.su",
    "leadgid.ru",
    "unicom24.ru",
    "pampadu.ru",
    "raf.tbank.ru",
    "travelpayouts.com",
    "tp.media",
    "cpa.ru",
    "clktrk.ru",
    "gotrack.ru",
    "salesdoubler.com",
    "partner.market.yandex.ru",
}

_META_REFRESH_REGEX = re.compile(
    r"""(?i)<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]+content\s*=\s*["'][^"']*url\s*=\s*([^"';\s>]+)""",
)
_LANDING_PRICE_REGEX = re.compile(
    r"(?i)(?:\b\d[\d\s\u00a0]{1,9}\s*(?:₽|руб(?:лей|\.)?)\b|"
    r"\bот\s+\d[\d\s\u00a0]{1,7}\s*(?:₽|руб|\$|€)|"
    r"\b(?:тарифн\w*\s+план\w*|тарифы|прайс-лист|стоимость\s+подписки|выбрать\s+тариф|цена\s+от)\b)"
)
_LANDING_CTA_REGEX = re.compile(
    r"(?i)\b(?:купить|в\s+корзину|оформить\s+заказ|оплатить|заказать|"
    r"выбрать\s+тариф|подключить\s+тариф|подключить\s+сервис|оставить\s+заявку|"
    r"записаться\s+на\s+курс|получить\s+демо|запросить\s+демо|попробовать\s+бесплатно|"
    r"начать\s+бесплатно|открыть\s+сч[её]т|оформить\s+подписку|забронировать)\b"
)
_LANDING_PAYMENT_CART_REGEX = re.compile(
    r"(?i)(?:yookassa|yoomoney|cloudpayments|robokassa|paykeeper|prodamus|"
    r"tinkoff\.ru/(?:acquiring|kassa)|tbank\.ru/kassa|sberbank\.ru/payment|"
    r"getcourse\.ru/pl/|stripe\.com|paymaster\.ru|unitpay\.ru|payanyway|"
    r"/(?:cart|checkout|basket|order/pay)(?:/|\?|\"|'|$)|"
    r"\b(?:оформление\s+заказа|перейти\s+к\s+оплате|добавить\s+в\s+корзину|онлайн-оплата|эквайринг)\b)"
)
_LANDING_SUBMIT_COMMERCIAL_REGEX = re.compile(
    r"(?i)\b(?:отправить\s+заявку|оставить\s+заявку|заказать|купить|оплатить|"
    r"получить\s+консультацию|получить\s+демо|записаться|подключить|рассчитать\s+стоимость)\b"
)


# =============================================================================
# 1. PER-LINK ERID COVERAGE VERIFIER (ZERO-NETWORK, DETERMINISTIC)
# =============================================================================

def _extract_url_erid_raw(url: str) -> Optional[str]:
    """Extracts the raw erid query parameter from a URL (even if format is invalid)."""
    if not url:
        return None
    m = ERID_URL_REGEX.search(url)
    if not m:
        return None
    return urllib.parse.unquote(m.group(1)).strip()


def _extract_text_only_erids(
    text: str,
    platform_ad_meta: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Extracts valid erid tokens explicitly declared in body text or platform metadata (not URL params)."""
    body_text = strip_links_footer(text or "")
    body_no_urls = URL_IN_TEXT_REGEX.sub(" ", body_text)
    found: Set[str] = set()
    for rgx in ERID_TEXT_REGEXES:
        for m in rgx.finditer(body_no_urls):
            tok = m.group(1).strip()
            if validate_erid_token_format(tok):
                found.add(tok)

    if platform_ad_meta:
        def _scan(obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in ("erid", "erid_token", "token", "ord_token", "marking_token") and isinstance(v, str):
                        if validate_erid_token_format(v.strip()):
                            found.add(v.strip())
                    else:
                        _scan(v)
            elif isinstance(obj, list):
                for item in obj:
                    _scan(item)

        _scan(platform_ad_meta)

    return sorted(found)


def _link_advertiser_key(url: str) -> str:
    """
    Computes an advertiser/campaign grouping key for a commercial link.
    Distinguishes different campaigns behind shared shorteners/trackers (e.g. clktrk.ru?s1=yesim vs s1=unisim).
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
        path_clean = (parsed.path or "").strip("/").lower()
        qs = urllib.parse.parse_qs(parsed.query)
    except Exception:
        return (url or "").strip().lower()[:80]

    if not host:
        return (url or "").strip().lower()[:80]

    if host in SHORTENER_HOSTS or _is_host_in_set(host, KNOWN_AFFILIATE_NETWORK_HOSTS):
        for param_k in ("s1", "sub1", "campaign", "utm_campaign", "offer", "ulp", "to", "url"):
            vals = qs.get(param_k)
            if vals and vals[0].strip():
                val_clean = urllib.parse.unquote(vals[0]).strip().lower()
                if val_clean.startswith(("http://", "https://")):
                    sub_host = urllib.parse.urlparse(val_clean).netloc.lower().split(":")[0].removeprefix("www.")
                    if sub_host:
                        return extract_root_domain(sub_host) or sub_host
                return f"{host}:{val_clean[:48]}"
        return f"{host}/{path_clean[:48]}" if path_clean else host

    if host in MESSENGER_HOSTS:
        seg = path_clean.split("/")[0] if path_clean else ""
        return f"{host}/{seg}" if seg else host

    return extract_root_domain(host) or host


def _is_plain_author_messenger_link(url: str) -> bool:
    """Returns True for plain messenger/social channel links without bot/ref/start/erid markers."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return False
    if host not in MESSENGER_HOSTS:
        return False
    if _extract_url_erid_raw(url) is not None:
        return False
    if STRICT_AFFILIATE_PARAM_REGEX.search(url):
        return False
    if re.search(r"(?i)(?:bot(?:\b|$|\?|/)|[?&]start=)", url):
        return False
    return True


def _brand_near_text_erid(body_text: str, adv_key: str) -> bool:
    """Checks whether the advertiser's domain stem appears within +/-250 chars of an explicit text erid."""
    if not body_text or not adv_key:
        return False
    stem = adv_key.split(":")[0].split("/")[0].split(".")[0].lower()
    if len(stem) < 4:
        return False
    body_lower = body_text.lower()
    for rgx in ERID_TEXT_REGEXES:
        for m in rgx.finditer(body_lower):
            w_start = max(0, m.start() - 250)
            w_end = min(len(body_lower), m.end() + 250)
            if stem in body_lower[w_start:w_end]:
                return True
    return False


def verify_per_link_erid_coverage(
    text: str,
    commercial_urls: Optional[List[str]] = None,
    raw_links: Optional[List[str]] = None,
    page_url: str = "",
    platform_ad_meta: Optional[Dict[str, Any]] = None,
    extra_url_erids: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Verifies erid token coverage for EVERY commercial/affiliate URL in an article.
    Detects:
      - Partial marking gaps (some links carry valid erid, other commercial/affiliate links omit it)
      - Malformed/corrupted erid tokens on URLs (URL_ERID_INVALID_FORMAT)
    Preserves zero-FP compliance for:
      - Single-advertiser articles with a valid body-text erid covering all links of that advertiser
      - Marked multi-link roundups where every affiliate link carries a valid ?erid=
      - Author personal Telegram channel links without bot/ref markers inside marked partner posts
    """
    raw_text = text or ""
    body_text = strip_links_footer(raw_text)
    extra_map = extra_url_erids or {}

    page_host = ""
    if page_url:
        try:
            page_host = urllib.parse.urlparse(page_url).netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            page_host = ""

    candidates: List[str] = []
    seen: Set[str] = set()

    def _add_candidate(u: str) -> None:
        if not isinstance(u, str):
            return
        cu = u.strip().rstrip(".,;:!?)")
        if cu.startswith(("http://", "https://")) and cu not in seen:
            seen.add(cu)
            candidates.append(cu)

    for u in (commercial_urls or []):
        _add_candidate(u)
    for u in (raw_links or []):
        if isinstance(u, str) and (ERID_URL_REGEX.search(u) or u.strip() in extra_map):
            _add_candidate(u)
    for m in URL_IN_TEXT_REGEX.finditer(raw_text):
        cu = m.group(0).strip().rstrip(".,;:!?)")
        if ERID_URL_REGEX.search(cu) or cu in extra_map:
            _add_candidate(cu)

    text_only_erids = _extract_text_only_erids(raw_text, platform_ad_meta=platform_ad_meta)

    active_items: List[Dict[str, Any]] = []
    for u in candidates:
        try:
            parsed = urllib.parse.urlparse(u)
            host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            continue
        if not host:
            continue

        raw_erid = _extract_url_erid_raw(u)
        extra_erid = extra_map.get(u)
        has_strict_aff = bool(STRICT_AFFILIATE_PARAM_REGEX.search(u))
        is_same_page = bool(page_host and (host == page_host or host.endswith("." + page_host)))
        is_neutral = (
            _is_host_in_set(host, NEUTRAL_PLATFORM_HOSTS)
            or host.endswith(".gov.ru")
            or host.endswith(".edu")
        )

        if (is_same_page or (is_neutral and host not in MESSENGER_HOSTS)) and not raw_erid and not extra_erid and not has_strict_aff:
            continue

        active_items.append(
            {
                "url": u,
                "host": host,
                "advertiser_key": _link_advertiser_key(u),
                "raw_erid": raw_erid,
                "extra_erid": extra_erid,
                "has_tracking": bool(has_explicit_commercial_tracking(u, page_url=page_url) or has_strict_aff),
                "is_plain_msg": _is_plain_author_messenger_link(u),
            }
        )

    any_messenger_has_erid = any(
        (item["host"] in MESSENGER_HOSTS)
        and bool((item["raw_erid"] and validate_erid_token_format(item["raw_erid"])) or item["extra_erid"])
        for item in active_items
    )

    non_skipped_keys: Set[str] = set()
    has_url_erid_links = False
    url_erids_found: Set[str] = set()
    for item in active_items:
        if item["is_plain_msg"] and not any_messenger_has_erid:
            continue
        non_skipped_keys.add(item["advertiser_key"])
        valid_direct = item["raw_erid"] if (item["raw_erid"] and validate_erid_token_format(item["raw_erid"])) else None
        valid_extra = item["extra_erid"] if (item["extra_erid"] and validate_erid_token_format(item["extra_erid"])) else None
        if valid_direct or valid_extra:
            has_url_erid_links = True
            url_erids_found.add(valid_direct or valid_extra)

    unique_advertisers_count = len(non_skipped_keys)

    per_link_details: List[Dict[str, Any]] = []
    marked_count = 0
    unmarked_links: List[str] = []
    invalid_erid_links: List[str] = []

    for item in active_items:
        u = item["url"]
        adv_key = item["advertiser_key"]
        raw_erid = item["raw_erid"]
        extra_erid = item["extra_erid"]

        if extra_erid and validate_erid_token_format(extra_erid):
            status = "URL_ERID_VALID"
            erid_val: Optional[str] = extra_erid
            reason = "Валидный токен erid подтвержден в целевом URL после раскрытия редиректа"
            marked_count += 1
        elif raw_erid and validate_erid_token_format(raw_erid):
            status = "URL_ERID_VALID"
            erid_val = raw_erid
            reason = "Валидный токен erid присутствует непосредственно в URL"
            marked_count += 1
        elif raw_erid is not None:
            status = "URL_ERID_INVALID_FORMAT"
            erid_val = raw_erid
            reason = f"Некорректный или поврежденный формат параметра erid='{raw_erid}'"
            invalid_erid_links.append(u)
        elif item["is_plain_msg"] and not any_messenger_has_erid:
            status = "SKIPPED_AUTHOR_MESSENGER"
            erid_val = None
            reason = "Нейтральная ссылка на канал автора без реферальных меток и бота"
        else:
            covered_by_text = False
            if text_only_erids:
                if unique_advertisers_count == 1:
                    covered_by_text = True
                elif (not has_url_erid_links) and unique_advertisers_count <= len(text_only_erids):
                    covered_by_text = True
                elif _brand_near_text_erid(body_text, adv_key) and not (has_url_erid_links and item["has_tracking"]):
                    covered_by_text = True

            if covered_by_text:
                status = "COVERED_BY_TEXT_ERID"
                erid_val = text_only_erids[0]
                reason = "Ссылка охвачена текстовым токеном erid единого рекламодателя"
                marked_count += 1
            else:
                status = "MISSING_ERID"
                erid_val = None
                reason = "Коммерческая/партнерская ссылка не содержит валидного токена erid"
                unmarked_links.append(u)

        per_link_details.append(
            {
                "url": u,
                "advertiser_key": adv_key,
                "status": status,
                "erid": erid_val,
                "has_tracking": item["has_tracking"],
                "reason": reason,
            }
        )

    unmarked_count = len(unmarked_links)
    invalid_count = len(invalid_erid_links)
    total_commercial = marked_count + unmarked_count + invalid_count

    has_partial_erid_gap = bool(
        invalid_count > 0
        or (
            marked_count >= 1
            and unmarked_count >= 1
            and (has_url_erid_links or unique_advertisers_count > len(text_only_erids))
        )
    )

    return {
        "total_commercial_links": total_commercial,
        "marked_links_count": marked_count,
        "unmarked_links_count": unmarked_count,
        "invalid_erid_links_count": invalid_count,
        "unique_advertisers_count": unique_advertisers_count,
        "text_erids": text_only_erids,
        "url_erids": sorted(url_erids_found),
        "has_partial_erid_gap": has_partial_erid_gap,
        "unmarked_links": unmarked_links,
        "invalid_erid_links": invalid_erid_links,
        "per_link_details": per_link_details,
    }


# =============================================================================
# 2. ANTI-SSRF REDIRECT & LANDING PAGE FACTUAL VERIFIER
# =============================================================================

def analyze_landing_page_html(html: str, final_url: str) -> Dict[str, Any]:
    """
    Pure, deterministic HTML analyzer for a resolved landing page.
    Requires >= 2 independent objective commercial signals (and non-editorial URL)
    to set `is_confirmed_commercial_landing=True`.
    """
    snippet_html = (html or "")[:120_000]
    soup = BeautifulSoup(snippet_html, "html.parser")

    page_title = ""
    if soup.title and soup.title.string:
        page_title = soup.title.string.strip()[:160]
    h1_tag = soup.find("h1")
    h1_text = h1_tag.get_text(" ", strip=True)[:160] if h1_tag else ""

    cta_matches: List[str] = []
    for elem in soup.find_all(["button", "a", "input"]):
        if elem.name == "input":
            if str(elem.get("type") or "").lower() not in ("submit", "button"):
                continue
            label = str(elem.get("value") or "").strip()
        else:
            label = elem.get_text(" ", strip=True)
        if not label or len(label) > 80:
            continue
        m_cta = _LANDING_CTA_REGEX.search(label)
        if m_cta:
            clean_lbl = re.sub(r"\s+", " ", label).strip()[:60]
            if clean_lbl not in cta_matches:
                cta_matches.append(clean_lbl)
            if len(cta_matches) >= 3:
                break
    has_buy_or_checkout_cta = len(cta_matches) > 0

    has_lead_capture_form = False
    for form_el in soup.find_all("form"):
        has_contact_input = bool(
            form_el.find("input", attrs={"type": re.compile(r"^(?:tel|email)$", re.I)})
            or form_el.find("input", attrs={"name": re.compile(r"(?:phone|tel|email|mail)", re.I)})
        )
        if not has_contact_input:
            continue
        form_text = form_el.get_text(" ", strip=True)
        submit_vals = " ".join(
            str(inp.get("value") or "")
            for inp in form_el.find_all("input", attrs={"type": re.compile(r"^(?:submit|button)$", re.I)})
        )
        if _LANDING_SUBMIT_COMMERCIAL_REGEX.search(f"{form_text} {submit_vals}"):
            has_lead_capture_form = True
            break

    m_pay = _LANDING_PAYMENT_CART_REGEX.search(snippet_html)
    has_payment_or_cart_marker = bool(m_pay)
    pay_quote = m_pay.group(0).strip()[:50] if m_pay else ""

    for junk in soup.find_all(["script", "style", "noscript", "svg"]):
        junk.decompose()
    visible_text = soup.get_text(" ", strip=True)
    m_price = _LANDING_PRICE_REGEX.search(visible_text)
    has_price_or_tariff = bool(m_price)
    price_quote = re.sub(r"\s+", " ", m_price.group(0)).strip()[:50] if m_price else ""

    has_commercial_path = bool(has_custom_campaign_or_service_path(final_url))
    is_editorial = bool(
        is_editorial_crypto_news_url(final_url)
        and not has_payment_or_cart_marker
        and not has_buy_or_checkout_cta
    )

    is_confirmed_commercial_landing = bool(
        (not is_editorial)
        and (
            (has_price_or_tariff and has_buy_or_checkout_cta)
            or (has_payment_or_cart_marker and (has_buy_or_checkout_cta or has_price_or_tariff))
            or (has_commercial_path and (has_buy_or_checkout_cta or has_lead_capture_form or has_price_or_tariff))
            or (has_buy_or_checkout_cta and has_lead_capture_form)
        )
    )

    evidence_quotes: List[str] = []
    if cta_matches:
        evidence_quotes.append(f"Кнопка/CTA: «{cta_matches[0]}»")
    if price_quote:
        evidence_quotes.append(f"Цена/тариф: «{price_quote}»")
    if pay_quote:
        evidence_quotes.append(f"Оплата/корзина: {pay_quote}")
    if has_lead_capture_form:
        evidence_quotes.append("Форма коммерческой заявки с полями контактов")

    return {
        "page_title": page_title,
        "h1": h1_text,
        "has_price_or_tariff": has_price_or_tariff,
        "has_buy_or_checkout_cta": has_buy_or_checkout_cta,
        "has_payment_or_cart_marker": has_payment_or_cart_marker,
        "has_lead_capture_form": has_lead_capture_form,
        "has_commercial_path": has_commercial_path,
        "is_editorial_or_informational_landing": is_editorial,
        "is_confirmed_commercial_landing": is_confirmed_commercial_landing,
        "commercial_evidence_quotes": evidence_quotes[:4],
    }


def _ssrf_blocked_result(orig_url: str, final_url: str, hops: List[str], reason: str, started_at: float) -> Dict[str, Any]:
    final_host = urllib.parse.urlparse(final_url).netloc.lower().split(":")[0].removeprefix("www.")
    return {
        "original_url": orig_url,
        "final_url": final_url,
        "final_host": final_host,
        "status": "SSRF_BLOCKED",
        "status_code": 0,
        "ssrf_blocked": True,
        "error": reason,
        "redirect_hops": hops,
        "redirect_count": max(0, len(hops) - 1),
        "hidden_erid_revealed": None,
        "hidden_affiliate_network_detected": False,
        "hidden_tracking_revealed": False,
        "is_confirmed_commercial_landing": False,
        "landing_signals": {},
        "commercial_evidence_quotes": [],
        "latency_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
    }


async def verify_link_redirect_and_landing(
    url: str,
    client: httpx.AsyncClient,
    article_url: str = "",
    timeout: float = 5.0,
    max_hops: int = 5,
) -> Dict[str, Any]:
    """
    Safely follows HTTP redirects via `safe_fetch_with_redirects` (Anti-SSRF),
    inspects every hop for hidden CPA networks / UTM tracking / revealed erid tokens,
    and analyzes the destination HTML landing page for commercial evidence.
    """
    t0 = time.perf_counter()
    orig_url = (url or "").strip()
    try:
        orig_host = urllib.parse.urlparse(orig_url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        orig_host = ""

    orig_erid = _extract_url_erid_raw(orig_url)
    orig_has_valid_erid = bool(orig_erid and validate_erid_token_format(orig_erid))
    orig_had_tracking = bool(
        has_explicit_commercial_tracking(orig_url, page_url=article_url)
        and orig_host not in SHORTENER_HOSTS
    )

    try:
        resp, final_url, hops = await safe_fetch_with_redirects(
            client,
            orig_url,
            max_hops=max_hops,
            timeout=timeout,
            method="GET",
        )
    except SSRFBlockedError as e:
        return _ssrf_blocked_result(orig_url, orig_url, [orig_url], str(e), t0)

    # Optional 1-hop <meta http-equiv="refresh"> follow (strictly Anti-SSRF verified)
    if resp is not None and resp.status_code == 200:
        ctype = str(resp.headers.get("content-type") or "").lower()
        if "html" in ctype or not ctype:
            m_meta = _META_REFRESH_REGEX.search(resp.text[:8000])
            if m_meta:
                meta_target = urllib.parse.urljoin(final_url, m_meta.group(1).strip().strip("\"'"))
                if meta_target != final_url and meta_target not in hops:
                    safe_meta, meta_reason = is_safe_url(meta_target)
                    if not safe_meta:
                        return _ssrf_blocked_result(
                            orig_url,
                            meta_target,
                            hops + [meta_target],
                            f"Anti-SSRF Blocked meta-refresh: {meta_reason}",
                            t0,
                        )
                    try:
                        resp2, final2, hops2 = await safe_fetch_with_redirects(
                            client,
                            meta_target,
                            max_hops=2,
                            timeout=timeout,
                            method="GET",
                        )
                        if resp2 is not None:
                            resp = resp2
                        final_url = final2
                        hops = hops + [h for h in hops2 if h not in hops]
                    except SSRFBlockedError as e:
                        return _ssrf_blocked_result(orig_url, meta_target, hops + [meta_target], str(e), t0)

    try:
        final_host = urllib.parse.urlparse(final_url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        final_host = ""

    hidden_erid_revealed: Optional[str] = None
    hidden_affiliate_network_detected = False
    hidden_tracking_revealed = False

    for idx_h, hop_u in enumerate(hops):
        try:
            hop_host = urllib.parse.urlparse(hop_u).netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            hop_host = ""

        if hop_host and _is_host_in_set(hop_host, KNOWN_AFFILIATE_NETWORK_HOSTS):
            hidden_affiliate_network_detected = True

        hop_erid = _extract_url_erid_raw(hop_u)
        if hop_erid and validate_erid_token_format(hop_erid) and not orig_has_valid_erid and hidden_erid_revealed is None:
            hidden_erid_revealed = hop_erid

        if idx_h > 0:
            if has_explicit_commercial_tracking(hop_u, page_url=article_url) or STRICT_AFFILIATE_PARAM_REGEX.search(hop_u):
                if not orig_had_tracking:
                    hidden_tracking_revealed = True

    landing_signals: Dict[str, Any] = {}
    is_confirmed_commercial = False
    evidence_quotes: List[str] = []
    status_code = int(resp.status_code) if resp is not None else 0
    status = "FETCH_FAILED" if resp is None else f"HTTP_{status_code}"

    if resp is not None and status_code == 200:
        ctype = str(resp.headers.get("content-type") or "").lower()
        if "html" in ctype or not ctype:
            status = "OK"
            landing_signals = analyze_landing_page_html(resp.text, final_url)
            is_confirmed_commercial = bool(landing_signals.get("is_confirmed_commercial_landing"))
            evidence_quotes = list(landing_signals.get("commercial_evidence_quotes") or [])
        else:
            status = "NON_HTML"

    if len(hops) > 1:
        evidence_quotes.insert(0, f"Редирект ({len(hops) - 1} переход): {orig_url} -> {final_url}")

    return {
        "original_url": orig_url,
        "final_url": final_url,
        "final_host": final_host,
        "status": status,
        "status_code": status_code,
        "ssrf_blocked": False,
        "redirect_hops": hops,
        "redirect_count": max(0, len(hops) - 1),
        "hidden_erid_revealed": hidden_erid_revealed,
        "hidden_affiliate_network_detected": hidden_affiliate_network_detected,
        "hidden_tracking_revealed": hidden_tracking_revealed,
        "is_confirmed_commercial_landing": is_confirmed_commercial,
        "landing_signals": landing_signals,
        "commercial_evidence_quotes": evidence_quotes[:4],
        "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
    }


async def verify_article_links_live(
    urls: List[str],
    client: httpx.AsyncClient,
    article_url: str = "",
    max_links: int = 5,
    timeout: float = 5.0,
    probe_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Concurrently verifies up to `max_links` commercial URLs (prioritizing shorteners first,
    skipping messenger previews) with optional cross-article result caching.
    """
    shorteners: List[str] = []
    websites: List[str] = []
    seen: Set[str] = set()

    for raw_u in (urls or []):
        if not isinstance(raw_u, str):
            continue
        u = raw_u.strip().rstrip(".,;:!?)")
        if not u.startswith(("http://", "https://")) or u in seen:
            continue
        try:
            host = urllib.parse.urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
        except Exception:
            continue
        if not host or host in MESSENGER_HOSTS:
            continue
        seen.add(u)
        if host in SHORTENER_HOSTS or _is_host_in_set(host, KNOWN_AFFILIATE_NETWORK_HOSTS):
            shorteners.append(u)
        else:
            websites.append(u)

    selected = (shorteners + websites)[: max(1, int(max_links))]
    if not selected:
        return []

    sem = asyncio.Semaphore(4)

    async def _probe_one(target_u: str) -> Dict[str, Any]:
        if probe_cache is not None and target_u in probe_cache:
            return dict(probe_cache[target_u])
        async with sem:
            res = await verify_link_redirect_and_landing(
                target_u,
                client=client,
                article_url=article_url,
                timeout=timeout,
            )
        if probe_cache is not None:
            probe_cache[target_u] = dict(res)
        return res

    results = await asyncio.gather(*(_probe_one(u) for u in selected))
    return list(results)

"""
AdRadar Forensics Engine v3.0 (Deep Multi-Layer Cross-Forensics & Perimeter Investigation).
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and actual Russian court/RKN practice.

Eliminates architectural blindness of single-page audits:
1. Perimeter & GeoIP Discovery:
   - discover_perimeter_hosts: Root domain extraction, button/link extraction (panel.*, app.*, lk.*),
     Policy/Oferta parsing, crt.sh query (5s timeout), live HTTP probing.
   - inspect_infrastructure_geo_and_mx: Resolves A records for all perimeter hosts, RIPE NCC MaxMind
     GeoIP check (/24 cache), DNS-over-HTTPS MX verification.
   - inspect_external_js_endpoints: Downloads external JS widgets (<= 512 KB), extracts embedded hosts/IPv4,
     detects foreign server backends (e.g., AthenaChat -> GB / Leaseweb UK).
2. Legal Forensics & Social Networks:
   - meta_extremist_vs_blocked_rule: Accurate Meta vs blocked LinkedIn/X rule, footnote awareness.
   - policy_and_terms_forensics: 6 precise defect detectors for Policy & B2C Oferta.
   - detect_missing_seller_requisites: ст. 9 ЗоЗПП, п. 19 ПП РФ № 2463 -> ч. 1 ст. 14.8 КоАП РФ.
   - resolve_phoenix_clones_in_fns: Cross-registry detection of liquidated predecessor + active clone.
   - detect_regalia_and_pdf_chronology_mismatch: OGRN registration year vs older copyright/patents/reviews & pypdf audit.
   - detect_instant_protocol_triggers: Instagram/Facebook Meta links (ч. 1 ст. 20.3 КоАП), credit without PSK (ст. 28 38-ФЗ).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import socket
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import httpx
from bs4 import BeautifulSoup

try:
    import pypdf
except ImportError:
    pypdf = None

logger = logging.getLogger("legal_sheaf.forensics_v3")


class TriState(str):
    """Three-valued logic under Kleene semantics: TRUE | FALSE | UNVERIFIED."""
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNVERIFIED = "UNVERIFIED"


# In-memory subnet /24 Geo cache to avoid repetitive queries
_GEO_SUBNET_CACHE: Dict[str, Dict[str, Any]] = {}

TRUSTED_RUSSIAN_SCRIPT_DOMAINS: Set[str] = {
    "mc.yandex.ru", "yastatic.net", "api-maps.yandex.ru", "smartcaptcha.cloud.yandex.ru",
    "vk.com", "vk.ru", "userapi.com", "mail.ru", "tinkoff.ru", "sberbank.ru",
    "uiscom.ru", "comagic.ru", "bitrix24.ru", "cdn-ru.bitrix24.ru", "jivo.ru",
    "jivosite.com", "livetex.ru", "carrotquest.io", "calltouch.ru", "roistat.com"
}

STATIC_CDNS: Set[str] = {
    "jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com",
    "fonts.googleapis.com", "fonts.gstatic.com", "ajax.googleapis.com",
    "code.jquery.com", "cdn.tailwindcss.com", "bootstrapcdn.com"
}

IGNORED_DOCS_AND_STANDARDS_DOMAINS: Set[str] = {
    "w3.org", "schema.org", "socket.io", "mozilla.org", "github.com",
    "github.io", "reactjs.org", "react.dev", "vuejs.org", "nextjs.org",
    "npmjs.com", "npmjs.org", "localhost", "t.me", "telegram.me",
    "telegram.org", "wa.me", "whatsapp.com", "marked.js",
}


def extract_root_domain(domain: str) -> str:
    """Extracts second-level domain name (e.g. 'c3.ru' from 'c3.c3.ru' or 'athenachat.ai' from 'ru.athenachat.ai')."""
    clean = domain.strip().lower()
    clean = re.sub(r"^https?://", "", clean).split("/")[0].split(":")[0]
    if clean.startswith("www."):
        clean = clean[4:]
    parts = clean.split(".")
    if len(parts) <= 2:
        return clean
    # Handle known 2-level TLDs (.co.uk, .com.ru, etc.)
    if parts[-2] in ("com", "net", "org", "co", "spb", "msk", "gov", "edu") and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


async def resolve_host_ip_with_fallback(host: str, client: Optional[httpx.AsyncClient] = None) -> List[str]:
    """Resolves IP addresses for host, using DoH fallback if local resolver returns RFC2544 benchmark/sandbox IPs."""
    clean_host = host.strip().lower().split(":")[0]
    ips: List[str] = []

    # 1. Try local socket
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(clean_host, None, family=socket.AF_INET)
        for it in addr_info:
            ip = it[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    # If socket returned sandbox IP (198.18.*) or empty, query Google DoH
    is_sandbox = any(ip.startswith("198.18.") for ip in ips)
    if not ips or is_sandbox:
        close_c = False
        if client is None:
            client = httpx.AsyncClient(timeout=4.0)
            close_c = True
        try:
            doh_resp = await client.get(
                f"https://dns.google/resolve?name={clean_host}&type=A",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if doh_resp.status_code == 200:
                data = doh_resp.json()
                real_ips = [a.get("data") for a in data.get("Answer", []) if a.get("type") == 1 and a.get("data")]
                if real_ips:
                    return real_ips
        except Exception:
            pass
        finally:
            if close_c:
                await client.aclose()

    return ips


async def query_ripe_geo_ip(ip: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    """Queries RIPE NCC MaxMind GeoLite API with /24 subnet caching."""
    parts = ip.split(".")
    subnet_key = ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip

    if subnet_key in _GEO_SUBNET_CACHE:
        return _GEO_SUBNET_CACHE[subnet_key]

    close_c = False
    if client is None:
        client = httpx.AsyncClient(timeout=4.0)
        close_c = True

    geo_result = {
        "ip": ip,
        "country": "UNKNOWN",
        "city": "",
        "org": "",
        "is_foreign": TriState.UNVERIFIED,
        "status": TriState.UNVERIFIED,
    }

    try:
        try:
            url = f"https://stat.ripe.net/data/maxmind-geo-lite/data.json?resource={ip}"
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                payload = resp.json().get("data", {})
                locs = payload.get("located_resources", [{}])[0].get("locations", [{}])
                if locs and isinstance(locs, list) and len(locs) > 0:
                    c = locs[0].get("country", "")
                    city = locs[0].get("city", "")
                    if c:
                        geo_result["country"] = c
                        geo_result["city"] = city
                        geo_result["is_foreign"] = bool(c.upper() != "RU")
                        geo_result["status"] = "OK"
        except Exception as e:
            logger.debug(f"RIPE GeoIP query failed for {ip}: {e}")

        # Fallback to ip-api.com if RIPE had no data
        if geo_result["country"] == "UNKNOWN":
            try:
                url_ip = f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city,org,as"
                resp2 = await client.get(url_ip, headers={"User-Agent": "Mozilla/5.0"}, timeout=3.0)
                if resp2.status_code == 200:
                    d2 = resp2.json()
                    c2 = d2.get("countryCode", "")
                    if c2:
                        geo_result["country"] = c2
                        geo_result["city"] = d2.get("city", "")
                        geo_result["org"] = d2.get("org") or d2.get("as", "")
                        geo_result["is_foreign"] = bool(c2.upper() != "RU")
                        geo_result["status"] = "OK"
            except Exception:
                pass
    finally:
        if close_c:
            await client.aclose()

    # Never cache unverified/failed responses in memory
    if geo_result.get("status") == "OK" and geo_result.get("country") != "UNKNOWN":
        _GEO_SUBNET_CACHE[subnet_key] = geo_result
    return geo_result


# -----------------------------------------------------------------------------
# 1. PERIMETER & NETWORK INVESTIGATION
# -----------------------------------------------------------------------------

async def discover_perimeter_hosts(
    domain: str,
    full_raw_html: Union[str, List[str]] = "",
    policy_html: str = "",
    oferta_html: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> List[str]:
    """
    Discovers active perimeter hosts:
    1. Extracts root 2nd-level domain (e.g. c3.ru, athenachat.ai).
    2. Extracts candidate subdomains from:
       (a) links and buttons for Login/Register/Start (panel.*, app.*, lk.*, auth.*, login.*, cabinet.*);
       (b) URLs inside Policy, Oferta and full site HTML;
       (c) crt.sh query with 5s timeout (graceful fallback).
    3. Validates live HTTP availability (HTTP 200/301/302/403).
    """
    root = extract_root_domain(domain)
    candidates: Set[str] = {domain.lower()}

    # Normalize html string
    if isinstance(full_raw_html, list):
        html_str = " ".join(full_raw_html)
    else:
        html_str = full_raw_html or ""

    combined = html_str + " " + (policy_html or "") + " " + (oferta_html or "")

    # (a) Extract from auth/login buttons & links
    auth_kw = re.compile(r"(?i)(?:войти|вход|регистрация|зарегистрироваться|начать|личный\s+кабинет|панель|кабинет|app|login|sign\s*in|sign\s*up|dashboard)")
    soup = BeautifulSoup(combined[:250000], "html.parser")
    for a_tag in soup.find_all(["a", "button"], href=True):
        href = a_tag.get("href", "").strip()
        text = a_tag.get_text()
        if auth_kw.search(text) or auth_kw.search(href) or any(sub in href.lower() for sub in ["panel.", "app.", "lk.", "auth.", "login.", "cabinet.", "my."]):
            try:
                parsed = urllib.parse.urlparse(href)
                h = parsed.netloc.lower().split(":")[0]
                if h.startswith("www."):
                    h = h[4:]
                if h and (h.endswith("." + root) or h == root):
                    candidates.add(h)
            except Exception:
                pass

    # (b) Regex for any subdomains of root in text
    sub_pattern = re.compile(r"(?:https?://)?([a-zA-Z0-9][-a-zA-Z0-9]*\." + re.escape(root) + r")\b", re.I)
    for m in sub_pattern.finditer(combined):
        cand = m.group(1).lower().strip()
        if cand.startswith("www."):
            cand = cand[4:]
        if cand.endswith(root):
            candidates.add(cand)

    # (c) Query crt.sh (graceful fallback on timeout/error)
    close_c = False
    if client is None:
        client = httpx.AsyncClient(timeout=5.0)
        close_c = True

    try:
        crt_url = f"https://crt.sh/?q=%.{root}&output=json"
        resp = await client.get(crt_url, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            data = resp.json()
            for it in data[:35]:
                nv = it.get("name_value", "")
                for sub in nv.split("\n"):
                    s = sub.strip().lower()
                    if s.startswith("*."):
                        s = s[2:]
                    if s.startswith("www."):
                        s = s[4:]
                    if s.endswith(root):
                        candidates.add(s)
    except Exception as e:
        logger.debug(f"crt.sh query skipped/timed out for {root}: {e}")

    # (d) Validate live HTTP availability
    live_hosts: List[str] = []
    for cand in sorted(candidates):
        if cand in (root, domain.lower()):
            live_hosts.append(cand)
            continue
        try:
            r = await client.get(f"https://{cand}", timeout=3.0, follow_redirects=True)
            if r.status_code in (200, 301, 302, 307, 308, 403):
                live_hosts.append(cand)
        except Exception:
            try:
                r_http = await client.get(f"http://{cand}", timeout=2.0, follow_redirects=True)
                if r_http.status_code in (200, 301, 302, 307, 308, 403):
                    live_hosts.append(cand)
            except Exception:
                pass

    if close_c:
        await client.aclose()

    return sorted(list(set(live_hosts))) if live_hosts else [domain]


# Alias for backwards compatibility
discover_related_subdomains = discover_perimeter_hosts


async def inspect_infrastructure_geo_and_mx(
    hosts: Union[str, List[str]],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    1. Resolves A-records of main site and discovered subdomains.
    2. Determines real country and city via RIPE NCC API (/24 subnet cache).
       If server is outside RU (!= 'RU', e.g. SG, NL, US, GB), flags cross-border storage node.
    3. Queries DNS-over-HTTPS MX records of root domain to detect foreign corporate email hosts.
    """
    if isinstance(hosts, str):
        target_hosts = [hosts]
    else:
        target_hosts = list(hosts)

    if not target_hosts:
        return {"findings": [], "ips": [], "geo": {}, "mx_records": []}

    primary_domain = target_hosts[0]
    root = extract_root_domain(primary_domain)
    # Canonical deterministic sort: primary first, subdomains alphabetically
    canonical_target_hosts = [primary_domain] + sorted([h for h in set(target_hosts) if h != primary_domain])

    close_c = False
    if client is None:
        client = httpx.AsyncClient(timeout=4.0)
        close_c = True

    findings: List[Dict[str, Any]] = []
    all_ips: List[str] = []
    primary_ips: List[str] = []
    geo_map: Dict[str, Dict[str, Any]] = {}

    for host in canonical_target_hosts[:6]:
        ips = await resolve_host_ip_with_fallback(host, client=client)
        if host == primary_domain:
            primary_ips = list(ips)
        for ip in ips:
            if ip not in all_ips:
                all_ips.append(ip)
            geo = await query_ripe_geo_ip(ip, client=client)
            geo_map[host] = geo
            if geo.get("is_foreign") is True:
                findings.append({
                    "signature": "host_geo_foreign_risk",
                    "severity": "RED_REAL_FINE",
                    "legal_basis": "ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
                    "description": (
                        f"Сервер сайта/поддомена {host} размещен за пределами РФ (Страна: {geo.get('country')}, "
                        f"Город: {geo.get('city')}, IP: {ip}). "
                        f"Первичный сбор персональных данных на иностранном хостинге влечет штраф от 1 000 000 до 6 000 000 руб."
                    ),
                    "host": host,
                    "ip": ip,
                    "country": geo.get("country"),
                })

    # Check MX records
    mx_records: List[str] = []
    foreign_mx_detected = False
    try:
        doh_resp = await client.get(
            f"https://dns.google/resolve?name={root}&type=MX",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if doh_resp.status_code == 200:
            for ans in doh_resp.json().get("Answer", []):
                val = ans.get("data", "")
                if val:
                    mx_records.append(val)
                    if any(f_host in val.lower() for f_host in [
                        "google.com", "googlemail.com", "outlook.com",
                        "protection.outlook.com", "zoho.com", "mailgun.org",
                        "privateemail.com"
                    ]):
                        foreign_mx_detected = True
    except Exception as e:
        logger.debug(f"DoH MX query failed for {root}: {e}")

    if foreign_mx_detected:
        findings.append({
            "signature": "foreign_mx_email_transfer_risk",
            "severity": "ORANGE_REGULATORY_DEMAND",
            "legal_basis": "ст. 12 152-ФЗ, ст. 49 248-ФЗ",
            "description": (
                f"Корпоративная почта домена {root} обслуживается иностранным провайдером ({', '.join(mx_records)}). "
                f"Заявки с форм, отправляемые на корпоративный email, образуют трансграничную передачу ПДн без локализации."
            ),
            "mx_records": mx_records,
        })

    if close_c:
        await client.aclose()

    return {
        "domain": primary_domain,
        "root_domain": root,
        "ips": primary_ips if primary_ips else (all_ips[:1] if all_ips else []),
        "all_ips": all_ips,
        "geo": geo_map.get(primary_domain, {}),
        "geo_map": geo_map,
        "mx_records": mx_records,
        "findings": findings,
    }


# Backwards compatibility
inspect_dns_mx_and_host_geo = inspect_infrastructure_geo_and_mx


async def inspect_external_js_endpoints(
    subdomains_html: Dict[str, str],
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """
    Downloads external JS scripts (<= 512 KB) across discovered perimeter hosts,
    extracts embedded API endpoints and public IPs, resolving their GeoIP.
    Catches hidden foreign widgets (such as AthenaChat sending data to GB/US).
    """
    close_c = False
    if client is None:
        client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=5.0,
            follow_redirects=True,
        )
        close_c = True

    detected_leaks: List[Dict[str, Any]] = []
    processed_scripts: Set[str] = set()

    for sub_name, html in sorted(subdomains_html.items(), key=lambda kv: kv[0]):
        script_srcs = sorted(list(set(re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I))))
        for src in script_srcs:
            if not src.startswith("http"):
                continue
            parsed_src = urllib.parse.urlparse(src)
            src_host = parsed_src.netloc.lower().split(":")[0]
            if src_host.startswith("www."):
                src_host = src_host[4:]

            if (
                any(src_host.endswith(td) for td in TRUSTED_RUSSIAN_SCRIPT_DOMAINS)
                or src_host in STATIC_CDNS
                or any(src_host.endswith("." + c) for c in STATIC_CDNS)
            ):
                continue

            if src in processed_scripts:
                continue
            processed_scripts.add(src)

            try:
                resp = await client.get(src)
                if resp.status_code == 200 and len(resp.content) <= 512 * 1024:
                    js_text = resp.text
                    urls_in_js = set(re.findall(r'https?://([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})(?:/[^\s"\'`<>)]*)?', js_text))
                    athena_match = re.search(r'https?://([a-zA-Z0-9.-]*athenachat\.ai[^\s"\'`)]*)', js_text)
                    if athena_match:
                        urls_in_js.add("api.athenachat.ai")

                    for target_host in sorted(list(urls_in_js)):
                        t_host = target_host.split("/")[0].split(":")[0].lower()
                        if t_host.startswith("www."):
                            t_host = t_host[4:]
                        if (
                            any(t_host.endswith(td) for td in TRUSTED_RUSSIAN_SCRIPT_DOMAINS)
                            or t_host in STATIC_CDNS
                            or any(t_host.endswith("." + c) for c in STATIC_CDNS)
                        ):
                            continue
                        if (
                            t_host in IGNORED_DOCS_AND_STANDARDS_DOMAINS
                            or any(t_host.endswith("." + d) for d in IGNORED_DOCS_AND_STANDARDS_DOMAINS)
                        ):
                            continue

                        t_ips = await resolve_host_ip_with_fallback(t_host, client=client)
                        if t_ips:
                            geo = await query_ripe_geo_ip(t_ips[0], client=client)
                            if geo.get("is_foreign") is True:
                                detected_leaks.append({
                                    "signature": "foreign_widget_data_leak",
                                    "subdomain": sub_name,
                                    "script_src": src,
                                    "target_host": t_host,
                                    "target_ip": t_ips[0],
                                    "country": geo.get("country"),
                                    "city": geo.get("city"),
                                    "org": geo.get("org"),
                                    "legal_basis": "ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
                                    "description": (
                                        f"Внешний скрипт виджета/чата {src} на субдомене {sub_name} "
                                        f"осуществляет прямую передачу данных пользователей на зарубежный узел "
                                        f"{t_host} (IP {t_ips[0]}, Страна: {geo.get('country')}, Город: {geo.get('city')}). "
                                        f"Нарушение локализации баз данных граждан РФ влечет штраф от 1 000 000 до 6 000 000 руб."
                                    ),
                                })
            except Exception as e:
                logger.debug(f"Failed to inspect external JS {src}: {e}")

    if close_c:
        await client.aclose()

    return detected_leaks


# -----------------------------------------------------------------------------
# 2. SOCIAL NETWORKS: META EXTREMIST VS BLOCKED RULE
# -----------------------------------------------------------------------------

def meta_extremist_vs_blocked_rule(full_raw_html: str) -> List[Dict[str, Any]]:
    """
    Evaluates social networks in full raw HTML:
    - LinkedIn, X/Twitter: NOT an extremist violation (blocked, but not extremist under 20.3 KoAP).
    - WhatsApp* / Instagram* in text with a Meta banned/extremist footnote in full HTML -> CLEAN (0 violations).
    - Triggers ч. 1 ст. 20.3 КоАП РФ ONLY if:
      (a) Graphic symbolism/icon of Instagram/Facebook (svg/img/icon);
      (b) Direct link <a href="...instagram.com/..."> to profile without footnote or prohibited link.
    """
    findings: List[Dict[str, Any]] = []

    # 1. Check if Meta footnote exists anywhere in the full HTML
    meta_disclaimer_pattern = re.compile(
        r"(?i)(?:"
        r"деятельность\s+(?:организации\s+)?meta\b.*?(?:запрещен|экстремист)"
        r"|meta\b.*?(?:признан[а-я]*|запрещен[а-я]*|экстремист)"
        r"|организаци[а-я]*\s+meta\b.*?(?:запрещен|экстремист)"
        r"|(?:instagram|facebook|whatsapp).*?meta.*?(?:запрещен|экстремист)"
        r")"
    )
    has_meta_disclaimer = bool(meta_disclaimer_pattern.search(full_raw_html))

    # 2. Check for graphic symbolism / icons of Instagram / Facebook
    icon_pattern = re.compile(
        r'(?i)(?:<svg[^>]+(?:class|id|data-icon)=["\'][^"\']*(?:instagram|facebook|fb-icon|insta-icon)[^"\']*["\']|<img[^>]+(?:src|alt|title)=["\'][^"\']*(?:instagram|facebook|fb-icon|insta-icon)[^"\']*["\']|<i[^>]+class=["\'][^"\']*(?:fa-instagram|fa-facebook|icon-instagram|icon-facebook)[^"\']*["\'])'
    )
    icon_match = icon_pattern.search(full_raw_html)

    # 3. Check for direct profile links to Instagram / Facebook
    link_pattern = re.compile(
        r'(?i)<a\s+[^>]*href=["\']https?://(?:www\.)?(?:instagram\.com|facebook\.com)/[^"\']+["\']'
    )
    link_match = link_pattern.search(full_raw_html)

    if icon_match or link_match:
        matched_snippet = (icon_match.group(0) if icon_match else link_match.group(0))[:120]
        findings.append({
            "signature": "extremist_meta_social_links_20_3",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ч. 1 ст. 20.3 КоАП РФ",
            "description": (
                "Публичное демонстрирование символики или размещение активных гиперссылок на ресурсы "
                "экстремистской организации Meta (Instagram/Facebook) в верстке страницы."
            ),
            "raw_snippet": matched_snippet,
            "quote": "Instagram/Facebook",
        })

    return findings


# -----------------------------------------------------------------------------
# 3. POLICY & B2C OFERTA TEXT FORENSICS
# -----------------------------------------------------------------------------

def policy_and_terms_forensics(
    audited_domain: str,
    policy_html: str = "",
    oferta_html: str = "",
    site_html: str = "",
    policy_text: str = "",
    active_inn: Optional[str] = None,
    **kwargs: Any,
) -> List[Dict[str, Any]]:
    """
    Forensic defect detectors for Privacy Policy and B2C Terms of Service (Oferta):
    1. policy-domain-mismatch: Policy explicitly written for another domain or links lead to untranslated /terms.
    2. implicit-browse-consent-invalid: 'Использование сайта означает согласие' (156-FZ / ст. 9 152-FZ).
    3. analytics-denied-as-pd: 'Техническая аналитика / cookies не являются ПД' (ст. 13.11 КоАП РФ, Case 05-1297/422/2022).
    4. hidden-subprocessors-list: 'Перечень третьих лиц предоставляется по запросу' (ч. 3 ст. 6, ст. 18.1 152-FZ).
    5. illegal-no-refund-b2c-clause: 'Оплаченные суммы возврату не подлежат' in B2C oferta (ст. 16, 32 ЗоЗПП -> 14.8(2) KoAP)
       or missing physical address (ч. 2 ст. 10 149-FZ).
    6. unprotected-affiliate-program: Affiliate/referral program without erid/ORD marking requirements (ч. 16 ст. 14.3 KoAP).
    """
    if not policy_html and policy_text:
        policy_html = policy_text
    findings: List[Dict[str, Any]] = []
    clean_domain = audited_domain.strip().lower()

    # 1. policy-domain-mismatch
    if policy_html:
        dom_matches = re.findall(r"(?i)(?:веб-сайта?|сайта?|ресурса?)\s+https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", policy_html)
        if dom_matches:
            target_in_policy = dom_matches[0].lower().strip()
            if target_in_policy != clean_domain and clean_domain not in target_in_policy:
                findings.append({
                    "signature": "policy_domain_mismatch_paradox",
                    "tier": "RED_REAL_FINE",
                    "legal_basis": "ч. 3 ст. 13.11 КоАП РФ, ч. 2 ст. 18.1 152-ФЗ",
                    "description": (
                        f"Парадокс Политики конфиденциальности: документ на домене {clean_domain} "
                        f"составлен исключительно для стороннего ресурса '{target_in_policy}'. "
                        f"Сбор данных на текущем домене осуществляется без надлежащей опубликованной Политики (ч. 3 ст. 13.11 КоАП РФ)."
                    ),
                    "referenced_domain": target_in_policy,
                    "audited_domain": clean_domain,
                })

    # Check untranslated English links under forms
    if re.search(r'(?i)href=["\'][^"\']*/(?:terms|terms-of-service|privacy-policy)["\'][^>]*>\s*(?:terms\s+of\s+service|privacy\s+policy)\s*<', site_html):
        findings.append({
            "signature": "policy_domain_mismatch_paradox",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ч. 3 ст. 13.11 КоАП РФ, ч. 2 ст. 18.1 152-ФЗ",
            "description": "Ссылки на юридические документы под формой ведут на непереведенные англоязычные Terms/Policy",
        })

    combined_policy = (policy_html + " " + site_html).lower()

    # 2. implicit-browse-consent-invalid
    if re.search(r"(?i)(?:использование\s+сайта\s+(?:означает|является|подтверждает)|продолжая\s+использование\s+сайта|факт\s+использования\s+сайта|просмотр\s+сайта\s+(?:означает|подтверждает))", combined_policy):
        findings.append({
            "signature": "implicit_browse_consent_invalid",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ, 156-ФЗ",
            "description": "Презумпция согласия при просмотре сайта ничтожна (нарушение ст. 9 152-ФЗ и 156-ФЗ)",
        })

    # 3. analytics-denied-as-pd
    if re.search(r"(?i)(?:техническ(?:ая|ие)\s+(?:информация|данные)[^.\n]{0,80}(?:не\s+явля(?:ются|ется)|не\s+относят?ся\s+к)\s+персональн|файлы\s+cookies?\s+не\s+являются\s+персональными|cookies?\s+не\s+явля(?:ются|ется)\s+персональн)", combined_policy):
        findings.append({
            "signature": "analytics_denied_as_pd",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ч. 1 ст. 13.11 КоАП РФ, ст. 3 152-ФЗ, Постановление мирового судьи с/у № 422 Таганского района г. Москвы по делу № 05-1297/422/2022",
            "description": "Отказ признавать техническую аналитику и cookies персональными данными (ст. 13.11 КоАП РФ, прецедент № 05-1297/422/2022)",
        })

    # 4. hidden-subprocessors-list
    if re.search(r"(?i)(?:перечень|список)\s+(?:субобработчиков|третьих\s+лиц|партнеров)[^.\n]{0,80}(?:предоставляется|направляется|доступен|может\s+быть\s+предоставлен)\s+по\s+(?:письменному\s+)?запросу", combined_policy):
        findings.append({
            "signature": "hidden_subprocessors_list",
            "tier": "ORANGE_REGULATORY_DEMAND",
            "legal_basis": "ч. 1 ст. 13.11 КоАП РФ, ч. 3 ст. 6, ст. 18.1 152-ФЗ",
            "description": "Сокрытие перечня субобработчиков: отсылка к предоставлению списка третьих лиц 'по запросу'",
        })

    # 5. illegal-no-refund-b2c-clause in Oferta
    if oferta_html:
        combined_oferta = oferta_html.lower()
        has_no_refund = bool(re.search(r"(?i)(?:оплаченные\s+суммы\s+возврату\s+не\s+подлежат|не\s+влечет\s+(?:перерасчета|возврата)|возврат\s+денежных\s+средств\s+не\s+производится|услуга\s+считается\s+оказанной\s+в\s+момент\s+оплаты)", combined_oferta))
        has_physical_address = bool(re.search(r"(?i)(?:адрес\s*(?:местонахождения|юр(?:идический)?|для\s+корреспонденции)?\s*[:\s]+(?:\d{6}|\bг\.\s*[а-я]+|\bул\.\s*[а-я]+))", combined_oferta))

        if has_no_refund or not has_physical_address:
            findings.append({
                "signature": "illegal_no_refund_b2c_clause",
                "tier": "RED_REAL_FINE",
                "legal_basis": "ст. 16, ст. 32 Закона РФ № 2300-1 (ЗоЗПП), ч. 2 ст. 14.8 КоАП РФ, ч. 2 ст. 10 149-ФЗ",
                "description": "Ущемление прав потребителей в B2C-оферте: условие о невозвратности оплаты / отказ от перерасчета либо сокрытие адреса",
            })

    # 6. unprotected-affiliate-program
    if re.search(r"(?i)(?:/affiliate|/partners|/referral|реферальн[а-я]+\s+программ|партнерск[а-я]+\s+программ)", site_html):
        mentions_reward = bool(re.search(r"(?i)(?:комисси[яеи]\s+за\s+переход|реферальн[а-я]+\s+вознаграждени|выплаты\s+за\s+каждого|партнерск[а-я]+\s+процент)", site_html))
        mentions_erid = bool(re.search(r"(?i)(?:erid|маркировк|орд|токен)", site_html))
        if mentions_reward and not mentions_erid:
            findings.append({
                "signature": "unprotected_affiliate_program",
                "tier": "RED_REAL_FINE",
                "legal_basis": "ч. 16 ст. 18.1 Федерального закона № 38-ФЗ 'О рекламе', ч. 16 ст. 14.3 КоАП РФ",
                "description": "Партнерская/реферальная программа с вознаграждением за переходы без требования маркировки erid",
            })

    return findings


# Backwards compatibility
detect_policy_domain_and_entity_paradox = policy_and_terms_forensics


# -----------------------------------------------------------------------------
# 4. REQUISITES & PHOENIX CLONE DETECTORS
# -----------------------------------------------------------------------------

def detect_missing_seller_requisites(
    main_html: str,
    contacts_html: str,
    found_inns: List[Tuple[str, int]],
    found_ogrns: List[Tuple[str, int]],
) -> Optional[Dict[str, Any]]:
    """
    ст. 9 ЗоЗПП, п. 19 ПП РФ № 2463 -> ч. 1 ст. 14.8 КоАП РФ.
    Checks if a commercial storefront lacks INN and OGRN on main page and contacts page.
    """
    combined = main_html + " " + contacts_html
    is_commercial = bool(
        re.search(r"(?i)(?:8\s*800\s*\d{3}|\+7\s*\(?\d{3}\)?|\bзаказать\b|\bкупить\b|\bполучить\b|\bоформить\b|\bцена\b|\bруб\b)", combined)
    )
    if not is_commercial:
        return None

    if not found_inns and not found_ogrns:
        return {
            "signature": "missing_seller_requisites_zozpp",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ст. 9 Закона РФ № 2300-1 (ЗоЗПП), п. 19 ПП РФ № 2463, ч. 1 ст. 14.8 КоАП РФ",
            "description": (
                "Сокрытие обязательных реквизитов продавца: на коммерческом сайте отсутствуют "
                "сведения об ИНН, ОГРН и юридическом лице (ст. 9 Закона о защите прав потребителей)."
            ),
        }
    return None


async def resolve_phoenix_clones_in_fns(
    org_names: List[str],
    city_hint: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """
    Searches bo.nalog.gov.ru for organization names to detect 'Phoenix Clones':
    Same brand/legal name where predecessor is INACTIVE (liquidated) and successor is ACTIVE.
    """
    close_c = False
    if client is None:
        client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://bo.nalog.gov.ru/",
            },
            timeout=8.0,
        )
        close_c = True

    phoenix_findings: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()

    for raw_name in sorted(list(set(org_names))):
        clean = re.sub(r'[\'\"«»]|\b(?:ООО|ОАО|ЗАО|ИП|Общество с ограниченной ответственностью)\b', '', raw_name, flags=re.IGNORECASE).strip()
        if len(clean) < 3 or clean in seen_names:
            continue
        seen_names.add(clean)

        try:
            enc_query = urllib.parse.quote(clean)
            url = f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={enc_query}"
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("content", [])
                inactive_entities: List[Dict[str, Any]] = []
                active_entities: List[Dict[str, Any]] = []
                clean_target = clean.lower().replace(" ", "")

                for org in items:
                    raw_sname = org.get("shortName") or org.get("fullName") or ""
                    clean_sname = re.sub(r'[\'\"«»]|\b(?:ООО|ОАО|ЗАО|ИП|Общество с ограниченной ответственностью)\b', '', raw_sname, flags=re.IGNORECASE).strip().lower().replace(" ", "")
                    if clean_target not in clean_sname and clean_sname not in clean_target:
                        continue
                    inn_org = org.get("inn", "")
                    clean_inn = re.sub(r"\D", "", inn_org)
                    st = org.get("statusCode")
                    bfo = org.get("bfo") or {}
                    gain = bfo.get("gainSum") or 0

                    ent_info = {
                        "inn": clean_inn,
                        "ogrn": org.get("ogrn"),
                        "name": org.get("shortName") or org.get("fullName"),
                        "status": st,
                        "status_date": org.get("statusDate"),
                        "city": org.get("city"),
                        "address": f"{org.get('city') or ''}, {org.get('street') or ''}",
                        "revenue_rub": gain * 1000 if gain else 0,
                    }
                    if st == "INACTIVE":
                        inactive_entities.append(ent_info)
                    elif st == "ACTIVE":
                        active_entities.append(ent_info)

                if inactive_entities and active_entities:
                    inact = inactive_entities[0]
                    act = active_entities[0]
                    phoenix_findings.append({
                        "signature": "phoenix_clone_detected",
                        "tier": "RED_REAL_FINE",
                        "legal_basis": "ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ",
                        "description": (
                            f"Обнаружен паттерн компании-феникса для бренда '{clean}': "
                            f"предшественник ООО (ИНН {inact['inn']}, ОГРН {inact['ogrn']}) ЛИКВИДИРОВАН ({inact.get('status_date')}), "
                            f"а деятельность ведется от лица компании-клона (ИНН {act['inn']}, ОГРН {act['ogrn']}, "
                            f"выручка {act.get('revenue_rub', 0):,} руб.). Использование регалий и патентов ликвидированного "
                            f"лица образует состав недостоверной рекламы и обмана потребителей."
                        ),
                        "predecessor": inact,
                        "successor": act,
                    })
        except Exception as e:
            logger.debug(f"bo.nalog search error for {clean}: {e}")

    if close_c:
        await client.aclose()

    return phoenix_findings


async def detect_regalia_and_pdf_chronology_mismatch(
    active_ogrn: Optional[str],
    html_text: str,
    pdf_urls: List[str],
    liquidated_inn: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """
    ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ.
    1. Extracts creation year from active OGRN (2000 + int(ogrn[1:3])).
    2. Identifies older regalia/patents/reviews prior to OGRN creation year.
    3. Reads PDF text (using pypdf, max 3 files <= 3MB) to verify if documents contain
       liquidated company's INN or third-party rights.
    """
    findings: List[Dict[str, Any]] = []
    if not active_ogrn or len(active_ogrn) < 3:
        return findings

    try:
        ogrn_year = 2000 + int(active_ogrn[1:3])
    except Exception:
        ogrn_year = 2026

    # 1. Check copyright and historical awards
    copy_match = re.search(r"©\s*(200\d|201\d|202[0-3])\b", html_text)
    has_old_copy = False
    if copy_match:
        c_year = int(copy_match.group(1))
        if c_year < ogrn_year:
            has_old_copy = True

    # Check reviews and patents
    has_patents = bool(re.search(r"(?i)\bпатент\s*(?:RU\s*)?\d{5,7}\b", html_text))
    has_old_reviews = bool(re.search(r"(?i)\b(?:201[0-9]|202[0-3])\s*год[а-я]*", html_text))

    # 2. PDF inspection via pypdf
    pdf_contained_liquidated_inn = False
    if pypdf and pdf_urls:
        close_c = False
        if client is None:
            client = httpx.AsyncClient(timeout=6.0, follow_redirects=True)
            close_c = True

        for p_url in sorted(list(set(pdf_urls)))[:3]:
            try:
                r_pdf = await client.get(p_url)
                if r_pdf.status_code == 200 and len(r_pdf.content) <= 3 * 1024 * 1024:
                    reader = pypdf.PdfReader(io.BytesIO(r_pdf.content))
                    pdf_text = ""
                    for page in reader.pages[:10]:
                        pdf_text += page.extract_text() or ""
                    if liquidated_inn and liquidated_inn in pdf_text:
                        pdf_contained_liquidated_inn = True
                        break
            except Exception as e:
                logger.debug(f"Failed to read PDF {p_url}: {e}")

        if close_c:
            await client.aclose()

    if (has_old_copy or has_old_reviews or has_patents or pdf_contained_liquidated_inn) and ogrn_year >= 2024:
        findings.append({
            "signature": "regalia_chronology_mismatch",
            "tier": "RED_REAL_FINE",
            "legal_basis": "ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ",
            "description": (
                f"Хронологический разрыв регалий: активное юридическое лицо зарегистрировано в {ogrn_year} г. "
                f"(ОГРН {active_ogrn}), однако на сайте демонстрируются патенты, награды и отзывы предшественника "
                f"до даты создания компании. Торговля под чужими/историческими документами без подтверждения передачи "
                f"прав образует состав ст. 14.7 КоАП РФ и ст. 5 закона 'О рекламе' (до 500 000 руб.)."
            ),
            "ogrn_year": ogrn_year,
            "has_old_copyright": has_old_copy,
            "pdf_contained_liquidated_inn": pdf_contained_liquidated_inn,
        })

    return findings


def detect_instant_protocol_triggers(html_texts: List[str]) -> List[Dict[str, Any]]:
    """
    1. Evaluates Meta extremist social links/icons vs blocked LinkedIn/X rule.
    2. Promises 'Рассрочка 0% / В кредит от ...' without mentioning bank name and PSK -> ст. 28 38-ФЗ.
    """
    findings: List[Dict[str, Any]] = []
    combined = " ".join(html_texts)

    # 1. Social networks rule (Meta extremist vs LinkedIn/X)
    meta_findings = meta_extremist_vs_blocked_rule(combined)
    findings.extend(meta_findings)

    # 2. Credit / Installments without PSK / Bank
    credit_match = re.search(r"(?i)(?:рассрочк[а-я]*\s*0\s*%|в\s+кредит\s+от\s*\d+|кредит\s+без\s+переплат)", combined)
    if credit_match:
        window = combined[max(0, credit_match.start() - 200): min(len(combined), credit_match.end() + 200)]
        has_bank = any(b in window.lower() for b in ["банк", "пск", "лицензи", "кредитн", "пао", "ао "])
        if not has_bank:
            findings.append({
                "signature": "unmarked_credit_installment_38fz",
                "tier": "RED_REAL_FINE",
                "legal_basis": "ст. 28 Федерального закона № 38-ФЗ 'О рекламе', ч. 1 ст. 14.3 КоАП РФ",
                "description": (
                    "Реклама финансовых услуг (рассрочка 0% / кредит) без указания наименования банка, "
                    "предоставляющего услугу, и полной стоимости кредита (ПСК). Нарушение ст. 28 38-ФЗ влечет штраф от 100 000 до 500 000 руб."
                ),
                "quote": credit_match.group(0),
            })

    return findings

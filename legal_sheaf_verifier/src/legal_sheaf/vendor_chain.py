"""
AdRadar Vendor Chain Resolver (Hop 0 -> Hop 1 Graph Investigation).
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and actual Russian regulatory practice.

Audits third-party supply chain widgets/scripts/iframes on client websites:
1. Fast dictionary KNOWN_INFRA_VENDORS for standard domestic and foreign operators.
2. Max-Hop=1 automatic jump to unknown vendor websites (inspect_unknown_vendor_hop1):
   - Extracts root 2nd-level domain (e.g., athenachat.ai from widget-js.athenachat.ai).
   - Fetches vendor root, /ru/, /policy, /terms.
   - Extracts valid FNS checksum INN (10/12 digits) and legal name (ООО/АО/ИП).
   - Validates vendor in RKN Operator Registry (pd.rkn.gov.ru).
   - Resolves vendor infrastructure IPs and physical country via RIPE NCC MaxMind GeoLite.
3. Legal Synthesis:
   - Verifies whether client's Privacy Policy explicitly discloses the vendor (ч. 3 ст. 6 152-ФЗ).
   - If vendor is unregistered in RKN or runs foreign servers -> escalates to RED_REAL_FINE
     (toxic-supply-chain-widget, ч. 3, 5 ст. 6, ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ).
   - Persists dossiers (14-day SQLite cache, WAL) and saves discovered vendor leads into SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

from legal_sheaf.config import DB_DIR
from legal_sheaf.forensics_v3 import extract_root_domain, query_ripe_geo_ip, resolve_host_ip_with_fallback
from legal_sheaf.inn_erid_validators import find_valid_inns_in_text, validate_inn_checksum
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async

logger = logging.getLogger("legal_sheaf.vendor_chain")

VENDOR_CACHE_TTL_SECONDS = 14 * 86400  # 14 days


class TriState(str):
    """Three-valued logic under Kleene semantics: TRUE | FALSE | UNVERIFIED."""
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNVERIFIED = "UNVERIFIED"


# Standard Russian & Global Infrastructure Dictionary
KNOWN_INFRA_VENDORS: Dict[str, Dict[str, Any]] = {
    # Russian Infrastructure & Trackers
    "mc.yandex.ru": {"brand": "Яндекс.Метрика", "entity": "ООО «ЯНДЕКС»", "inn": "7736207543", "is_foreign": False, "skip_hop": True},
    "yastatic.net": {"brand": "Яндекс", "entity": "ООО «ЯНДЕКС»", "inn": "7736207543", "is_foreign": False, "skip_hop": True},
    "api-maps.yandex.ru": {"brand": "Яндекс.Карты", "entity": "ООО «ЯНДЕКС»", "inn": "7736207543", "is_foreign": False, "skip_hop": True},
    "smartcaptcha.cloud.yandex.ru": {"brand": "Яндекс SmartCaptcha", "entity": "ООО «Яндекс.Облако»", "inn": "7704458265", "is_foreign": False, "skip_hop": True},
    "vk.com": {"brand": "ВКонтакте", "entity": "ООО «ВК»", "inn": "7743001840", "is_foreign": False, "skip_hop": True},
    "vk.ru": {"brand": "ВКонтакте", "entity": "ООО «ВК»", "inn": "7743001840", "is_foreign": False, "skip_hop": True},
    "userapi.com": {"brand": "ВКонтакте", "entity": "ООО «ВК»", "inn": "7743001840", "is_foreign": False, "skip_hop": True},
    "top-fwz1.mail.ru": {"brand": "Top.Mail.ru", "entity": "ООО «ВК»", "inn": "7743001840", "is_foreign": False, "skip_hop": True},
    "jivo.ru": {"brand": "JivoSite", "entity": "АО «Живой Сайт»", "inn": "7722744877", "is_foreign": False, "skip_hop": True},
    "jivosite.com": {"brand": "JivoSite", "entity": "АО «Живой Сайт»", "inn": "7722744877", "is_foreign": False, "skip_hop": True},
    "calltouch.ru": {"brand": "Calltouch", "entity": "ООО «Телеком Дистрибуция»", "inn": "7703386927", "is_foreign": False, "skip_hop": True},
    "roistat.com": {"brand": "Roistat", "entity": "ООО «Бизнес-аналитика»", "inn": "7724324488", "is_foreign": False, "skip_hop": True},
    "bitrix24.ru": {"brand": "Битрикс24", "entity": "ООО «1С-Битрикс»", "inn": "7717586110", "is_foreign": False, "skip_hop": True},
    "cdn-ru.bitrix24.ru": {"brand": "Битрикс24", "entity": "ООО «1С-Битрикс»", "inn": "7717586110", "is_foreign": False, "skip_hop": True},
    "1c-bitrix.ru": {"brand": "1С-Битрикс", "entity": "ООО «1С-Битрикс»", "inn": "7717586110", "is_foreign": False, "skip_hop": True},
    "marquiz.ru": {"brand": "Marquiz", "entity": "ООО «Маркиз»", "inn": "7726438060", "is_foreign": False, "skip_hop": True},
    "carrotquest.io": {"brand": "Carrot Quest", "entity": "ООО «Телепорт»", "inn": "5902043694", "is_foreign": False, "skip_hop": True},
    "uiscom.ru": {"brand": "CoMagic / UIS", "entity": "ООО «Новосистем»", "inn": "7728612666", "is_foreign": False, "skip_hop": True},
    "comagic.ru": {"brand": "CoMagic / UIS", "entity": "ООО «Новосистем»", "inn": "7728612666", "is_foreign": False, "skip_hop": True},
    "livetex.ru": {"brand": "LiveTex", "entity": "ООО «ЛайвТекс»", "inn": "7841444458", "is_foreign": False, "skip_hop": True},
    "envybox.io": {"brand": "Envybox", "entity": "ООО «Энвибокс»", "inn": "5904344440", "is_foreign": False, "skip_hop": True},

    # Known Foreign Services (No hop needed, foreign status established)
    "googletagmanager.com": {"brand": "Google Tag Manager", "entity": "Google LLC", "inn": None, "is_foreign": True, "skip_hop": True},
    "google-analytics.com": {"brand": "Google Analytics", "entity": "Google LLC", "inn": None, "is_foreign": True, "skip_hop": True},
    "google.com": {"brand": "Google", "entity": "Google LLC", "inn": None, "is_foreign": True, "skip_hop": True},
    "gstatic.com": {"brand": "Google Static", "entity": "Google LLC", "inn": None, "is_foreign": True, "skip_hop": True},
    "intercom.io": {"brand": "Intercom", "entity": "Intercom Inc.", "inn": None, "is_foreign": True, "skip_hop": True},
    "intercomcdn.com": {"brand": "Intercom CDN", "entity": "Intercom Inc.", "inn": None, "is_foreign": True, "skip_hop": True},
    "tawk.to": {"brand": "Tawk.to", "entity": "Tawk.to Inc.", "inn": None, "is_foreign": True, "skip_hop": True},
    "crisp.chat": {"brand": "Crisp", "entity": "Crisp IM SARL", "inn": None, "is_foreign": True, "skip_hop": True},
    "clarity.ms": {"brand": "Microsoft Clarity", "entity": "Microsoft Corp.", "inn": None, "is_foreign": True, "skip_hop": True},
}

STATIC_CDNS: Set[str] = {
    "jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com",
    "fonts.googleapis.com", "fonts.gstatic.com", "ajax.googleapis.com",
    "code.jquery.com", "cdn.tailwindcss.com", "bootstrapcdn.com"
}


def _get_db_path() -> Path:
    """Returns path to vendor dossiers SQLite database and ensures parent directory exists."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return DB_DIR / "vendor_dossiers.sqlite"


def init_vendor_db() -> sqlite3.Connection:
    """Initializes SQLite connection in WAL mode with requisite tables."""
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vendor_dossiers (
            vendor_root TEXT PRIMARY KEY,
            brand_name TEXT,
            inn TEXT,
            entity_name TEXT,
            is_registered_in_rkn INTEGER,
            rkn_reg_number TEXT,
            has_cross_border_declared INTEGER,
            servers_geo_json TEXT,
            has_foreign_servers INTEGER,
            terms_url TEXT,
            policy_url TEXT,
            audit_dossier_json TEXT,
            updated_at REAL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_vendor_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inn TEXT,
            entity_name TEXT,
            vendor_root TEXT,
            found_on_client_domain TEXT,
            risk_reasons TEXT,
            discovered_at REAL
        );
    """)
    conn.commit()
    return conn


def get_cached_vendor_dossier(vendor_root: str) -> Optional[Dict[str, Any]]:
    """Retrieves cached dossier if fresh (< 14 days)."""
    try:
        conn = init_vendor_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT audit_dossier_json, updated_at FROM vendor_dossiers WHERE vendor_root = ?",
            (vendor_root.lower(),)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            raw_json, updated_at = row
            if time.time() - updated_at < VENDOR_CACHE_TTL_SECONDS:
                return json.loads(raw_json)
    except Exception as e:
        logger.debug(f"Vendor cache read error for {vendor_root}: {e}")
    return None


def save_vendor_dossier(dossier: Dict[str, Any]) -> None:
    """
    Saves vendor dossier into SQLite cache ONLY if verified (200 OK / non-error).
    Kleene invariant: Never cache UNVERIFIED / timeout / 5xx error states!
    """
    if (
        dossier.get("status") == TriState.UNVERIFIED
        or dossier.get("is_registered_in_rkn") == TriState.UNVERIFIED
        or not dossier.get("is_verified_200", False)
    ):
        logger.debug("Skipping SQLite cache save for unverified vendor dossier")
        return

    try:
        conn = init_vendor_db()
        v_root = dossier.get("vendor_root", "").lower()
        is_rkn_val = 1 if dossier.get("is_registered_in_rkn") is True else 0
        conn.execute("""
            INSERT OR REPLACE INTO vendor_dossiers (
                vendor_root, brand_name, inn, entity_name, is_registered_in_rkn,
                rkn_reg_number, has_cross_border_declared, servers_geo_json,
                has_foreign_servers, terms_url, policy_url, audit_dossier_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            v_root,
            dossier.get("brand_name"),
            dossier.get("inn"),
            dossier.get("entity_name"),
            is_rkn_val,
            dossier.get("rkn_reg_number"),
            1 if dossier.get("has_cross_border_declared") else 0,
            json.dumps(dossier.get("servers_geo", []), ensure_ascii=False),
            1 if dossier.get("has_foreign_servers") else 0,
            dossier.get("terms_url"),
            dossier.get("policy_url"),
            json.dumps(dossier, ensure_ascii=False),
            time.time(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Vendor cache save error: {e}")


def save_discovered_vendor_lead(
    inn: Optional[str],
    entity_name: Optional[str],
    vendor_root: str,
    found_on_client_domain: str,
    risk_reasons: str,
) -> None:
    """Saves discovered domestic vendor with compliance risks as a B2B sales lead."""
    try:
        conn = init_vendor_db()
        conn.execute("""
            INSERT INTO discovered_vendor_leads (
                inn, entity_name, vendor_root, found_on_client_domain, risk_reasons, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            inn,
            entity_name,
            vendor_root,
            found_on_client_domain,
            risk_reasons,
            time.time(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Discovered vendor lead save error: {e}")


def match_known_vendor(host: str) -> Optional[Dict[str, Any]]:
    """Checks if host or any parent domain belongs to KNOWN_INFRA_VENDORS."""
    clean = host.lower().strip().split(":")[0]
    if clean.startswith("www."):
        clean = clean[4:]

    # Direct match
    if clean in KNOWN_INFRA_VENDORS:
        res = dict(KNOWN_INFRA_VENDORS[clean])
        res["matched_host"] = clean
        return res

    # Suffix match
    for k_host, v_data in KNOWN_INFRA_VENDORS.items():
        if clean.endswith("." + k_host):
            res = dict(v_data)
            res["matched_host"] = k_host
            return res

    # Check static CDNs
    for cdn in STATIC_CDNS:
        if clean == cdn or clean.endswith("." + cdn):
            return {"brand": "Static CDN", "is_cdn": True, "skip_hop": True}

    return None


async def inspect_unknown_vendor_hop1(
    vendor_host: str,
    client_domain: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Automatic Jump to Source of Unknown Widget (Hop 0 -> Hop 1):
    1. Extracts root 2nd-level domain (vendor_root).
    2. Fetches vendor home page (https://{vendor_root}/ru/ and https://{vendor_root}/).
    3. Finds links to /policy, /privacy, /terms, /oferta, and auth panel (panel.*, app.*, lk.*).
    4. Extracts valid FNS checksum INN (10/12 digits) and legal name (ООО / АО / ИП).
    5. Queries pd.rkn.gov.ru for official registration status & cross-border notification.
    6. Resolves IP addresses of widget host, vendor root, and panel, verifying GeoIP via RIPE NCC.
    """
    v_root = extract_root_domain(vendor_host)
    if not v_root:
        return {"vendor_root": vendor_host, "status": "INVALID_ROOT"}

    # Check 14-day SQLite cache
    cached = get_cached_vendor_dossier(v_root)
    if cached:
        return cached

    close_c = False
    if client is None:
        client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
            },
            timeout=8.0,
            follow_redirects=True,
        )
        close_c = True

    dossier: Dict[str, Any] = {
        "vendor_root": v_root,
        "vendor_host": vendor_host,
        "brand_name": v_root.split(".")[0].capitalize(),
        "inn": None,
        "entity_name": None,
        "is_registered_in_rkn": TriState.UNVERIFIED,
        "rkn_reg_number": None,
        "has_cross_border_declared": False,
        "servers_geo": [],
        "has_foreign_servers": False,
        "terms_url": None,
        "policy_url": None,
        "is_russian_entity": False,
        "is_verified_200": False,
        "status": TriState.UNVERIFIED,
        "hop_level": 1,
    }

    try:
        # Step B: Fetch vendor root
        home_urls = [f"https://{v_root}/ru/", f"https://{v_root}/"]
        vendor_html = ""
        resolved_home_url = home_urls[0]

        for h_url in home_urls:
            try:
                resp = await client.get(h_url, timeout=5.0)
                if resp.status_code == 200:
                    vendor_html = resp.text
                    resolved_home_url = str(resp.url)
                    dossier["is_verified_200"] = True
                    dossier["status"] = "VERIFIED"
                    break
                elif resp.status_code in (429, 500, 502, 503, 504):
                    dossier["status"] = TriState.UNVERIFIED
            except Exception:
                dossier["status"] = TriState.UNVERIFIED

        # Parse links for /terms, /policy, auth buttons
        candidate_docs: List[str] = []
        auth_hosts: List[str] = []

        if vendor_html:
            soup = BeautifulSoup(vendor_html[:300000], "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                t_text = a_tag.get_text().strip().lower()
                full_href = urllib.parse.urljoin(resolved_home_url, href)

                # Terms / Oferta / Policy links
                if any(k in href.lower() or k in t_text for k in ["terms", "условия", "оферт", "oferta", "пользовательск"]):
                    if full_href not in candidate_docs:
                        candidate_docs.append(full_href)
                        if not dossier["terms_url"]:
                            dossier["terms_url"] = full_href

                if any(k in href.lower() or k in t_text for k in ["privacy", "policy", "политик", "конфиденциальн"]):
                    if full_href not in candidate_docs:
                        candidate_docs.append(full_href)
                        if not dossier["policy_url"]:
                            dossier["policy_url"] = full_href

                # Auth panel hosts (panel.*, app.*, lk.*)
                try:
                    p_loc = urllib.parse.urlparse(full_href).netloc.lower().split(":")[0]
                    if p_loc.startswith("www."):
                        p_loc = p_loc[4:]
                    if p_loc.endswith("." + v_root) and any(sub in p_loc for sub in ["panel.", "app.", "lk.", "login.", "auth."]):
                        if p_loc not in auth_hosts:
                            auth_hosts.append(p_loc)
                except Exception:
                    pass

        # Default fallback doc URLs
        if not candidate_docs:
            candidate_docs = [
                f"https://{v_root}/ru/terms/",
                f"https://{v_root}/terms/",
                f"https://{v_root}/ru/privacy-policy/",
                f"https://{v_root}/privacy/",
            ]

        # Step C: Fetch doc contents to extract valid INN & legal entity
        candidate_docs = sorted(list(dict.fromkeys(candidate_docs)))
        auth_hosts = sorted(list(set(auth_hosts)))
        doc_texts: List[str] = [vendor_html]
        for d_url in candidate_docs[:4]:
            try:
                r_doc = await client.get(d_url, timeout=5.0)
                if r_doc.status_code == 200:
                    doc_texts.append(r_doc.text)
                    if "terms" in d_url and not dossier["terms_url"]:
                        dossier["terms_url"] = d_url
                    if "privacy" in d_url and not dossier["policy_url"]:
                        dossier["policy_url"] = d_url
            except Exception:
                pass

        combined_docs = " ".join(doc_texts)

        # Extract INN with FNS checksum validation
        found_inns = find_valid_inns_in_text(combined_docs)
        if found_inns:
            # Pick first valid INN
            valid_inn = found_inns[0][0]
            dossier["inn"] = valid_inn
            dossier["is_russian_entity"] = True

            # Extract Entity Name around INN
            ent_match = re.search(
                r'(?i)(?:ИП|ООО|ОАО|ЗАО|АО|Индивидуальный предприниматель|Общество с ограниченной ответственностью)\s+[А-ЯЁA-Z0-9«\"].{3,50}?(?=(?:ИНН|ОГРН|\bг\.\s|(?:\.|\n)))',
                combined_docs
            )
            if ent_match:
                clean_ent = re.sub(r'[\r\n\t]+', ' ', ent_match.group(0)).strip()
                dossier["entity_name"] = clean_ent
            else:
                dossier["entity_name"] = f"Организация с ИНН {valid_inn}"

        # Step D1: Check RKN Registry if Russian INN was found
        if dossier["inn"]:
            try:
                rkn_res = await check_rkn_registry_async(client, dossier["inn"])
                if rkn_res.get("status") == "RKN_CHECK_UNAVAILABLE" or rkn_res.get("is_registered_in_rkn") is None:
                    dossier["is_registered_in_rkn"] = TriState.UNVERIFIED
                    dossier["status"] = TriState.UNVERIFIED
                else:
                    dossier["is_registered_in_rkn"] = bool(rkn_res.get("is_registered_in_rkn"))
                    dossier["rkn_reg_number"] = rkn_res.get("reg_number")
                    dossier["has_cross_border_declared"] = bool(rkn_res.get("has_cross_border_declared"))
                    if rkn_res.get("operator_name") and not dossier["entity_name"]:
                        dossier["entity_name"] = rkn_res.get("operator_name")
            except Exception as e_rkn:
                logger.debug(f"Vendor RKN check failed for INN {dossier['inn']}: {e_rkn}")
                dossier["is_registered_in_rkn"] = TriState.UNVERIFIED
                dossier["status"] = TriState.UNVERIFIED

        # Step D2: Check Server GeoIP for widget host, root, and panel
        hosts_to_geo = sorted(list(set([vendor_host, v_root] + sorted(auth_hosts)[:3])))
        for h in hosts_to_geo:
            ips = await resolve_host_ip_with_fallback(h, client=client)
            for ip in sorted(ips):
                # Exclude benchmark/sandbox IPs
                if ip.startswith("198.18."):
                    continue
                geo = await query_ripe_geo_ip(ip, client=client)
                geo_entry = {
                    "host": h,
                    "ip": ip,
                    "country": geo.get("country"),
                    "city": geo.get("city"),
                    "is_foreign": geo.get("is_foreign"),
                }
                dossier["servers_geo"].append(geo_entry)
                if geo.get("is_foreign") is True:
                    dossier["has_foreign_servers"] = True

        # Save to SQLite cache ONLY if verified
        save_vendor_dossier(dossier)

        # If domestic Russian vendor has verified compliance risks, log as lead
        if dossier["is_russian_entity"] and (dossier["is_registered_in_rkn"] is False or dossier["has_foreign_servers"] is True):
            reasons = []
            if dossier["is_registered_in_rkn"] is False:
                reasons.append("Не зарегистрирован в реестре операторов РКН (ч. 10 ст. 13.11 КоАП РФ)")
            if dossier["has_foreign_servers"] is True:
                reasons.append("Серверы приема данных размещены за пределами РФ (ч. 8 ст. 13.11 КоАП РФ)")
            save_discovered_vendor_lead(
                inn=dossier["inn"],
                entity_name=dossier["entity_name"],
                vendor_root=v_root,
                found_on_client_domain=client_domain,
                risk_reasons="; ".join(reasons),
            )

    except Exception as e:
        logger.warning(f"Error in inspect_unknown_vendor_hop1 for {vendor_host}: {e}")
    finally:
        if close_c:
            await client.aclose()

    return dossier


async def audit_vendor_supply_chain(
    client_domain: str,
    all_html_pages: List[str],
    policy_html: str = "",
    client: Optional[httpx.AsyncClient] = None,
    max_unknown_vendors: int = 5,
    visited_roots: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extracts all external scripts/iframes across client pages and subdomains.
    Performs Hop 0 -> Hop 1 resolution and checks compliance with ч. 3 & ч. 5 ст. 6,
    ч. 5 ст. 18 152-ФЗ and ч. 8 ст. 13.11 КоАП РФ.
    Enforces DAG cycle immunity (visited_roots) and canonical deterministic sorting.

    Returns:
        (findings, vendor_dossiers)
    """
    client_root = extract_root_domain(client_domain)
    if visited_roots is None:
        visited_roots = set()
    visited_roots.add(client_root)

    discovered_external_hosts: Set[str] = set()

    # Extract all external script src, iframe src, and endpoint domains
    for page_html in all_html_pages:
        # Script src
        for s_match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', page_html, re.I):
            src = s_match.group(1).strip()
            if src.startswith("http"):
                try:
                    h = urllib.parse.urlparse(src).netloc.lower().split(":")[0]
                    if h.startswith("www."):
                        h = h[4:]
                    if h and not h.endswith("." + client_root) and h != client_root:
                        discovered_external_hosts.add(h)
                except Exception:
                    pass

        # Iframe src
        for if_match in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', page_html, re.I):
            src = if_match.group(1).strip()
            if src.startswith("http"):
                try:
                    h = urllib.parse.urlparse(src).netloc.lower().split(":")[0]
                    if h.startswith("www."):
                        h = h[4:]
                    if h and not h.endswith("." + client_root) and h != client_root:
                        discovered_external_hosts.add(h)
                except Exception:
                    pass

    findings: List[Dict[str, Any]] = []
    vendor_dossiers: List[Dict[str, Any]] = []
    processed_roots: Set[str] = set()
    unknown_checked_count = 0

    clean_policy_text = policy_html.lower()

    # Canonical deterministic sort key: priority_rank (0=known, 1=unknown), root, host
    def canonical_vendor_sort_key(h: str) -> Tuple[int, str, str]:
        r = extract_root_domain(h)
        is_known = 0 if match_known_vendor(h) else 1
        return (is_known, r, h)

    sorted_hosts = sorted(list(discovered_external_hosts), key=canonical_vendor_sort_key)

    for host in sorted_hosts:
        v_root = extract_root_domain(host)
        if not v_root or v_root in visited_roots or v_root in processed_roots:
            continue
        visited_roots.add(v_root)
        processed_roots.add(v_root)

        # Check known dictionary first
        known = match_known_vendor(host)
        if known:
            if known.get("is_cdn"):
                continue

            brand = known.get("brand", v_root)
            inn = known.get("inn")
            entity = known.get("entity", brand)

            # Check disclosure in client's Privacy Policy
            is_disclosed = False
            if clean_policy_text:
                if brand.lower() in clean_policy_text or (inn and inn in clean_policy_text) or v_root in clean_policy_text:
                    is_disclosed = True

            if not is_disclosed:
                findings.append({
                    "signature": "undisclosed_third_party_processor",
                    "tier": "ORANGE_REGULATORY_DEMAND",
                    "legal_basis": "ч. 3 ст. 6, ст. 18.1 152-ФЗ",
                    "description": (
                        f"На сайте подключен сторонний сервис {brand} ({entity}), однако в тексте Политики "
                        f"конфиденциальности отсутствует прямое указание на поручение обработки ПДн данному лицу "
                        f"(нарушение ч. 3 ст. 6 152-ФЗ)."
                    ),
                    "vendor": brand,
                    "host": host,
                })

            vendor_dossiers.append({
                "vendor_root": v_root,
                "brand_name": brand,
                "inn": inn,
                "entity_name": entity,
                "is_known_infra": True,
                "is_disclosed_in_policy": is_disclosed,
            })
            continue

        # Unknown vendor -> Hop 1 Investigation
        if unknown_checked_count >= max_unknown_vendors:
            continue

        unknown_checked_count += 1

        dossier = await inspect_unknown_vendor_hop1(host, client_domain=client_domain, client=client)
        vendor_dossiers.append(dossier)

        brand_name = dossier.get("brand_name") or v_root
        entity_name = dossier.get("entity_name") or brand_name
        inn = dossier.get("inn")
        is_rkn = dossier.get("is_registered_in_rkn")
        has_foreign_srv = dossier.get("has_foreign_servers", False)

        # 1. Check disclosure in Client's Policy
        is_disclosed = False
        if clean_policy_text:
            if v_root in clean_policy_text or brand_name.lower() in clean_policy_text or (inn and inn in clean_policy_text):
                is_disclosed = True

        if not is_disclosed:
            findings.append({
                "signature": "undisclosed_third_party_processor",
                "tier": "ORANGE_REGULATORY_DEMAND",
                "legal_basis": "ч. 3 ст. 6, ст. 18.1 152-ФЗ",
                "description": (
                    f"На сайте клиента обнаружен внешний скрипт/виджет {host} (Владелец: {entity_name}, ИНН {inn or 'не установлен'}), "
                    f"однако в Политике конфиденциальности отсутствует информация о поручении обработки ПДн данному лицу."
                ),
                "vendor": brand_name,
                "host": host,
            })

        # 2. Check toxic supply chain (Not in RKN or Foreign Servers)
        # Kleene invariant: UNVERIFIED is NOT treated as False!
        is_toxic_rkn = dossier.get("is_russian_entity") and is_rkn is False
        is_toxic_foreign = has_foreign_srv is True
        if is_toxic_foreign or is_toxic_rkn:
            srv_list = [f"{s.get('ip')} ({s.get('country')})" for s in dossier.get("servers_geo", []) if s.get("is_foreign")]
            srv_str = ", ".join(srv_list) if srv_list else "зарубежные серверы"

            reasons_desc = []
            if has_foreign_srv:
                reasons_desc.append(f"передача данных на иностранные серверные мощности ({srv_str})")
            if dossier.get("is_russian_entity") and not is_rkn:
                reasons_desc.append(f"отсутствие регистрации разработчика ({entity_name}, ИНН {inn}) в реестре операторов РКН")

            findings.append({
                "signature": "toxic_supply_chain_widget",
                "tier": "RED_REAL_FINE",
                "legal_basis": "ч. 3, ч. 5 ст. 6, ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
                "description": (
                    f"Токсичный сторонний виджет в цепочке поставок (Supply Chain Risk): виджет {host} "
                    f"(Владелец: {entity_name}, ИНН: {inn or 'Н/Д'}, домен {v_root}) осуществляет сбор данных "
                    f"пользователей сайта, при этом зафиксировано: {'; '.join(reasons_desc)}. "
                    f"По закону № 152-ФЗ ответственность за незаконные действия стороннего обработчика перед субъектами "
                    f"и Роскомнадзором несет владелец сайта-заказчика (ч. 5 ст. 6 152-ФЗ; штраф от 1 000 000 до 6 000 000 руб.)."
                ),
                "vendor_root": v_root,
                "vendor_host": host,
                "entity_name": entity_name,
                "inn": inn,
                "is_registered_in_rkn": is_rkn,
                "servers_geo": dossier.get("servers_geo", []),
            })

    return findings, vendor_dossiers

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
from datetime import datetime
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
    detect_untranslated_foreign_words_168fz,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    validate_inn_checksum,
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
from legal_sheaf.it_accreditation import ITAccreditationAuditor
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput
from legal_sheaf.vendor_chain import KNOWN_INFRA_VENDORS, STATIC_CDNS, audit_vendor_supply_chain

logger = logging.getLogger("legal_sheaf.dual_auditor")

COMMON_PLATFORMS: Set[str] = {
    "vc.ru", "t.me", "telegram.org", "telegram.me", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru", "github.com",
    "wikipedia.org", "google.com", "yandex.ru", "ya.ru", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "rutube.ru", "tenchat.ru", "pikabu.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com", "spacex.com"
}

SUPERLATIVE_REGEX = re.compile(
    r"(?i)\b(?:№\s*1|номер\s+(?:один|1)|лучш(?:ий|ая|ее|ие|ую|им|их)|лидер\s+рынка|абсолютный\s+лидер|первый\s+в\s+россии|самый\s+(?:надежный|популярный|выгодный|быстрый|удобный))\b"
)

AFFILIATE_REGEX = re.compile(
    r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|r|utm_medium=affiliate)=|/(?:r|promo|aff|partner)/|start=(?:ref|w\d+))"
)

PROMO_CODE_REGEX = re.compile(
    r"(?i)(?:промокод[:\s]+[A-Za-z0-9_-]+|скидк\w*\s+по\s+промокоду|бонус\s+по\s+ссылке)"
)

LABEL_REKLAMA_REGEX = re.compile(
    r"(?i)(?:^|\s)(?:реклама|партнерский\s+материал|на\s+правах\s+рекламы)"
)

ERID_REGEX = re.compile(
    r"(?i)\berid[:=\s]+([A-Za-z0-9_-]+)\b"
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


class DualAuditor:
    """
    Unified Dual-Tier Compliance Auditor for Articles and Corporate Websites.
    """

    def __init__(
        self,
        bundle: Optional[AdRadarBrainBundle] = None,
        koap_calculator: Optional[KoAPSanctionsCalculator] = None,
        scanner_152: Optional[Compliance152FZScanner] = None,
    ):
        if bundle is not None:
            self.bundle = bundle
        else:
            bundle_path = MODELS_CACHE_DIR / "champion_v2.bundle"
            if not bundle_path.exists() and (ARTIFACTS_DIR / "champion_v2.bundle").exists():
                bundle_path = ARTIFACTS_DIR / "champion_v2.bundle"
            if bundle_path.exists():
                self.bundle = AdRadarBrainBundle.load(bundle_path)
            else:
                self.bundle = AdRadarBrainBundle.create_from_artifacts()

        self.koap_calculator = koap_calculator or KoAPSanctionsCalculator()
        self.scanner_152 = scanner_152 or Compliance152FZScanner()

    # -------------------------------------------------------------------------
    # TIER 1: ARTICLE AUDIT (EXTERNAL_PUBLICATION)
    # -------------------------------------------------------------------------
    async def audit_article(
        self,
        url: str,
        text: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
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
            title = ""
            author = "Не указан"
            article_text = text or ""
            is_promo_platform_flag = False
            commercial_links: List[str] = []

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
                            title = entry.get("title") or ""
                            author = entry.get("author", {}).get("name") or entry.get("subsite", {}).get("name") or "Не указан"
                            is_promo_platform_flag = bool(entry.get("isPromo") or entry.get("is_promo") or entry.get("ad_data") or entry.get("ad") or entry.get("isFlash"))
                            blocks = entry.get("blocks", [])
                            text_parts = []
                            for b in blocks:
                                bdata = b.get("data", {}) or {}
                                t = bdata.get("text", "")
                                if t:
                                    soup = BeautifulSoup(t, "html.parser")
                                    text_parts.append(soup.get_text())
                                    for a in soup.find_all("a", href=True):
                                        commercial_links.append(resolve_redirect_url(a["href"].strip()))
                                l_obj = bdata.get("link", {}).get("url") if isinstance(bdata.get("link"), dict) else bdata.get("url")
                                if l_obj and isinstance(l_obj, str):
                                    commercial_links.append(resolve_redirect_url(l_obj.strip()))
                            article_text = " ".join(text_parts).strip()
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
                        # Extract article body text
                        article_tag = soup.find("article") or soup.find("main") or soup.body
                        if article_tag:
                            article_text = article_tag.get_text(separator=" ", strip=True)
                            for a in article_tag.find_all("a", href=True):
                                commercial_links.append(resolve_redirect_url(a["href"].strip()))
                        else:
                            article_text = soup.get_text(separator=" ", strip=True)

            if not title and article_text:
                title = article_text[:80] + "..."

            # Filter external commercial links (exclude social networks, search engines, vc.ru itself)
            filtered_comm_links = []
            for lk in commercial_links:
                try:
                    p = urllib.parse.urlparse(lk)
                    host = p.netloc.lower().split(":")[0]
                    if host.startswith("www."):
                        host = host[4:]
                    if host and host not in COMMON_PLATFORMS:
                        if lk not in filtered_comm_links:
                            filtered_comm_links.append(lk)
                except Exception:
                    pass

            # 2. Extract markers
            erid_m = ERID_REGEX.search(article_text)
            has_erid = bool(erid_m)
            erid_token = erid_m.group(1) if erid_m else None

            has_affiliate = any(is_affiliate_link(lnk) for lnk in filtered_comm_links) or bool(AFFILIATE_REGEX.search(article_text))
            has_promo_phrase = bool(PROMO_CODE_REGEX.search(article_text))
            has_label_reklama = bool(LABEL_REKLAMA_REGEX.search(article_text))
            found_inns = find_valid_inns_in_text(article_text)
            has_inn = len(found_inns) > 0

            # 3. Neural sheaf model prediction in EXTERNAL_PUBLICATION mode
            author_domain = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
            doc_in = DocumentInput(
                clean_text=article_text[:8000],
                page_url=url,
                author_domain=author_domain or "vc.ru",
                context_mode="EXTERNAL_PUBLICATION",
            )
            sheaf_report = self.bundle.verify_document(doc_in)

            # 4. Strict 38-FZ Scenario Classification
            if (has_label_reklama or is_promo_platform_flag) and (not has_erid or not has_inn):
                scenario = "INCOMPLETE_MARKING_LABEL_OR_INN"
                is_violation_38 = True
                verdict = "Суррогатная пометка: указано 'Реклама' / 'Партнерский материал', но отсутствует токен erid или ИНН рекламодателя (Приказ РКН № 68)"
                primary_sig = "incomplete_marking_label_or_inn"
            elif (has_affiliate or has_promo_phrase or (filtered_comm_links and sheaf_report.is_violation)) and not has_erid:
                scenario = "UNMARKED_AD_NO_ERID"
                is_violation_38 = True
                verdict = "Скрытая интернет-реклама / партнерская реф-ссылка без маркировки токеном erid (ч. 16 ст. 14.3 КоАП РФ)"
                primary_sig = "unmarked_ad_no_erid"
            elif has_erid and not filtered_comm_links and not sheaf_report.is_violation:
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

            # 5. Form evidence witness and lock findings
            findings_raw = []
            if is_violation_38:
                # Find quote snippet
                quote_text = ""
                quote_start = 0
                quote_end = 0
                if has_affiliate:
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

                witness = EvidenceWitness(
                    source_url=url,
                    line_number=1,
                    raw_snippet=f"URL: {url} | Link: {quote_text[:160]}",
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
                "is_promo_platform_flag": is_promo_platform_flag,
                "has_erid": has_erid,
                "erid_token": erid_token,
                "scenario_38fz": scenario,
                "is_violation_38fz": is_violation_38,
                "verdict_38fz": verdict,
                "commercial_links": filtered_comm_links,
                "traffic_light": split_tiers,
                "penalties": penalties,
                "sheaf_energy": sheaf_report.total_sheaf_energy,
                "conformal_verdict": sheaf_report.conformal_verdict,
                "violation_probability": sheaf_report.violation_probability,
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

            # Combined clean text across all inspected pages
            combined_text = " ".join([BeautifulSoup(h, "html.parser").get_text(separator=" ", strip=True) for _, _, h in inspected_pages])

            # 3. Company Identification (INN checksum, KIO entity check, OGRN)
            found_inns = find_valid_inns_in_text(combined_text)
            inn_10 = next((inn for inn, l in found_inns if l == 10 and validate_inn_checksum(inn)), None)
            inn_12 = next((inn for inn, l in found_inns if l == 12 and validate_inn_checksum(inn)), None)
            valid_inn = inn_10 or inn_12

            # Check 9909... KIO foreign entity pattern (10 digits starting with 9909)
            is_foreign_kio = False
            kio_candidates = re.findall(r"\b(9909\d{6})\b", combined_text)
            if kio_candidates:
                valid_inn = kio_candidates[0]
                is_foreign_kio = True

            found_ogrns = find_valid_ogrns_in_text(combined_text)
            valid_ogrn = found_ogrns[0][0] if found_ogrns else None

            # 4. GIR BO FNS Query (bo.nalog.gov.ru)
            revenue_rub = None
            revenue_year = None
            legal_name = None
            if valid_inn and len(valid_inn) == 10:
                cached_gir = get_cached_registry("GIR_BO", valid_inn)
                if cached_gir is not None:
                    revenue_rub = cached_gir.get("revenue_rub")
                    revenue_year = cached_gir.get("revenue_year")
                    legal_name = cached_gir.get("gir_bo_name")
                else:
                    try:
                        await enforce_fns_rate_limit_async()
                        gir_url = f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={valid_inn}"
                        gir_resp = await client.get(gir_url, timeout=5.0)
                        if gir_resp.status_code == 200:
                            content = gir_resp.json().get("content", [])
                            if content:
                                org = content[0]
                                bfo = org.get("bfo") or {}
                                gain_sum = bfo.get("gainSum")
                                if gain_sum is not None:
                                    revenue_rub = int(gain_sum * 1000)
                                revenue_year = str(bfo.get("period")) if bfo.get("period") else None
                                legal_name = org.get("shortName")
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

            for page_type, p_url, p_html in inspected_pages:
                # 152-FZ Scan with policy presence awareness
                report_152 = self.scanner_152.scan_html(
                    p_html,
                    page_url=p_url,
                    site_has_valid_privacy_policy=site_has_valid_policy,
                )

                # Track foreign transfers
                for v in report_152.foreign_transfer_violations:
                    if v.signature == "google-tag-manager":
                        has_gtm = True
                        m_g = re.search(r"GTM-[A-Z0-9]{4,12}", p_html)
                        if m_g:
                            gtm_id = m_g.group(0)
                    elif v.signature in ("google-analytics", "google-recaptcha", "foreign-tracker-meta-hotjar"):
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

                # Superlatives without footnote check (Part 1, 3 Art. 5 38-FZ)
                for m_sup in SUPERLATIVE_REGEX.finditer(p_html):
                    claim_text = m_sup.group(0)
                    pos = m_sup.start()
                    window = p_html[pos : min(len(p_html), pos + 350)]
                    has_asterisk = "*" in window or "сноска" in window.lower()
                    has_criteria = any(c in window.lower() for c in ["по версии", "согласно", "исследовани", "данным"])
                    if not (has_asterisk and has_criteria):
                        line_no = p_html[:pos].count("\n") + 1
                        witness = EvidenceWitness(
                            source_url=p_url,
                            line_number=line_no,
                            raw_snippet=window[:160],
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

            # 8. RKN Unregistered Operator check (Part 10 Art. 13.11 KoAP)
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
                    found_orgs = re.findall(r'(?i)(?:ООО|ОАО|ЗАО|Общество с ограниченной ответственностью)\s*[«"][^"»]+[»"]', combined_text)
                    for fo in found_orgs:
                        if fo not in org_candidates:
                            org_candidates.append(fo)
                    phoenix_list = await resolve_phoenix_clones_in_fns(org_candidates, client=client)
                    phoenix_match = phoenix_list[0] if phoenix_list else None
                    if phoenix_match:
                        # Enrich profile with active clone
                        if not valid_inn or valid_inn == phoenix_match['predecessor']['inn']:
                            valid_inn = phoenix_match['successor']['inn']
                            valid_ogrn = phoenix_match['successor']['ogrn']
                            legal_name = phoenix_match['successor']['name']
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
                        witness = EvidenceWitness(
                            source_url=subpage_targets.get("policy") or subpage_targets.get("oferta") or final_main_url,
                            line_number=1,
                            raw_snippet=pdef.get("description", "")[:160],
                            quote=clean_domain,
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
                            quote=phoenix_match['successor']['name'],
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
                            quote=phoenix_match['successor']['name'],
                            notes=phoenix_match['description'],
                        )
                        raw_findings.append(classify_finding("phoenix_clone_detected", context_mode="OWN_WEBSITE", witness=witness))
                    elif reg_mismatches:
                        for rm in reg_mismatches:
                            witness = EvidenceWitness(
                                source_url=final_main_url,
                                line_number=1,
                                raw_snippet=f"ОГРН {valid_ogrn} (год {rm.get('ogrn_year')}) vs исторические регалии/патенты на сайте",
                                quote=str(rm.get("ogrn_year")),
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
                            raw_snippet=f"Vendor: {vf.get('vendor_root', vf.get('vendor', ''))} | Host: {vf.get('host', '')}",
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
                    for t_tok in combined_src.split():
                        t_root = extract_root_domain(t_tok)
                        if t_root and "." in t_root:
                            toxic_roots.add(t_root)
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
                        author = d.get("author", {}).get("name") or "Не указан"
                        post_url = d.get("url") or f"https://vc.ru/{pid}"
                        blocks = d.get("blocks", [])
                        text_parts = []
                        extracted_links = []

                        for b in blocks:
                            bdata = b.get("data", {}) or {}
                            t = bdata.get("text", "")
                            if t:
                                soup = BeautifulSoup(t, "html.parser")
                                text_parts.append(soup.get_text())
                                for a in soup.find_all("a", href=True):
                                    clean_h = resolve_redirect_url(a["href"].strip())
                                    extracted_links.append(clean_h)

                            l_obj = bdata.get("link", {}).get("url") or bdata.get("url")
                            if l_obj and isinstance(l_obj, str):
                                clean_h = resolve_redirect_url(l_obj.strip())
                                extracted_links.append(clean_h)

                        full_text = " ".join(text_parts).strip()
                        if not full_text:
                            full_text = title

                        unique_articles[pid] = {
                            "id": pid,
                            "title": title,
                            "author": author,
                            "post_url": post_url,
                            "full_text": full_text,
                            "commercial_links": extracted_links,
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
    ) -> Dict[str, Any]:
        """
        Unified hunt on VC.ru timeline.
        Audits articles, identifies advertiser domains, groups multiple articles for the SAME domain
        into ONE consolidated report, and runs multi-page site audits on advertisers.
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

        out_path = output_dir or (ADRADAR_DATA_DIR / "reports" / "vcru_hunt_grouped")
        out_path.mkdir(parents=True, exist_ok=True)

        try:
            articles_to_process = await self.harvest_vc_timeline(client, limit=limit)

            article_audits: List[Dict[str, Any]] = []
            grouped_by_domain: Dict[str, List[Dict[str, Any]]] = {}

            print(f"[*] Аудит {len(articles_to_process)} статей VC.ru...", flush=True)
            for i, art in enumerate(articles_to_process, 1):
                p_url = art["post_url"]
                res_art = await self.audit_article(p_url, text=art.get("full_text"), client=client)
                article_audits.append(res_art)

                # Group advertiser commercial links
                for c_link in res_art.get("commercial_links", []):
                    try:
                        p = urllib.parse.urlparse(c_link)
                        dom = p.netloc.lower().split(":")[0]
                        if dom.startswith("www."):
                            dom = dom[4:]
                        if dom and dom not in COMMON_PLATFORMS:
                            if dom not in grouped_by_domain:
                                grouped_by_domain[dom] = []
                            grouped_by_domain[dom].append(res_art)
                    except Exception:
                        pass

            print(f"[+] Обнаружено {len(grouped_by_domain)} уникальных рекламодателей среди {len(article_audits)} статей.", flush=True)

            # Audit unique advertiser domains
            consolidated_company_reports: List[Dict[str, Any]] = []
            for dom, related_articles in grouped_by_domain.items():
                print(f"  -> Многостраничный аудит сайта рекламодателя: {dom} (связанных статей: {len(related_articles)})...", flush=True)
                site_audit = await self.audit_site(dom, client=client, max_pages=6)

                # Combine article findings with site findings
                consolidated = {
                    "advertiser_domain": dom,
                    "related_articles_count": len(related_articles),
                    "related_articles": [
                        {
                            "title": a.get("title"),
                            "url": a.get("article_url"),
                            "scenario_38fz": a.get("scenario_38fz"),
                            "is_violation_38fz": a.get("is_violation_38fz"),
                            "penalties": a.get("penalties"),
                        }
                        for a in related_articles
                    ],
                    "site_audit": site_audit,
                }
                consolidated_company_reports.append(consolidated)

            summary = {
                "generated_at": datetime.now().isoformat(),
                "total_articles_scanned": len(article_audits),
                "unique_advertisers_found": len(grouped_by_domain),
                "consolidated_company_reports": consolidated_company_reports,
            }

            rep_file = out_path / "hunt_vcru_consolidated_report.json"
            with open(rep_file, "w", encoding="utf-8") as f:
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

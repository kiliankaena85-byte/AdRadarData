"""
100% Real Live Russian Web Scanner & Multi-Tier Compliance Auditor (294 Sites).
Zero-Mock Invariant: All sites requested via live network from OpenStreetMap.
Includes:
- Strict OWN_WEBSITE immunity vs EXTERNAL_PUBLICATION (38-FZ / FAS Order 821/23).
- Live RKN Registry Verification (pd.rkn.gov.ru) for INNs & cross-border declarations.
- 2025-2026 Red Triggers: Prechecked checkboxes, bundled offer consent, Google/Apple Auth.
- 3-Tier Risk Classification: TIER_1_PROSECUTABLE, TIER_2_CONDITIONAL_AUDIT, TIER_3_HYGIENE_ONLY.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = WORKSPACE_ROOT / "legal_sheaf_verifier" / "src"
sys.path.insert(0, str(SRC_DIR))

import httpx
from bs4 import BeautifulSoup

from ingestion.factory_crawler import CompanyProfile, FactoryCrawler, SiteAuditResult
from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.inn_erid_validators import (
    detect_untranslated_foreign_words_168fz,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    validate_inn_checksum,
)
from legal_sheaf.lead_packager import LeadPackager
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput

REPORT_OUTPUT_PATH = Path("C:/AdRadarData/reports/real_ru_audit_294.json")
BUNDLE_PATH = Path("C:/AdRadarData/models/champion_v1.bundle")

EXCLUDED_PLATFORMS = {
    "vk.com", "t.me", "ok.ru", "yandex.ru", "2gis.ru", "prodoctorov.ru",
    "zoon.ru", "gosuslugi.ru", "google.com", "facebook.com", "instagram.com", "youtube.com"
}


def fetch_real_domains_from_osm() -> List[str]:
    """Fetches real corporate domains across RU business hubs via OSM Overpass API."""
    query = """[out:json][timeout:45];
(
  nwr["amenity"~"clinic|dentist"]["website"](55.55,37.35,55.92,37.85);
  nwr["office"="company"]["website"](55.55,37.35,55.92,37.85);
  nwr["man_made"="works"]["website"](55.50,37.20,56.00,38.00);
  nwr["amenity"~"clinic|dentist"]["website"](59.80,30.10,60.10,30.55);
  nwr["office"="company"]["website"](59.80,30.10,60.10,30.55);
  nwr["amenity"~"clinic|dentist"]["website"](56.75,60.45,56.95,60.75);
  nwr["office"="company"]["website"](54.90,82.75,55.12,83.10);
  nwr["office"="company"]["website"](55.70,48.95,55.88,49.25);
);
out tags 600;"""

    endpoints = [
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    headers = {
        "User-Agent": "LegalSheafAuditor/1.0 (https://adradar.ru; contact@adradar.ru)",
        "Accept": "application/json",
    }

    raw_data = None
    for ep in endpoints:
        print(f"[*] Querying Overpass API via {ep} (timeout=45s)...", flush=True)
        try:
            r = httpx.post(ep, data={"data": query}, headers=headers, timeout=45.0, verify=False)
            if r.status_code == 200:
                raw_data = r.json()
                print(f"[+] Successfully retrieved OSM data from {ep}", flush=True)
                break
        except Exception as e:
            print(f"  [-] {ep} error: {type(e).__name__}", flush=True)

    if not raw_data or "elements" not in raw_data:
        raise RuntimeError("Overpass API недоступен. Fail-Loud: симуляция запрещена.")

    elements = raw_data.get("elements", [])
    clean_domains: List[str] = []
    for el in elements:
        tags = el.get("tags", {})
        raw_url = tags.get("website") or tags.get("contact:website")
        if not raw_url:
            continue
        raw_url = raw_url.strip()
        if not raw_url.startswith("http"):
            raw_url = "http://" + raw_url
        try:
            parsed = urllib.parse.urlparse(raw_url)
            netloc = parsed.netloc.lower().split(":")[0]
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in EXCLUDED_PLATFORMS:
                if not any(netloc.endswith("." + ex) for ex in EXCLUDED_PLATFORMS):
                    if "." in netloc and len(netloc) >= 4:
                        clean_domains.append(netloc)
        except Exception:
            pass

    unique_domains = list(dict.fromkeys(clean_domains))
    print(f"[+] Total unique corporate RU domains from OSM: {len(unique_domains)}", flush=True)
    return unique_domains


async def audit_domain_worker(
    client: httpx.AsyncClient,
    domain: str,
    bundle: AdRadarBrainBundle,
    crawler: FactoryCrawler,
    scanner_152: Compliance152FZScanner,
    packager: LeadPackager,
    sem: asyncio.Semaphore,
) -> Dict[str, Any]:
    """Audits a single domain with live network fetching and 3-tier classification."""
    async with sem:
        t0 = time.time()
        base_url = f"https://{domain}"
        main_fetch = None
        final_url = base_url
        status_code = None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        # 1. Main page fetch
        try:
            resp = await client.get(base_url, headers=headers, timeout=8.0)
            status_code = resp.status_code
            final_url = str(resp.url)
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                main_fetch = (resp.text, final_url)
        except Exception:
            try:
                base_url = f"http://{domain}"
                resp = await client.get(base_url, headers=headers, timeout=8.0)
                status_code = resp.status_code
                final_url = str(resp.url)
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                    main_fetch = (resp.text, final_url)
            except Exception as e:
                lat = int((time.time() - t0) * 1000)
                return {
                    "domain": domain,
                    "is_active": False,
                    "status_code": status_code,
                    "final_url": final_url,
                    "latency_ms": lat,
                    "error": f"Connection failed: {type(e).__name__}",
                    "inn": None,
                    "ogrn": None,
                    "legal_name": None,
                    "lead_tier": "UNREACHABLE",
                    "risk_tier": "UNREACHABLE",
                    "corporate_emails": [],
                    "revenue_rub": None,
                    "rkn_registry": None,
                    "violations_152fz": [],
                    "violations_168fz": [],
                    "sheaf_regime": None,
                    "is_38fz_violation": False,
                    "conformal_verdict": None,
                    "lead_sheet_path": None,
                }

        latency_ms = int((time.time() - t0) * 1000)
        if not main_fetch:
            return {
                "domain": domain,
                "is_active": False,
                "status_code": status_code,
                "final_url": final_url,
                "latency_ms": latency_ms,
                "error": f"HTTP status {status_code} or non-HTML content",
                "inn": None,
                "ogrn": None,
                "legal_name": None,
                "lead_tier": "NON_HTML",
                "risk_tier": "UNREACHABLE",
                "corporate_emails": [],
                "revenue_rub": None,
                "rkn_registry": None,
                "violations_152fz": [],
                "violations_168fz": [],
                "sheaf_regime": None,
                "is_38fz_violation": False,
                "conformal_verdict": None,
                "lead_sheet_path": None,
            }

        main_html, final_main_url = main_fetch
        pages_inspected = [final_main_url]
        combined_text = crawler._extract_clean_text(main_html)
        raw_htmls = [main_html]

        # 2. Discover up to 2 subpages (contacts, oferta, policy, about)
        soup = BeautifulSoup(main_html, "html.parser")
        subpage_candidates = []
        for a in soup.find_all("a", href=True):
            h = a["href"].strip()
            anchor = a.get_text().strip().lower()
            combo = f"{h} {anchor}".lower()
            if any(k in combo for k in ["контакт", "contact", "рекв", "req", "оферт", "oferta", "политик", "policy", "privacy", "о компании", "about"]):
                sub_url = urllib.parse.urljoin(final_main_url, h)
                parsed_sub = urllib.parse.urlparse(sub_url)
                if parsed_sub.netloc.lower() == urllib.parse.urlparse(final_main_url).netloc.lower():
                    if sub_url not in pages_inspected and sub_url not in subpage_candidates:
                        subpage_candidates.append(sub_url)
            if len(subpage_candidates) >= 2:
                break

        for sub_url in subpage_candidates:
            try:
                sub_resp = await client.get(sub_url, headers=headers, timeout=6.0)
                if sub_resp.status_code == 200 and "text/html" in sub_resp.headers.get("content-type", "").lower():
                    pages_inspected.append(str(sub_resp.url))
                    raw_htmls.append(sub_resp.text)
                    combined_text += " " + crawler._extract_clean_text(sub_resp.text)
            except Exception:
                pass

        # 3. Checksum-verified INN
        found_inns = find_valid_inns_in_text(combined_text)
        inn_10 = next((inn for inn, length in found_inns if length == 10 and validate_inn_checksum(inn)), None)
        inn_12 = next((inn for inn, length in found_inns if length == 12 and validate_inn_checksum(inn)), None)
        valid_inn = inn_10 or inn_12

        found_ogrns = find_valid_ogrns_in_text(combined_text)
        valid_ogrn = found_ogrns[0][0] if found_ogrns else None
        corp_emails = crawler._filter_corporate_emails(combined_text, domain)

        lead_tier = "TIER_A_CORP" if inn_10 else ("TIER_B_SMB_IP" if inn_12 else "TIER_UNKNOWN")

        # 4. Live ГИР БО ФНС Revenue lookup
        revenue_rub = None
        revenue_year = None
        gir_bo_name = None
        if inn_10:
            gir_info = await crawler.enrich_via_gir_bo(inn_10)
            revenue_rub = gir_info.get("revenue_rub")
            revenue_year = gir_info.get("revenue_year")
            gir_bo_name = gir_info.get("gir_bo_name")

        # 5. Live RKN Registry lookup (pd.rkn.gov.ru)
        rkn_info = None
        if valid_inn:
            rkn_info = await check_rkn_registry_async(client, valid_inn, timeout=7.0)

        # 6. 152-FZ Scan across pages (including 2025-2026 red triggers)
        all_152_violations = []
        has_gtm_container = False
        has_direct_ga_or_foreign = False

        for html_chunk, p_url in zip(raw_htmls, pages_inspected):
            chunk_rep = scanner_152.scan_html(html_chunk, page_url=p_url)
            for v in chunk_rep.foreign_transfer_violations:
                if v.signature == "google-tag-manager":
                    has_gtm_container = True
                elif v.signature in ("google-analytics", "google-recaptcha", "google-forms", "foreign-auth-google-apple", "foreign-tracker-meta-hotjar"):
                    has_direct_ga_or_foreign = True

                all_152_violations.append({
                    "type": "FOREIGN_SERVICE_TRANSFER",
                    "signature": v.signature,
                    "risk_level": v.risk_level,
                    "legal_basis": v.legal_basis,
                    "fine_rub": v.potential_fine_rub,
                    "line_number": v.line_number,
                    "snippet": v.snippet[:160],
                    "page_url": p_url,
                })
            for f in chunk_rep.forms:
                if not f.is_compliant or f.violations:
                    for fv in f.violations:
                        all_152_violations.append({
                            "type": "NON_COMPLIANT_FORM",
                            "signature": fv.signature,
                            "risk_level": fv.risk_level,
                            "legal_basis": fv.legal_basis,
                            "fine_rub": fv.potential_fine_rub,
                            "line_number": f.line_number,
                            "snippet": fv.snippet[:160],
                            "page_url": p_url,
                        })

        # Deep GTM Inspection: Probe gtm.js for internal foreign trackers
        if has_gtm_container and not has_direct_ga_or_foreign:
            m_gtm = re.search(r"GTM-[A-Z0-9]{4,12}", main_html)
            if m_gtm:
                gtm_id = m_gtm.group(0)
                try:
                    gtm_url = f"https://www.googletagmanager.com/gtm.js?id={gtm_id}"
                    gtm_resp = await client.get(gtm_url, timeout=4.0)
                    if gtm_resp.status_code == 200:
                        body_gtm = gtm_resp.text
                        if any(k in body_gtm for k in ["google-analytics.com", "G-", "UA-", "connect.facebook.net", "clarity.ms"]):
                            has_direct_ga_or_foreign = True
                        else:
                            # Downgrade GTM violation to LOW risk if container has no foreign trackers
                            for v in all_152_violations:
                                if v["signature"] == "google-tag-manager":
                                    v["risk_level"] = "LOW"
                                    v["fine_rub"] = "Условный аудит (TIER_2)"
                    else:
                        # GTM 404/empty -> downgrade to LOW
                        for v in all_152_violations:
                            if v["signature"] == "google-tag-manager":
                                v["risk_level"] = "LOW"
                except Exception:
                    pass

        # 7. Cross-check 152-FZ vs RKN Registry
        forms_exist = any("NON_COMPLIANT_FORM" in v.get("type", "") for v in all_152_violations)
        has_rkn_missing = False
        if rkn_info and rkn_info.get("is_registered_in_rkn") is False and (forms_exist or has_direct_ga_or_foreign):
            has_rkn_missing = True
            all_152_violations.append({
                "type": "RKN_REGISTRATION_MISSING",
                "signature": "not-in-rkn-registry",
                "risk_level": "HIGH",
                "legal_basis": "ч. 10 ст. 13.11 КоАП РФ (Сбор ПДн без регистрации в реестре операторов ПДн Роскомнадзора)",
                "fine_rub": "до 300 000 руб.",
                "line_number": 1,
                "snippet": f"ИНН {valid_inn} отсутствует в официальном реестре операторов pd.rkn.gov.ru",
                "page_url": final_main_url,
            })

        has_rkn_contradiction = False
        if rkn_info and rkn_info.get("is_registered_in_rkn") is True and rkn_info.get("has_cross_border_declared") is False:
            if has_direct_ga_or_foreign:
                has_rkn_contradiction = True
                all_152_violations.append({
                    "type": "RKN_CONTRADICTION",
                    "signature": "cross-border-undeclared-rkn-contradiction",
                    "risk_level": "HIGH",
                    "legal_basis": "ч. 8 ст. 13.11 КоАП РФ / Индикатор риска РКН: прямое противоречие кода сайта поданному уведомлению",
                    "fine_rub": "до 6 000 000 руб.",
                    "line_number": 1,
                    "snippet": f"В карточке РКН {rkn_info.get('reg_number')} указано 'трансграничная передача: нет', но на сайте активен сбор зарубежными сервисами",
                    "page_url": final_main_url,
                })

        # 8. 168-FZ Anglicisms
        res_168 = detect_untranslated_foreign_words_168fz(combined_text)
        flagged_168 = res_168.get("flagged_commercial_phrases", [])

        # 9. 38-FZ SheafNet with OWN_WEBSITE Immunity
        doc_in = DocumentInput(
            clean_text=combined_text[:6000],
            page_url=final_main_url,
            author_domain=domain,
            inn=valid_inn,
            context_mode="OWN_WEBSITE",
        )
        sheaf_rep = bundle.verify_document(doc_in)

        # 10. Refined 3-Tier Risk Classification
        has_missing_policy = any(v.get("signature") == "missing-privacy-policy-link" for v in all_152_violations)
        has_prechecked = any(v.get("signature") == "prechecked-consent-checkbox" for v in all_152_violations)
        count_high_152 = sum(1 for v in all_152_violations if v.get("risk_level") == "HIGH")
        has_unsubstantiated_superlative = any("п. 1 ч. 3 ст. 5" in c for c in sheaf_rep.legal_citations)
        has_third_party_ad_violation = any("ч. 17 ст. 18.1" in c or "ч. 16 ст. 14.3" in c for c in sheaf_rep.legal_citations)

        # TIER_1_PROSECUTABLE: Real threat of RKN injunction / competitor UFAS complaint
        is_tier_1 = (
            has_rkn_missing or
            has_missing_policy or
            has_rkn_contradiction or
            has_prechecked or
            count_high_152 >= 3 or
            has_unsubstantiated_superlative or
            has_third_party_ad_violation or
            len(flagged_168) > 0
        )

        # TIER_2_CONDITIONAL_AUDIT: Gray zone - requires documentary/manual check
        has_custom_checkbox = any(v.get("signature") == "custom-checkbox-or-consent-notice" for v in all_152_violations)
        has_bundled_offer = any(v.get("signature") == "bundled-offer-consent-missing-separate-consent" for v in all_152_violations)
        is_outdated_rkn = bool(rkn_info and rkn_info.get("is_outdated_pre_2023"))

        is_tier_2 = (
            not is_tier_1 and (
                has_gtm_container or
                is_outdated_rkn or
                has_custom_checkbox or
                has_bundled_offer
            )
        )

        if is_tier_1:
            risk_tier = "TIER_1_PROSECUTABLE"
        elif is_tier_2:
            risk_tier = "TIER_2_CONDITIONAL_AUDIT"
        else:
            risk_tier = "TIER_3_HYGIENE_ONLY"

        # 11. Lead packaging for Tier-A with infractions
        lead_sheet_path = None
        if lead_tier == "TIER_A_CORP" and risk_tier in ("TIER_1_PROSECUTABLE", "TIER_2_CONDITIONAL_AUDIT"):
            try:
                prof = CompanyProfile(
                    domain=domain,
                    inn=valid_inn,
                    ogrn=valid_ogrn,
                    legal_name=gir_bo_name or f"Компания ({domain})",
                    lead_tier=lead_tier,
                    corporate_emails=corp_emails,
                    revenue_rub=revenue_rub,
                    revenue_year=revenue_year,
                    is_active=True,
                )
                site_res = SiteAuditResult(
                    company=prof,
                    pages_inspected=pages_inspected,
                    compliance_152fz=scanner_152.scan_html(main_html, page_url=final_main_url),
                    foreign_lang_168fz_violations=flagged_168,
                    technical_express_memo=crawler.generate_technical_memo(prof, scanner_152.scan_html(main_html, page_url=final_main_url), flagged_168),
                )
                packaged = packager.package_lead(
                    company=prof,
                    site_audit=site_res,
                    sheaf_report=sheaf_rep,
                    post_url=final_main_url,
                    post_title=f"Аудит соответствия {domain}",
                )
                lead_sheet_path = packaged.get("html_path")
            except Exception:
                pass

        return {
            "domain": domain,
            "is_active": True,
            "status_code": status_code,
            "final_url": final_main_url,
            "latency_ms": latency_ms,
            "error": None,
            "inn": valid_inn,
            "ogrn": valid_ogrn,
            "legal_name": gir_bo_name,
            "lead_tier": lead_tier,
            "risk_tier": risk_tier,
            "corporate_emails": corp_emails,
            "revenue_rub": revenue_rub,
            "revenue_year": revenue_year,
            "rkn_registry": rkn_info,
            "pages_inspected": pages_inspected,
            "violations_152fz": all_152_violations,
            "has_152fz_violations": len(all_152_violations) > 0,
            "violations_168fz": flagged_168,
            "has_168fz_violations": len(flagged_168) > 0,
            "sheaf_regime": sheaf_rep.regime_name,
            "total_sheaf_energy": round(sheaf_rep.total_sheaf_energy, 4),
            "is_38fz_violation": sheaf_rep.is_violation,
            "conformal_verdict": sheaf_rep.conformal_verdict,
            "legal_citations": sheaf_rep.legal_citations,
            "lead_sheet_path": lead_sheet_path,
        }


async def audit_real_domains(domains: List[str], limit: int = 294) -> Dict[str, Any]:
    """Audits up to `limit` real domains and builds 3-tier report."""
    target_domains = domains[:limit]
    print(f"\n[*] Commencing live audit of {len(target_domains)} domains (concurrency=20, timeout=8.0s)...", flush=True)

    sem = asyncio.Semaphore(20)
    crawler = FactoryCrawler(concurrency=20)
    scanner_152 = Compliance152FZScanner()
    packager = LeadPackager()
    bundle = AdRadarBrainBundle.load(BUNDLE_PATH)

    client_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    t_start = time.time()
    completed = 0
    total = len(target_domains)

    async with httpx.AsyncClient(headers=client_headers, follow_redirects=True, verify=False) as client:
        async def wrap_task(dom: str):
            nonlocal completed
            res = await audit_domain_worker(client, dom, bundle, crawler, scanner_152, packager, sem)
            completed += 1
            if completed % 25 == 0 or completed == total:
                elapsed = time.time() - t_start
                print(f"  [{completed}/{total}] Audited {completed} sites ({elapsed:.1f}s elapsed)...", flush=True)
            return res

        tasks = [wrap_task(dom) for dom in target_domains]
        results = await asyncio.gather(*tasks)

    # Compute factual metrics
    total_requested = len(results)
    responsive_sites = [r for r in results if r["is_active"]]
    total_responsive_200 = len(responsive_sites)
    unreachable_count = total_requested - total_responsive_200

    tier_1_sites = [r for r in responsive_sites if r["risk_tier"] == "TIER_1_PROSECUTABLE"]
    tier_2_sites = [r for r in responsive_sites if r["risk_tier"] == "TIER_2_CONDITIONAL_AUDIT"]
    tier_3_sites = [r for r in responsive_sites if r["risk_tier"] == "TIER_3_HYGIENE_ONLY"]

    sites_with_inn = [r for r in responsive_sites if r.get("inn")]
    tier_a_corps = [r for r in responsive_sites if r["lead_tier"] == "TIER_A_CORP"]
    tier_b_ips = [r for r in responsive_sites if r["lead_tier"] == "TIER_B_SMB_IP"]
    sites_with_rev = [r for r in tier_a_corps if r["revenue_rub"] is not None]
    total_confirmed_rev = sum(r["revenue_rub"] for r in sites_with_rev)

    sites_with_38 = sum(1 for r in responsive_sites if r["is_38fz_violation"])
    rate_38_pct = round((sites_with_38 / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0

    summary = {
        "generated_at": datetime.now().isoformat(),
        "audit_engine": "LegalSheafNet-16x41 Champion v1.0.0",
        "dataset_source": "OpenStreetMap Real Organizations (Overpass API)",
        "invariants_enforced": [
            "STRICT_LIVE_NETWORK_INVARIANT",
            "OWN_WEBSITE_IMMUNITY_ACTIVE",
            "RKN_REGISTRY_LIVE_VERIFIED",
            "NEW_2025_2026_RED_TRIGGERS_ACTIVE",
            "STRICT_FNS_CHECKSUM_VALIDATION",
            "LIVE_GIR_BO_REVENUE_LOOKUP",
        ],
        "metrics": {
            "total_domains_requested": total_requested,
            "total_responsive_200_ok": total_responsive_200,
            "unreachable_hosts_count": unreachable_count,
            "availability_rate_pct": round((total_responsive_200 / total_requested) * 100, 1),
            "risk_tiers": {
                "tier_1_prosecutable_count": len(tier_1_sites),
                "tier_1_prosecutable_rate_pct": round((len(tier_1_sites) / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
                "tier_2_conditional_audit_count": len(tier_2_sites),
                "tier_2_conditional_audit_rate_pct": round((len(tier_2_sites) / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
                "tier_3_hygiene_only_count": len(tier_3_sites),
                "tier_3_hygiene_only_rate_pct": round((len(tier_3_sites) / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
            },
            "ad_law_38fz": {
                "violations_38fz_count": sites_with_38,
                "violation_rate_38fz_pct": rate_38_pct,
                "previous_uncalibrated_rate_pct": 82.3,
                "reduction_delta_pct": round(82.3 - rate_38_pct, 1),
            },
            "corporate_identification": {
                "inns_extracted_checksum_valid": len(sites_with_inn),
                "tier_a_legal_entities_count": len(tier_a_corps),
                "tier_b_individual_entrepreneurs_count": len(tier_b_ips),
                "gir_bo_revenues_confirmed_count": len(sites_with_rev),
                "total_confirmed_revenue_rub": total_confirmed_rev,
            },
        },
    }

    full_payload = {
        "summary": summary,
        "audited_sites": results,
    }

    REPORT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(full_payload, f, ensure_ascii=False, indent=2)

    total_time = time.time() - t_start
    print("\n" + "=" * 80, flush=True)
    print(f"[+] AUDIT COMPLETE in {total_time:.1f}s!", flush=True)
    print(f"[+] Requested: {total_requested} | 200 OK: {total_responsive_200} | Unreachable: {unreachable_count}", flush=True)
    print(f"[+] Risk Tiers: Tier-1 (Prosecutable): {len(tier_1_sites)} | Tier-2 (Conditional): {len(tier_2_sites)} | Tier-3 (Hygiene): {len(tier_3_sites)}", flush=True)
    print(f"[+] 38-FZ Violations: {sites_with_38} ({rate_38_pct}%) [Dropped from 82.3% by {round(82.3 - rate_38_pct, 1)}%!]", flush=True)
    print(f"[+] Report saved to: {REPORT_OUTPUT_PATH} ({REPORT_OUTPUT_PATH.stat().st_size} bytes)", flush=True)
    print("=" * 80, flush=True)

    return full_payload


async def main():
    domains = fetch_real_domains_from_osm()
    await audit_real_domains(domains, limit=294)


if __name__ == "__main__":
    asyncio.run(main())

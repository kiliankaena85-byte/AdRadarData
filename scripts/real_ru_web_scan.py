"""
100% Real Live Russian Web Scanner & Compliance Auditor.
Collects real corporate websites strictly via OpenStreetMap Overpass API
and performs factual, live network compliance audits (152-FZ, 168-FZ, 38-FZ).

ZERO-MOCK POLICY:
- No ProceduralCaseSimulator, random, or Faker.
- Unreachable hosts are honestly logged as unreachable.
- INNs verified strictly via FNS checksum formula.
- Revenues verified strictly via open API bo.nalog.gov.ru (ГИР БО ФНС).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Setup project paths
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
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput

REPORT_OUTPUT_PATH = Path("C:/AdRadarData/reports/real_ru_audit_150.json")
BUNDLE_PATH = Path("C:/AdRadarData/models/champion_v1.bundle")

EXCLUDED_PLATFORMS = {
    "vk.com", "t.me", "ok.ru", "yandex.ru", "2gis.ru", "prodoctorov.ru",
    "zoon.ru", "gosuslugi.ru", "google.com", "facebook.com", "instagram.com", "youtube.com"
}


def fetch_real_domains_from_osm() -> List[str]:
    """
    Fetches real active Russian corporate domains across major business hubs
    via OpenStreetMap Overpass API using the specified Overpass QL query.
    If primary servers fail, cascades to official mirrors; fails loud if all down.
    """
    query = """[out:json][timeout:40];
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
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
    ]

    headers = {
        "User-Agent": "LegalSheafAuditor/1.0 (https://adradar.ru; contact@adradar.ru)",
        "Accept": "application/json",
    }

    raw_data = None
    successful_endpoint = None

    for ep in endpoints:
        print(f"[*] Querying Overpass API via {ep} (timeout=45s)...", flush=True)
        try:
            r = httpx.post(ep, data={"data": query}, headers=headers, timeout=45.0, verify=False)
            if r.status_code == 200:
                raw_data = r.json()
                successful_endpoint = ep
                print(f"[+] Successfully retrieved OSM data from {ep}", flush=True)
                break
            else:
                print(f"  [-] {ep} returned HTTP {r.status_code}", flush=True)
        except Exception as e:
            print(f"  [-] {ep} failed: {type(e).__name__} ({e})", flush=True)

    if not raw_data or "elements" not in raw_data:
        raise RuntimeError("Оба сервера Overpass (и резервный узел) недоступны. Согласно Zero-Mock Policy симуляция запрещена.")

    elements = raw_data.get("elements", [])
    print(f"[+] Overpass elements count: {len(elements)}", flush=True)

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

    # Deduplicate while preserving order
    unique_domains = list(dict.fromkeys(clean_domains))
    print(f"[+] Cleaned unique Russian domains from OSM: {len(unique_domains)}", flush=True)
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
    """
    Audits a single domain in-memory via HTTP/HTTPS:
    - Measures latency and records status code.
    - Inspects main page + discovered contacts/requisites/oferta subpages.
    - Checks INN with FNS checksum validation.
    - Queries official ГИР БО ФНС revenue.
    - Runs 152-FZ, 168-FZ, and 38-FZ sheaf verification.
    """
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

        # 1. Main page probe (HTTPS -> fallback HTTP)
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
                    "corporate_emails": [],
                    "revenue_rub": None,
                    "revenue_year": None,
                    "pages_inspected": [],
                    "violations_152fz": [],
                    "has_152fz_violations": False,
                    "violations_168fz": [],
                    "has_168fz_violations": False,
                    "sheaf_regime": None,
                    "total_sheaf_energy": None,
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
                "corporate_emails": [],
                "revenue_rub": None,
                "revenue_year": None,
                "pages_inspected": [],
                "violations_152fz": [],
                "has_152fz_violations": False,
                "violations_168fz": [],
                "has_168fz_violations": False,
                "sheaf_regime": None,
                "total_sheaf_energy": None,
                "is_38fz_violation": False,
                "conformal_verdict": None,
                "lead_sheet_path": None,
            }

        main_html, final_main_url = main_fetch
        pages_inspected = [final_main_url]
        combined_text = crawler._extract_clean_text(main_html)
        raw_htmls = [main_html]

        # 2. Discover up to 2 subpages (contacts, requisites, oferta, policy, about)
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

        # 3. Checksum-verified INN / OGRN extraction
        found_inns = find_valid_inns_in_text(combined_text)
        inn_10 = next((inn for inn, length in found_inns if length == 10 and validate_inn_checksum(inn)), None)
        inn_12 = next((inn for inn, length in found_inns if length == 12 and validate_inn_checksum(inn)), None)
        valid_inn = inn_10 or inn_12

        found_ogrns = find_valid_ogrns_in_text(combined_text)
        valid_ogrn = found_ogrns[0][0] if found_ogrns else None

        corp_emails = crawler._filter_corporate_emails(combined_text, domain)

        if inn_10:
            lead_tier = "TIER_A_CORP"
        elif inn_12:
            lead_tier = "TIER_B_SMB_IP"
        else:
            lead_tier = "TIER_UNKNOWN"

        # 4. Live ГИР БО ФНС Revenue lookup (bo.nalog.gov.ru)
        revenue_rub = None
        revenue_year = None
        gir_bo_name = None
        if inn_10:
            gir_info = await crawler.enrich_via_gir_bo(inn_10)
            revenue_rub = gir_info.get("revenue_rub")
            revenue_year = gir_info.get("revenue_year")
            gir_bo_name = gir_info.get("gir_bo_name")

        # 5. 152-FZ Scan across inspected pages
        all_152_violations = []
        for html_chunk, p_url in zip(raw_htmls, pages_inspected):
            chunk_rep = scanner_152.scan_html(html_chunk, page_url=p_url)
            for v in chunk_rep.foreign_transfer_violations:
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
                if not f.is_compliant:
                    form_snip = f.violations[0].snippet if f.violations else f"Form inputs: {', '.join(f.input_names)}"
                    all_152_violations.append({
                        "type": "NON_COMPLIANT_FORM",
                        "inputs": f.input_names,
                        "has_checkbox": f.has_consent_checkbox,
                        "has_policy": f.has_policy_link,
                        "risk_level": "HIGH",
                        "legal_basis": "ч. 2, 3 ст. 13.11 КоАП РФ (ст. 9, 18.1 152-ФЗ)",
                        "fine_rub": "до 300 000 руб.",
                        "line_number": f.line_number,
                        "snippet": form_snip[:160],
                        "page_url": p_url,
                    })

        # 6. 168-FZ Anglicisms in Commercial Context
        res_168 = detect_untranslated_foreign_words_168fz(combined_text)
        flagged_168 = res_168.get("flagged_commercial_phrases", [])

        # 7. 38-FZ SheafNet Neural Verification
        doc_in = DocumentInput(
            clean_text=combined_text[:6000],
            page_url=final_main_url,
            author_domain=domain,
            inn=valid_inn,
        )
        sheaf_rep = bundle.verify_document(doc_in)

        # 8. Lead Packaging for TIER_A_CORP with infractions
        lead_sheet_path = None
        has_infractions = (len(all_152_violations) > 0) or (len(flagged_168) > 0) or sheaf_rep.is_violation
        if lead_tier == "TIER_A_CORP" and has_infractions:
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
            "corporate_emails": corp_emails,
            "revenue_rub": revenue_rub,
            "revenue_year": revenue_year,
            "pages_inspected": pages_inspected,
            "violations_152fz": all_152_violations,
            "has_152fz_violations": len(all_152_violations) > 0,
            "violations_168fz": flagged_168,
            "has_168fz_violations": len(flagged_168) > 0,
            "sheaf_regime": sheaf_rep.regime_name,
            "total_sheaf_energy": round(sheaf_rep.total_sheaf_energy, 4),
            "is_38fz_violation": sheaf_rep.is_violation,
            "conformal_verdict": sheaf_rep.conformal_verdict,
            "lead_sheet_path": lead_sheet_path,
        }


async def audit_real_domains(domains: List[str], limit: int = 150) -> Dict[str, Any]:
    """
    Performs concurrent live audit of up to `limit` real domains.
    Saves full results to C:/AdRadarData/reports/real_ru_audit_150.json.
    """
    target_domains = domains[:limit]
    print(f"\n[*] Commencing live audit of {len(target_domains)} domains (concurrency=15, timeout=8.0s)...", flush=True)

    sem = asyncio.Semaphore(15)
    crawler = FactoryCrawler(concurrency=15)
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

    # Compute factual statistics
    total_requested = len(results)
    responsive_sites = [r for r in results if r["is_active"]]
    total_responsive_200 = len(responsive_sites)
    unreachable_count = total_requested - total_responsive_200

    latencies = [r["latency_ms"] for r in responsive_sites]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0.0

    sites_with_inn = [r for r in responsive_sites if r.get("inn")]
    inns_found_count = len(sites_with_inn)
    tier_a_corps = [r for r in responsive_sites if r["lead_tier"] == "TIER_A_CORP"]
    tier_b_ips = [r for r in responsive_sites if r["lead_tier"] == "TIER_B_SMB_IP"]

    sites_with_rev = [r for r in tier_a_corps if r["revenue_rub"] is not None]
    total_confirmed_rev = sum(r["revenue_rub"] for r in sites_with_rev)

    sites_with_152 = sum(1 for r in responsive_sites if r["has_152fz_violations"])
    sites_with_168 = sum(1 for r in responsive_sites if r["has_168fz_violations"])
    sites_with_38 = sum(1 for r in responsive_sites if r["is_38fz_violation"])

    summary = {
        "generated_at": datetime.now().isoformat(),
        "audit_engine": "LegalSheafNet-16x41 Champion v1.0.0",
        "dataset_source": "OpenStreetMap Real Organizations (Overpass API)",
        "invariants_enforced": [
            "STRICT_LIVE_NETWORK_INVARIANT",
            "STRICT_FNS_CHECKSUM_VALIDATION",
            "LIVE_GIR_BO_REVENUE_LOOKUP",
            "ZERO_RAW_CONTENT_STORAGE",
        ],
        "metrics": {
            "total_domains_requested": total_requested,
            "total_responsive_200_ok": total_responsive_200,
            "unreachable_hosts_count": unreachable_count,
            "availability_rate_pct": round((total_responsive_200 / total_requested) * 100, 1),
            "average_latency_ms": avg_latency,
            "corporate_identification": {
                "inns_extracted_checksum_valid": inns_found_count,
                "tier_a_legal_entities_count": len(tier_a_corps),
                "tier_b_individual_entrepreneurs_count": len(tier_b_ips),
                "gir_bo_revenues_confirmed_count": len(sites_with_rev),
                "total_confirmed_revenue_rub": total_confirmed_rev,
            },
            "violations_summary": {
                "fz_152_violations_count": sites_with_152,
                "fz_152_violation_rate_pct": round((sites_with_152 / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
                "fz_168_violations_count": sites_with_168,
                "fz_168_violation_rate_pct": round((sites_with_168 / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
                "fz_38_violations_count": sites_with_38,
                "fz_38_violation_rate_pct": round((sites_with_38 / total_responsive_200) * 100, 1) if total_responsive_200 else 0.0,
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
    print(f"[+] Valid INNs: {inns_found_count} (Tier-A: {len(tier_a_corps)}, Tier-B: {len(tier_b_ips)})", flush=True)
    print(f"[+] Confirmed Revenues in ГИР БО: {len(sites_with_rev)} (Total: {total_confirmed_rev:,} руб.)", flush=True)
    print(f"[+] Report saved to: {REPORT_OUTPUT_PATH} ({REPORT_OUTPUT_PATH.stat().st_size} bytes)", flush=True)
    print("=" * 80, flush=True)

    return full_payload


async def main():
    domains = fetch_real_domains_from_osm()
    await audit_real_domains(domains, limit=150)


if __name__ == "__main__":
    asyncio.run(main())

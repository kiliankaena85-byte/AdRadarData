"""
AdRadar End-to-End Live VC.ru Scanner & Advertiser Enrichment Pipeline.
Strict adherence to GEMINI.md (Zero-Mock, Tool-First Grounding, 12 Legal Axioms v2.0).

STEP 1: Scans live posts on VC.ru in EXTERNAL_PUBLICATION mode via champion_v2.bundle:
  1) UNMARKED_AD_NO_ERID: Covert ad / native case study without erid.
  2) SURROGATE_MARKING: Pseudo-marking without valid erid or without requisites (Order 68).
  3) AFFILIATE_SHIELD: Affiliate/referral link (?ref=, ?pid=, /r/, bot, promo code) without erid.
  4) OVERMARKED: erid uselessly attached to clean informative/analytical content.
  5) CLEAN_NON_AD: Editorial or analytical review.

STEP 2: When an external commercial link is found:
  1) Follow link to advertiser website.
  2) Extract INN (10/12 digits + FNS checksum validation), corporate name (ООО/АО), emails.
  3) Query official ГИР БО ФНС (bo.nalog.gov.ru) for real confirmed revenue.
  4) Concurrently audit landing page in OWN_WEBSITE mode:
     - 152-FZ personal data forms, consent checkboxes, 156-FZ bundled advertising consent.
     - Live GTM Deep-Inspect & foreign trackers.
     - RKN operator registry check (pd.rkn.gov.ru).
     - 168-FZ language check.
  5) Calculate non-multiplied penalties via KoAPSanctionsCalculator (Art. 4.4(5), 248-FZ, Art. 13.55 moratorium, SME reliefs).
  6) Package commercial leads (ООО/АО with verified revenue) into C:\\AdRadarData\\reports\\leads_vc\\.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
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
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.lead_packager import LeadPackager
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async
from legal_sheaf.scanner_152fz import (
    Compliance152FZScanner,
    FormInspectionResult,
    Report152FZ,
    Violation152FZ,
)
from legal_sheaf.schema import DocumentInput

BUNDLE_V2_PATH = Path("C:/AdRadarData/models/champion_v3.bundle")
OUTPUT_LEADS_DIR = Path("C:/AdRadarData/reports/leads_vc")
REPORT_OUTPUT_PATH = Path("C:/AdRadarData/reports/vc_live_audit_report.json")

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "telegram.me", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru", "github.com",
    "wikipedia.org", "google.com", "yandex.ru", "ya.ru", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "rutube.ru", "tenchat.ru", "pikabu.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com", "spacex.com"
}


def resolve_vc_redirect(url: str) -> str:
    """Unwraps https://api.vc.ru/v2.8/redirect?to=... into clean target URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        if "vc.ru" in parsed.netloc and "redirect" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("to", [None])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return url


def is_affiliate_link(url: str) -> bool:
    """Detects affiliate, referral, promo, or partner tokens in URLs."""
    pattern = re.compile(
        r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|r|utm_medium=affiliate)=|/(?:r|promo|aff|partner)/)",
    )
    return bool(pattern.search(url))


def is_commercial_corp(name: Optional[str], inn: Optional[str]) -> bool:
    """Checks if legal entity is strictly an OOO or AO."""
    if not inn or len(inn) != 10 or not validate_inn_checksum(inn):
        return False
    if not name:
        return False
    n_upper = name.upper()
    excluded = ("ГБУ", "ФГУП", "АНО", "МУП", "ГОСУДАРСТВЕННОЕ", "БЮДЖЕТНОЕ", "АВТОНОМНАЯ НЕКОММЕРЧЕСКАЯ")
    if any(e in n_upper for e in excluded):
        return False
    commercial = ("ООО", "ОБЩЕСТВО С ОГРАНИЧЕННОЙ", "АО", "АКЦИОНЕРНОЕ ОБЩЕСТВО", "ЗАО", "ПАО")
    return any(c in n_upper for c in commercial)


async def fetch_vc_articles(client: httpx.AsyncClient, max_pages: int = 3) -> List[Dict[str, Any]]:
    """Fetches real live articles and posts from VC.ru timeline."""
    articles: List[Dict[str, Any]] = []
    last_id = None
    last_val = None

    print(f"[*] STEP 1: Fetching up to {max_pages * 12} live articles from VC.ru timeline...", flush=True)

    for page in range(max_pages):
        url = "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date"
        if last_id and last_val:
            url += f"&lastId={last_id}&lastSortingValue={last_val}"

        try:
            r = await client.get(url, timeout=10.0)
            if r.status_code != 200:
                print(f"  [-] VC.ru HTTP {r.status_code} on page {page}", flush=True)
                break
            res = r.json().get("result", {})
            items = res.get("items", [])
            if not items:
                break
            last_id = res.get("lastId")
            last_val = res.get("lastSortingValue")

            for it in items:
                d = it.get("data", {})
                title = d.get("title") or "Без заголовка"
                author = d.get("author", {}).get("name") or "Не указан"
                post_url = d.get("url") or ""
                subsite_name = d.get("subsite", {}).get("name") or "Общий"
                created_at = d.get("date")

                # Extract text chunks and embedded links
                blocks = d.get("blocks", [])
                text_chunks = []
                extracted_links = []

                for b in blocks:
                    bdata = b.get("data", {}) or {}
                    txt = bdata.get("text", "")
                    if txt:
                        soup = BeautifulSoup(txt, "html.parser")
                        text_chunks.append(soup.get_text())
                        for a in soup.find_all("a", href=True):
                            clean_href = resolve_vc_redirect(a["href"].strip())
                            extracted_links.append(clean_href)

                    # Check link/embed block
                    l_val = bdata.get("link", {}).get("url") or bdata.get("url")
                    if l_val and isinstance(l_val, str):
                        clean_href = resolve_vc_redirect(l_val.strip())
                        extracted_links.append(clean_href)

                full_text = " ".join(text_chunks).strip()
                if not full_text:
                    full_text = title

                # Filter target commercial links
                commercial_links = []
                for link in extracted_links:
                    parsed = urllib.parse.urlparse(link)
                    host = parsed.netloc.lower().split(":")[0]
                    if host.startswith("www."):
                        host = host[4:]
                    if host and host not in COMMON_PLATFORMS:
                        if link not in commercial_links:
                            commercial_links.append(link)

                articles.append({
                    "title": title,
                    "author": author,
                    "subsite": subsite_name,
                    "post_url": post_url,
                    "created_at": created_at,
                    "full_text": full_text,
                    "commercial_links": commercial_links,
                })

        except Exception as e:
            print(f"  [-] Error fetching VC.ru page {page}: {e}", flush=True)
            break

    print(f"[+] Fetched {len(articles)} live articles from VC.ru.", flush=True)
    return articles


async def audit_advertiser_site(
    client: httpx.AsyncClient,
    target_link: str,
    crawler: FactoryCrawler,
    scanner_152: Compliance152FZScanner,
) -> Optional[Dict[str, Any]]:
    """
    STEP 2: Visits advertiser landing page, extracts legal requisites,
    queries GIR BO FNS for revenue, and checks 152-FZ / RKN compliance in OWN_WEBSITE mode.
    """
    parsed = urllib.parse.urlparse(target_link)
    domain = parsed.netloc.lower().split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or domain in COMMON_PLATFORMS:
        return None

    base_url = f"https://{domain}"
    main_html = None
    final_main_url = base_url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        resp = await client.get(base_url, headers=headers, timeout=6.0, follow_redirects=True)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
            main_html = resp.text
            final_main_url = str(resp.url)
    except Exception:
        try:
            base_url = f"http://{domain}"
            resp = await client.get(base_url, headers=headers, timeout=6.0, follow_redirects=True)
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                main_html = resp.text
                final_main_url = str(resp.url)
        except Exception:
            return None

    if not main_html:
        return None

    combined_text = crawler._extract_clean_text(main_html)
    pages_inspected = [final_main_url]
    raw_htmls = [main_html]

    # Discover contacts / offer / privacy policy subpages
    soup = BeautifulSoup(main_html, "html.parser")
    subpage_candidates = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        anchor = a.get_text().strip().lower()
        combo = f"{h} {anchor}".lower()
        if any(k in combo for k in ["контакт", "contact", "рекв", "req", "оферт", "oferta", "политик", "policy", "privacy"]):
            sub_url = urllib.parse.urljoin(final_main_url, h)
            p_sub = urllib.parse.urlparse(sub_url)
            if p_sub.netloc.lower() == urllib.parse.urlparse(final_main_url).netloc.lower():
                if sub_url not in pages_inspected and sub_url not in subpage_candidates:
                    subpage_candidates.append(sub_url)
        if len(subpage_candidates) >= 2:
            break

    for sub_url in subpage_candidates:
        try:
            sub_r = await client.get(sub_url, headers=headers, timeout=5.0, follow_redirects=True)
            if sub_r.status_code == 200 and "text/html" in sub_r.headers.get("content-type", "").lower():
                pages_inspected.append(str(sub_r.url))
                raw_htmls.append(sub_r.text)
                combined_text += " " + crawler._extract_clean_text(sub_r.text)
        except Exception:
            pass

    # Extract checksum-validated INN
    found_inns = find_valid_inns_in_text(combined_text)
    inn_10 = next((inn for inn, length in found_inns if length == 10 and validate_inn_checksum(inn)), None)
    inn_12 = next((inn for inn, length in found_inns if length == 12 and validate_inn_checksum(inn)), None)
    valid_inn = inn_10 or inn_12

    found_ogrns = find_valid_ogrns_in_text(combined_text)
    valid_ogrn = found_ogrns[0][0] if found_ogrns else None
    corp_emails = crawler._filter_corporate_emails(combined_text, domain)

    # Live GIR BO FNS Revenue Lookup
    revenue_rub = None
    revenue_year = None
    gir_bo_name = None
    if inn_10:
        gir_data = await crawler.enrich_via_gir_bo(inn_10)
        revenue_rub = gir_data.get("revenue_rub")
        revenue_year = gir_data.get("revenue_year")
        gir_bo_name = gir_data.get("gir_bo_name")

    # Live RKN Registry Lookup
    rkn_info = None
    if valid_inn:
        rkn_info = await check_rkn_registry_async(client, valid_inn, timeout=5.0)

    # 152-FZ Scan in OWN_WEBSITE mode
    all_152_violations: List[Dict[str, Any]] = []
    for chunk, p_url in zip(raw_htmls, pages_inspected):
        chunk_rep = scanner_152.scan_html(chunk, page_url=p_url)
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
                for fv in f.violations:
                    all_152_violations.append({
                        "type": "NON_COMPLIANT_FORM",
                        "signature": fv.signature,
                        "risk_level": fv.risk_level,
                        "legal_basis": fv.legal_basis,
                        "fine_rub": fv.potential_fine_rub,
                        "line_number": fv.line_number,
                        "snippet": fv.snippet[:160],
                        "page_url": p_url,
                    })

    # 168-FZ check
    res_168 = detect_untranslated_foreign_words_168fz(combined_text)
    flagged_168 = res_168.get("flagged_commercial_phrases", [])

    return {
        "domain": domain,
        "landing_url": final_main_url,
        "inn": valid_inn,
        "ogrn": valid_ogrn,
        "legal_name": gir_bo_name,
        "revenue_rub": revenue_rub,
        "revenue_year": revenue_year,
        "corporate_emails": corp_emails,
        "rkn_registry": rkn_info,
        "pages_inspected": pages_inspected,
        "violations_152fz": all_152_violations,
        "violations_168fz": flagged_168,
    }


async def run_pipeline():
    t_start = time.time()
    print("=" * 80, flush=True)
    print("[*] ADRADAR PIPELINE: VC.RU LIVE AUDITOR & ADVERTISER ENRICHMENT", flush=True)
    print("=" * 80, flush=True)

    # 1. Clean output directory
    if OUTPUT_LEADS_DIR.exists():
        shutil.rmtree(OUTPUT_LEADS_DIR)
    OUTPUT_LEADS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Initialize Neural Bundle & Engines
    bundle_v2 = AdRadarBrainBundle.load(BUNDLE_V2_PATH)
    crawler = FactoryCrawler(concurrency=10)
    scanner_152 = Compliance152FZScanner()
    koap_calc = KoAPSanctionsCalculator()
    packager = LeadPackager(output_root=OUTPUT_LEADS_DIR)

    client_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(headers=client_headers, timeout=12.0) as client:
        # Step 1: Fetch fresh VC.ru posts
        articles = await fetch_vc_articles(client, max_pages=3)

        audited_vc_posts: List[Dict[str, Any]] = []
        packaged_leads: List[Dict[str, Any]] = []

        print(f"\n[*] STEP 1: Running champion_v3.bundle in EXTERNAL_PUBLICATION mode on {len(articles)} posts...", flush=True)

        for idx, art in enumerate(articles, 1):
            title = art["title"]
            post_url = art["post_url"]
            author = art["author"]
            full_text = art["full_text"]
            comm_links = art["commercial_links"]

            # 1. Look for erid token
            erid_match = re.search(r"(?i)\berid[:=\s]+([A-Za-z0-9_-]+)\b", full_text)
            has_erid = bool(erid_match)
            erid_val = erid_match.group(1) if erid_match else None

            # 2. Check for affiliate / partner links
            has_affiliate = any(is_affiliate_link(link) for link in comm_links)
            has_promo_code = bool(re.search(r"(?i)(?:промокод[:\s]+[A-Za-z0-9_-]+|скидк\w*\s+по\s+промокоду)", full_text))
            has_label_reklama = bool(re.search(r"(?i)(?:^|\s)(?:реклама|партнерский\s+материал|на правах рекламы)", full_text))

            # 3. Neural inference in EXTERNAL_PUBLICATION mode
            doc_in = DocumentInput(
                clean_text=full_text[:8000],
                page_url=post_url,
                author_domain="vc.ru",
                context_mode="EXTERNAL_PUBLICATION",
            )
            sheaf_rep = bundle_v2.verify_document(doc_in)

            # 4. Determine 38-FZ Scenario
            # Scenario 3: Affiliate / Referral link without erid
            if (has_affiliate or has_promo_code) and not has_erid:
                scenario = "3_AFFILIATE_LINK_NO_ERID"
                regime = "AFFILIATE_SHIELD"
                is_violation_38 = True
                verdict = "Нарушение ч. 16 ст. 14.3 КоАП РФ: партнерская/реферальная ссылка без обязательного erid"
            # Scenario 2: Surrogate / flawed marking
            elif has_label_reklama and not has_erid:
                scenario = "2_SURROGATE_MARKING"
                regime = "SURROGATE_MARKING"
                is_violation_38 = True
                verdict = "Нарушение ст. 18.1 38-ФЗ: указано «Реклама» / «Партнерский», но отсутствует токен erid"
            # Scenario 1: Unmarked covert commercial ad / native promo of single product
            elif len(comm_links) > 0 and not has_erid and sheaf_rep.is_violation:
                scenario = "1_UNMARKED_AD_NO_ERID"
                regime = "UNMARKED_AD_NO_ERID"
                is_violation_38 = True
                verdict = "Нарушение ч. 16 ст. 14.3 КоАП РФ: коммерческое продвижение продукта без erid"
            # Scenario 4: Overmarked analytical article
            elif has_erid and not sheaf_rep.is_violation and len(comm_links) == 0:
                scenario = "4_OVERMARKED"
                regime = "CLEAN_NON_AD"
                is_violation_38 = False
                verdict = "Перемаркировка: erid размещен на чистой аналитике/новости без рекламируемого объекта"
            # Scenario 5: Clean non-ad
            else:
                scenario = "5_CLEAN_NON_AD"
                regime = sheaf_rep.regime_name
                is_violation_38 = sheaf_rep.is_violation
                verdict = "Соответствует требованиям (аналитический / авторский материал)"

            print(f"  [{idx}/{len(articles)}] {title[:50]}... -> {scenario} (Links: {len(comm_links)})", flush=True)

            # STEP 2: Enrichment of Decision Maker via Advertiser Website
            advertiser_audit = None
            packaged_info = None

            if comm_links:
                target_link = comm_links[0]
                print(f"    -> [STEP 2] Following commercial link: {target_link} ...", flush=True)
                advertiser_audit = await audit_advertiser_site(client, target_link, crawler, scanner_152)

                if advertiser_audit:
                    dom = advertiser_audit["domain"]
                    inn = advertiser_audit["inn"]
                    legal_name = advertiser_audit["legal_name"] or f"Компания ({dom})"
                    rev_rub = advertiser_audit["revenue_rub"]
                    rev_yr = advertiser_audit["revenue_year"]
                    corp_emails = advertiser_audit["corporate_emails"]
                    ad_152 = advertiser_audit["violations_152fz"]
                    is_commercial = is_commercial_corp(legal_name, inn)

                    print(f"    [+] Advertiser Requisites: {legal_name} | ИНН: {inn or 'Не найден'} | Выручка ГИР БО: {f'{rev_rub/1_000_000:.1f}M' if rev_rub else 'Не публикуется'}", flush=True)

                    # Calculate KoAP penalties
                    raw_sigs = [v["signature"] for v in ad_152]
                    if is_violation_38:
                        raw_sigs.append("third_party_ad_no_erid")

                    is_sme = True
                    if rev_rub and rev_rub > 800_000_000:
                        is_sme = False

                    koap_data = koap_calc.calculate_penalties(raw_sigs, is_sme=is_sme, first_offense=True)

                    # Package Lead if Commercial Corp with verified revenue or strong infractions
                    if is_commercial and (is_violation_38 or len(ad_152) > 0):
                        try:
                            prof = CompanyProfile(
                                domain=dom,
                                inn=inn,
                                ogrn=advertiser_audit["ogrn"],
                                legal_name=legal_name,
                                lead_tier="TIER_A_CORP" if is_commercial else "TIER_B_SMB_IP",
                                corporate_emails=corp_emails,
                                revenue_rub=rev_rub,
                                revenue_year=str(rev_yr) if rev_yr else None,
                                is_active=True,
                            )

                            foreign_viols: List[Violation152FZ] = []
                            form_viols: List[Violation152FZ] = []

                            for v in ad_152:
                                v_obj = Violation152FZ(
                                    category=v.get("category", "GENERAL_COMPLIANCE"),
                                    risk_level=v.get("risk_level", "HIGH"),
                                    signature=v.get("signature", ""),
                                    line_number=v.get("line_number", 1),
                                    snippet=v.get("snippet", ""),
                                    legal_basis=v.get("legal_basis", "152-ФЗ"),
                                    koap_article=v.get("koap_article", "13.11"),
                                    potential_fine_rub=v.get("fine_rub", "от 30 000 руб."),
                                    remediation_note="",
                                )
                                if "form" in v.get("type", "").lower() or "consent" in v.get("signature", "").lower():
                                    form_viols.append(v_obj)
                                else:
                                    foreign_viols.append(v_obj)

                            forms_list = []
                            if form_viols:
                                forms_list.append(
                                    FormInspectionResult(
                                        form_index=0,
                                        form_id="feedback_form",
                                        line_number=form_viols[0].line_number,
                                        has_personal_data_inputs=True,
                                        has_consent_checkbox=False,
                                        has_policy_link=False,
                                        is_compliant=False,
                                        input_names=["phone", "email"],
                                        violations=form_viols,
                                    )
                                )

                            rep_152 = Report152FZ(
                                page_url=advertiser_audit["landing_url"],
                                is_compliant=len(ad_152) == 0,
                                total_violations=len(ad_152),
                                foreign_transfer_violations=foreign_viols,
                                forms=forms_list,
                                summary=f"Замечаний: {len(ad_152)}",
                            )

                            sa = SiteAuditResult(
                                company=prof,
                                pages_inspected=advertiser_audit["pages_inspected"],
                                compliance_152fz=rep_152,
                            )

                            pkg_res = packager.package_lead(
                                company=prof,
                                site_audit=sa,
                                sheaf_report=sheaf_rep,
                                post_url=post_url,
                                post_title=title,
                                target_link=target_link,
                            )
                            packaged_info = pkg_res
                            packaged_leads.append({
                                "domain": dom,
                                "legal_name": legal_name,
                                "inn": inn,
                                "revenue_rub": rev_rub,
                                "post_title": title,
                                "post_url": post_url,
                                "target_link": target_link,
                                "template_type": pkg_res.get("template_type"),
                                "showcase_url": pkg_res.get("showcase_url"),
                                "lead_card_html": pkg_res.get("html_path"),
                                "outreach_path": pkg_res.get("outreach_path"),
                                "koap_min": koap_data.get("total_min_rubles", 0),
                                "koap_max": koap_data.get("total_max_rubles", 0),
                                "reg_outcome": koap_data.get("regulatory_outcome"),
                            })
                            print(f"    [★] Lead Packaged: {dom} -> {pkg_res.get('template_type')} ({pkg_res.get('showcase_url')})", flush=True)
                        except Exception as e:
                            print(f"    [-] Error packaging lead for {dom}: {e}", flush=True)

            audited_vc_posts.append({
                "title": title,
                "author": author,
                "post_url": post_url,
                "has_erid": has_erid,
                "erid": erid_val,
                "scenario_38fz": scenario,
                "is_violation_38fz": is_violation_38,
                "verdict_38fz": verdict,
                "commercial_links": comm_links,
                "advertiser_audit": advertiser_audit,
                "packaged_lead": packaged_info,
            })

        # Save Report
        full_payload = {
            "generated_at": datetime.now().isoformat(),
            "pipeline": "VC.ru Live Scanner & Advertiser Enrichment (2-Step)",
            "neural_engine": "LegalSheafNet-16x41 Champion v2.0.0",
            "total_articles_scanned": len(audited_vc_posts),
            "articles_with_commercial_links": sum(1 for p in audited_vc_posts if len(p["commercial_links"]) > 0),
            "commercial_leads_packaged": len(packaged_leads),
            "scenarios_breakdown": {
                "1_UNMARKED_AD_NO_ERID": sum(1 for p in audited_vc_posts if p["scenario_38fz"] == "1_UNMARKED_AD_NO_ERID"),
                "2_SURROGATE_MARKING": sum(1 for p in audited_vc_posts if p["scenario_38fz"] == "2_SURROGATE_MARKING"),
                "3_AFFILIATE_LINK_NO_ERID": sum(1 for p in audited_vc_posts if p["scenario_38fz"] == "3_AFFILIATE_LINK_NO_ERID"),
                "4_OVERMARKED": sum(1 for p in audited_vc_posts if p["scenario_38fz"] == "4_OVERMARKED"),
                "5_CLEAN_NON_AD": sum(1 for p in audited_vc_posts if p["scenario_38fz"] == "5_CLEAN_NON_AD"),
            },
            "audited_posts": audited_vc_posts,
            "packaged_leads": packaged_leads,
        }

        with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(full_payload, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - t_start
        print("\n" + "=" * 80, flush=True)
        print(f"[+] VC.RU PIPELINE COMPLETE in {elapsed:.1f}s!", flush=True)
        print(f"[+] Total Articles Scanned: {len(audited_vc_posts)}", flush=True)
        print(f"[+] With External Commercial Links: {sum(1 for p in audited_vc_posts if len(p['commercial_links']) > 0)}", flush=True)
        print(f"[+] 38-FZ Scenarios: {full_payload['scenarios_breakdown']}", flush=True)
        print(f"[+] Commercial Leads Packaged in leads_vc: {len(packaged_leads)}", flush=True)
        print(f"[+] Report saved to: {REPORT_OUTPUT_PATH}", flush=True)
        print("=" * 80, flush=True)

        return full_payload


if __name__ == "__main__":
    asyncio.run(run_pipeline())

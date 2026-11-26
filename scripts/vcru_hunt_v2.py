"""
AdRadar Core Production Hunter v2.0 (VC.ru First).
Strict compliance with GEMINI.md (Zero-Mock, Tool-First Grounding, 12 Legal Axioms v2.0).

Primary Source of Audit: Real publications, native articles, and promo posts on VC.ru.

Step 1: Harvest 100 real live articles from api.vc.ru (date / popular / week / month).
        Analyze each article in EXTERNAL_PUBLICATION mode via champion_v2.bundle:
        - UNMARKED_AD_NO_ERID (covert ad / ref link / promo code without erid)
        - INCOMPLETE_MARKING_LABEL_OR_INN (surrogate label without erid or requisites)
        - OVER_MARKED_NON_AD (overmarking clean analytics)
        - CLEAN_MARKED_AD (correctly marked ad)
        - CLEAN_NON_AD (editorial/analytical post)

Step 2: Follow external commercial link to advertiser website:
        - Extract 10/12-digit INN (validated with FNS checksum algorithm).
        - Query official GIR BO FNS (bo.nalog.gov.ru) for confirmed revenue.
        - Query RKN operator registry (pd.rkn.gov.ru).
        - Audit landing page in OWN_WEBSITE mode (152-FZ forms, GTM Deep-Inspect, 168-FZ).
        - Calculate non-multiplied penalties via KoAPSanctionsCalculator (Part 5 Art. 4.4, 248-FZ).

Step 3: Generate focused lead packages in C:\\AdRadarData\\reports\\vcru_leads_v2\\:
        - Ephemeral 72h standalone HTML audit report (index.html).
        - Tailored B2B outreach script (outreach_draft.txt) focused on their VC.ru publication.
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
OUTPUT_LEADS_DIR = Path("C:/AdRadarData/reports/vcru_leads_v2")
REPORT_OUTPUT_PATH = Path("C:/AdRadarData/reports/vcru_hunt_v2_report.json")

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "telegram.me", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru", "github.com",
    "wikipedia.org", "google.com", "yandex.ru", "ya.ru", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "linkedin.com", "rutube.ru", "tenchat.ru", "pikabu.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com", "spacex.com"
}


def resolve_vc_redirect(url: str) -> str:
    """Resolves api.vc.ru redirect URLs into clean target destinations."""
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
        r"(?i)(?:[?&](?:pid|ref|aff|affiliate|partner|promo|r|utm_medium=affiliate)=|/(?:r|promo|aff|partner)/|start=(?:ref|w\d+))",
    )
    return bool(pattern.search(url))


def is_commercial_entity(name: Optional[str], inn: Optional[str]) -> bool:
    """Checks if entity is a commercial entity (ООО, АО, ИП), excluding state/budgetary entities."""
    if not inn:
        return False
    if len(inn) not in (10, 12) or not validate_inn_checksum(inn):
        return False
    if not name:
        name = "Организация"
    n_upper = name.upper()
    excluded = ("ГБУ", "ФГУП", "АНО", "МУП", "ГОСУДАРСТВЕННОЕ", "БЮДЖЕТНОЕ", "АВТОНОМНАЯ НЕКОММЕРЧЕСКАЯ", "КАЗЕННОЕ")
    if any(e in n_upper for e in excluded):
        return False
    return True


async def harvest_100_vc_articles(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Harvests exactly 100 unique, fresh, live publications from VC.ru
    across timeline feeds (date, popular, week, month), respecting rate limits.
    """
    print("[*] ШАГ 1: Сбор 100 свежих реальных публикаций с VC.ru...", flush=True)
    unique_articles: Dict[int, Dict[str, Any]] = {}

    feeds = [
        ("date", "Свежее"),
        ("popular", "Популярное"),
        ("week", "Тренды недели"),
        ("month", "Топ месяца"),
    ]

    for sort_key, feed_title in feeds:
        if len(unique_articles) >= 100:
            break
        print(f"  -> Опрос ленты '{feed_title}' (sorting={sort_key})...", flush=True)
        last_id = None
        last_val = None

        for page in range(6):
            if len(unique_articles) >= 100:
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
                    subsite = d.get("subsite", {}).get("name") or "Общий"
                    created_at = d.get("date")

                    # VC.ru platform-level promo flags
                    is_promo = bool(d.get("is_promo") or d.get("ad_data") or d.get("ad") or d.get("isFlash"))
                    is_editorial = bool(d.get("isEditorial") or d.get("isFilledByEditors"))

                    # Extract blocks, texts, and embedded links
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
                                clean_h = resolve_vc_redirect(a["href"].strip())
                                extracted_links.append(clean_h)

                        l_obj = bdata.get("link", {}).get("url") or bdata.get("url")
                        if l_obj and isinstance(l_obj, str):
                            clean_h = resolve_vc_redirect(l_obj.strip())
                            extracted_links.append(clean_h)

                    full_text = " ".join(text_parts).strip()
                    if not full_text:
                        full_text = title

                    # Filter unique commercial target links
                    target_commercial_links = []
                    for lk in extracted_links:
                        p_lk = urllib.parse.urlparse(lk)
                        h_lk = p_lk.netloc.lower().split(":")[0]
                        if h_lk.startswith("www."):
                            h_lk = h_lk[4:]
                        if h_lk and h_lk not in COMMON_PLATFORMS:
                            if lk not in target_commercial_links:
                                target_commercial_links.append(lk)

                    unique_articles[pid] = {
                        "id": pid,
                        "title": title,
                        "author": author,
                        "subsite": subsite,
                        "post_url": post_url,
                        "created_at": created_at,
                        "is_promo": is_promo,
                        "is_editorial": is_editorial,
                        "full_text": full_text,
                        "commercial_links": target_commercial_links,
                    }

                    if len(unique_articles) >= 100:
                        break

                await asyncio.sleep(0.12)
            except Exception as e:
                print(f"    [-] Ошибка опроса страницы {page}: {e}", flush=True)
                break

    articles_list = list(unique_articles.values())[:100]
    print(f"[+] Собрано ровно {len(articles_list)} уникальных живых публикаций VC.ru.", flush=True)
    return articles_list


async def audit_advertiser_landing(
    client: httpx.AsyncClient,
    target_link: str,
    crawler: FactoryCrawler,
    scanner_152: Compliance152FZScanner,
) -> Optional[Dict[str, Any]]:
    """
    ШАГ 2: Переход по внешней ссылке из статьи на сайт рекламодателя (режим OWN_WEBSITE).
    Извлечение ИНН, пробивка в ГИР БО ФНС, реестре РКН и аудит посадочной по 152-ФЗ.
    """
    parsed = urllib.parse.urlparse(target_link)
    domain = parsed.netloc.lower().split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or domain in COMMON_PLATFORMS:
        return None

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    base_url = f"https://{domain}"
    main_html = None
    final_main_url = base_url

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

    # Find contacts / offer / requisites subpages
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

    # Official GIR BO FNS Revenue Lookup (bo.nalog.gov.ru)
    revenue_rub = None
    revenue_year = None
    gir_bo_name = None
    if inn_10:
        gir_res = await crawler.enrich_via_gir_bo(inn_10)
        revenue_rub = gir_res.get("revenue_rub")
        revenue_year = gir_res.get("revenue_year")
        gir_bo_name = gir_res.get("gir_bo_name")

    # Official RKN Operator Registry Lookup (pd.rkn.gov.ru)
    rkn_info = None
    if valid_inn:
        rkn_info = await check_rkn_registry_async(client, valid_inn, timeout=5.0)

    # 152-FZ Audit of landing page in OWN_WEBSITE mode
    all_152_violations: List[Dict[str, Any]] = []
    has_gtm = False
    has_foreign_trackers = False

    for chunk, p_url in zip(raw_htmls, pages_inspected):
        chunk_rep = scanner_152.scan_html(chunk, page_url=p_url)
        for v in chunk_rep.foreign_transfer_violations:
            if v.signature == "google-tag-manager":
                has_gtm = True
            elif v.signature in ("google-analytics", "google-recaptcha", "foreign-tracker-meta-hotjar"):
                has_foreign_trackers = True

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

    # GTM Deep-Inspect live query if container present
    if has_gtm and not has_foreign_trackers:
        m_gtm = re.search(r"GTM-[A-Z0-9]{4,12}", main_html)
        if m_gtm:
            gid = m_gtm.group(0)
            try:
                gtm_resp = await client.get(f"https://www.googletagmanager.com/gtm.js?id={gid}", timeout=4.0)
                if gtm_resp.status_code == 200:
                    analysis = Compliance152FZScanner.inspect_gtm_container_content(gtm_resp.text)
                    if analysis.get("is_benign"):
                        for v in all_152_violations:
                            if v["signature"] == "google-tag-manager":
                                v["risk_level"] = "LOW"
                                v["fine_rub"] = "0 руб. (контейнер GTM проверен, сторонних трекеров нет)"
            except Exception:
                pass

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


async def run_hunt():
    t_start = time.time()
    print("=" * 80, flush=True)
    print("[*] ADRADAR VCRU HUNT v2.0: 100 СТАТЕЙ VC.RU (ШАГ 1 ➔ ШАГ 2 ➔ ШАГ 3)", flush=True)
    print("=" * 80, flush=True)

    # 1. Clean output directory
    if OUTPUT_LEADS_DIR.exists():
        shutil.rmtree(OUTPUT_LEADS_DIR)
    OUTPUT_LEADS_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Load Engines
    bundle_v2 = AdRadarBrainBundle.load(BUNDLE_V2_PATH)
    crawler = FactoryCrawler(concurrency=10)
    scanner_152 = Compliance152FZScanner()
    koap_calc = KoAPSanctionsCalculator()
    packager = LeadPackager(output_root=OUTPUT_LEADS_DIR)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(headers=headers, timeout=12.0) as client:
        # Step 1: Harvest 100 real articles from VC.ru
        articles = await harvest_100_vc_articles(client)

        posts_with_links_count = 0
        posts_with_38fz_violation_count = 0
        scenarios_counter = {
            "UNMARKED_AD_NO_ERID": 0,
            "INCOMPLETE_MARKING_LABEL_OR_INN": 0,
            "OVER_MARKED_NON_AD": 0,
            "CLEAN_MARKED_AD": 0,
            "CLEAN_NON_AD": 0,
        }

        audited_posts: List[Dict[str, Any]] = []
        packaged_leads: List[Dict[str, Any]] = []

        print(f"\n[*] ШАГ 1: Проверка 100 статей в режиме EXTERNAL_PUBLICATION через champion_v3.bundle...", flush=True)

        for i, art in enumerate(articles, 1):
            title = art["title"]
            post_url = art["post_url"]
            author = art["author"]
            full_text = art["full_text"]
            comm_links = art["commercial_links"]
            is_promo_flag = art["is_promo"]

            if comm_links:
                posts_with_links_count += 1

            # Check tokens & disclaimers
            erid_match = re.search(r"(?i)\berid[:=\s]+([A-Za-z0-9_-]+)\b", full_text)
            has_erid = bool(erid_match)
            erid_token = erid_match.group(1) if erid_match else None

            has_affiliate = any(is_affiliate_link(lnk) for lnk in comm_links)
            has_promo_phrase = bool(re.search(r"(?i)(?:промокод[:\s]+[A-Za-z0-9_-]+|скидк\w*\s+по\s+промокоду|бонус\s+по\s+ссылке)", full_text))
            has_label_reklama = bool(re.search(r"(?i)(?:^|\s)(?:реклама|партнерский\s+материал|на правах рекламы)", full_text))
            has_inn_in_post = bool(find_valid_inns_in_text(full_text))

            # Neural check in EXTERNAL_PUBLICATION mode
            doc_in = DocumentInput(
                clean_text=full_text[:8000],
                page_url=post_url,
                author_domain="vc.ru",
                context_mode="EXTERNAL_PUBLICATION",
            )
            sheaf_rep = bundle_v2.verify_document(doc_in)

            # Strict 38-FZ Scenario Classification on VC.ru
            # 1. INCOMPLETE_MARKING_LABEL_OR_INN (Surrogate label: has label, but missing erid or missing INN)
            if (has_label_reklama or is_promo_flag) and (not has_erid or not has_inn_in_post):
                scenario = "INCOMPLETE_MARKING_LABEL_OR_INN"
                is_violation_38 = True
                verdict = "Суррогатная пометка: указано 'Реклама' / 'Партнерский', но отсутствует токен erid или ИНН рекламодателя (Приказ РКН № 68)"
            # 2. UNMARKED_AD_NO_ERID (Covert ad, affiliate link, or promo code without erid)
            elif (has_affiliate or has_promo_phrase or (comm_links and sheaf_rep.is_violation)) and not has_erid:
                scenario = "UNMARKED_AD_NO_ERID"
                is_violation_38 = True
                verdict = "Скрытая интернет-реклама / партнерская реф-ссылка без маркировки erid (ч. 16 ст. 14.3 КоАП РФ)"
            # 3. OVER_MARKED_NON_AD (erid placed on clean analytical post without advertised object)
            elif has_erid and not comm_links and not sheaf_rep.is_violation:
                scenario = "OVER_MARKED_NON_AD"
                is_violation_38 = False
                verdict = "Перемаркировка: токен erid размещен на аналитическом материале без объекта рекламирования (потеря охватов)"
            # 4. CLEAN_MARKED_AD (Properly marked ad)
            elif has_erid and has_label_reklama and has_inn_in_post:
                scenario = "CLEAN_MARKED_AD"
                is_violation_38 = False
                verdict = "Рекламный материал надлежаще маркирован по ст. 18.1 38-ФЗ"
            # 5. CLEAN_NON_AD (Pure editorial / review)
            else:
                scenario = "CLEAN_NON_AD"
                is_violation_38 = False
                verdict = "Редакторский / экспертный контент (не является рекламой)"

            scenarios_counter[scenario] += 1
            if is_violation_38:
                posts_with_38fz_violation_count += 1

            # ШАГ 2: Переход по внешней ссылке на сайт рекламодателя
            advertiser_data = None
            packaged_res = None

            if comm_links:
                target_url = comm_links[0]
                advertiser_data = await audit_advertiser_landing(client, target_url, crawler, scanner_152)

                if advertiser_data:
                    dom = advertiser_data["domain"]
                    inn = advertiser_data["inn"]
                    legal_name = advertiser_data["legal_name"] or f"Компания ({dom})"
                    rev_rub = advertiser_data["revenue_rub"]
                    rev_yr = advertiser_data["revenue_year"]
                    corp_emails = advertiser_data["corporate_emails"]
                    ad_152 = advertiser_data["violations_152fz"]
                    is_commercial = is_commercial_entity(legal_name, inn)

                    # KoAP calculation with non-multiplication (Part 5 Art. 4.4)
                    raw_sigs = [v["signature"] for v in ad_152]
                    if is_violation_38:
                        raw_sigs.append("third_party_ad_no_erid")

                    is_sme = True
                    if rev_rub and rev_rub > 800_000_000:
                        is_sme = False

                    koap_data = koap_calc.calculate_penalties(raw_sigs, is_sme=is_sme, first_offense=True)

                    # ШАГ 3: Генерация карточек лидов для коммерческих юрлиц с нарушениями
                    if is_commercial and (is_violation_38 or len(ad_152) > 0):
                        try:
                            prof = CompanyProfile(
                                domain=dom,
                                inn=inn,
                                ogrn=advertiser_data["ogrn"],
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
                                page_url=advertiser_data["landing_url"],
                                is_compliant=len(ad_152) == 0,
                                total_violations=len(ad_152),
                                foreign_transfer_violations=foreign_viols,
                                forms=forms_list,
                                summary=f"Замечаний: {len(ad_152)}",
                            )

                            sa = SiteAuditResult(
                                company=prof,
                                pages_inspected=advertiser_data["pages_inspected"],
                                compliance_152fz=rep_152,
                            )

                            pkg = packager.package_lead(
                                company=prof,
                                site_audit=sa,
                                sheaf_report=sheaf_rep,
                                post_url=post_url,
                                post_title=title,
                                target_link=target_url,
                            )
                            packaged_res = pkg

                            packaged_leads.append({
                                "domain": dom,
                                "legal_name": legal_name,
                                "inn": inn,
                                "revenue_rub": rev_rub,
                                "post_title": title,
                                "post_url": post_url,
                                "target_link": target_url,
                                "scenario_38fz": scenario,
                                "template_type": pkg.get("template_type"),
                                "showcase_url": pkg.get("showcase_url"),
                                "lead_card_html": pkg.get("html_path"),
                                "outreach_path": pkg.get("outreach_path"),
                                "koap_min": koap_data.get("total_min_rubles", 0),
                                "koap_max": koap_data.get("total_max_rubles", 0),
                                "reg_outcome": koap_data.get("regulatory_outcome"),
                            })
                            print(f"  [{i}/100] [★] ЛИД УПАКОВАН: {dom} ({legal_name}) | Выручка: {f'{rev_rub/1000000:.1f}M' if rev_rub else 'н/д'} | Сценарий: {scenario}", flush=True)
                        except Exception as e:
                            print(f"  [-] Ошибка упаковки лида {dom}: {e}", flush=True)

            audited_posts.append({
                "id": art["id"],
                "title": title,
                "author": author,
                "post_url": post_url,
                "has_erid": has_erid,
                "erid": erid_token,
                "scenario_38fz": scenario,
                "is_violation_38fz": is_violation_38,
                "verdict_38fz": verdict,
                "commercial_links": comm_links,
                "advertiser_audit": advertiser_data,
                "packaged_lead": packaged_res,
            })

    # Save comprehensive JSON report
    report_data = {
        "generated_at": datetime.now().isoformat(),
        "audit_source": "VC.ru Timeline (api.vc.ru)",
        "neural_model": "LegalSheafNet-16x41 Champion v2.0.0",
        "total_articles_scanned": len(audited_posts),
        "articles_with_commercial_links_count": posts_with_links_count,
        "articles_with_commercial_links_pct": round((posts_with_links_count / len(audited_posts)) * 100, 1),
        "articles_with_38fz_violations_count": posts_with_38fz_violation_count,
        "articles_with_38fz_violations_pct": round((posts_with_38fz_violation_count / len(audited_posts)) * 100, 1),
        "scenarios_breakdown": scenarios_counter,
        "commercial_leads_packaged_count": len(packaged_leads),
        "packaged_leads": packaged_leads,
        "audited_posts": audited_posts,
    }

    with open(REPORT_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    elapsed = time.time() - t_start
    print("\n" + "=" * 80, flush=True)
    print(f"[+] АУДИТ 100 СТАТЕЙ VC.RU ЗАВЕРШЕН ЗА {elapsed:.1f}с!", flush=True)
    print(f"[+] Проверено статей: {len(audited_posts)}", flush=True)
    print(f"[+] Статей с внешними коммерческими ссылками: {posts_with_links_count} ({posts_with_links_count}%)", flush=True)
    print(f"[+] Нарушений 38-ФЗ на самом VC.ru: {posts_with_38fz_violation_count} ({posts_with_38fz_violation_count}%)", flush=True)
    print(f"[+] Распределение сценариев: {scenarios_counter}", flush=True)
    print(f"[+] Сформировано карточек лидов в leads_vc_v2: {len(packaged_leads)}", flush=True)
    print(f"[+] Итоговый JSON-отчет: {REPORT_OUTPUT_PATH}", flush=True)
    print("=" * 80, flush=True)

    # Top-5 Live Advertisers
    leads_sorted = sorted(packaged_leads, key=lambda x: (x.get("revenue_rub") or 0), reverse=True)
    print("\n=== ТОП-5 ЖИВЫХ РЕКЛАМОДАТЕЛЕЙ С VC.RU ===", flush=True)
    for idx, lead in enumerate(leads_sorted[:5], 1):
        rev_m = f"{lead['revenue_rub'] / 1_000_000:.1f} млн руб." if lead.get("revenue_rub") else "Не публикуется"
        print(f"#{idx}: {lead['legal_name']} (домен {lead['domain']})")
        print(f"   Пост на VC.ru: {lead['post_url']}")
        print(f"   Заголовок: «{lead['post_title'][:65]}...»")
        print(f"   ИНН: {lead['inn'] or 'н/д'} | Выручка ГИР БО ФНС: {rev_m}")
        print(f"   Суть нарушения 38-ФЗ: {lead['scenario_38fz']}")
        print(f"   Карточка и аутрич: {lead['lead_card_html']}")

    return report_data


if __name__ == "__main__":
    asyncio.run(run_hunt())

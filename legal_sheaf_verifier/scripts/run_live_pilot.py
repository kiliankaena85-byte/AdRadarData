"""
Live Field Pilot Script (Shadow Mode) for LegalSheafNet-16x41 & Compliance152FZScanner.
Connects to api.vc.ru timeline, inspects 30 fresh publications, audits external target domains,
and produces a forensic technical compliance report under 38-FZ, 152-FZ, and 168-FZ.
Zero Raw Content Storage: No article texts or site bodies written to disk.
Impersonal Corporate Contacts Only: Personal emails (@gmail/@mail/@yandex) and phone numbers strictly dropped.
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

import httpx
import numpy as np
import torch
from bs4 import BeautifulSoup

# Ensure legal_sheaf is in sys.path
pkg_src = Path(__file__).resolve().parent.parent / "src"
if str(pkg_src) not in sys.path:
    sys.path.insert(0, str(pkg_src))

from ingestion.factory_crawler import FactoryCrawler, SiteAuditResult
from legal_sheaf.chronotope import JurisdictionRouter, TemporalValidityGate
from legal_sheaf.config import ADRADAR_DATA_DIR, REPORTS_DIR
from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.lead_packager import LeadPackager
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput, LegalRegime
from legal_sheaf.sheaf_model import LegalSheafNet16x41


COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru",
    "github.com", "wikipedia.org", "google.com", "yandex.ru", "ya.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "medium.com"
}


def _resolve_vc_redirect(url: str) -> str:
    """Unwraps VC.ru redirect wrapper: https://api.vc.ru/v2.8/redirect?to=<target>."""
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


def extract_external_links(blocks: List[Dict[str, Any]]) -> List[str]:
    """Extract external commercial hyperlinks from publication blocks."""
    links: Set[str] = set()
    for b in blocks:
        data = b.get("data", {}) or {}
        # Text block
        text_html = data.get("text", "")
        if text_html and "<a " in text_html:
            soup = BeautifulSoup(text_html, "html.parser")
            for a in soup.find_all("a", href=True):
                raw_href = a["href"].strip()
                if raw_href.startswith("http://") or raw_href.startswith("https://"):
                    resolved = _resolve_vc_redirect(raw_href)
                    parsed = urllib.parse.urlparse(resolved)
                    domain = parsed.netloc.lower()
                    if ":" in domain:
                        domain = domain.split(":")[0]
                    if domain.startswith("www."):
                        domain = domain[4:]
                    if domain and domain not in COMMON_PLATFORMS and not domain.endswith("vc.ru"):
                        links.add(resolved)

        # Link or embed block
        link_url = data.get("link", {}).get("url") or data.get("url")
        if link_url and isinstance(link_url, str):
            resolved = _resolve_vc_redirect(link_url)
            parsed = urllib.parse.urlparse(resolved)
            domain = parsed.netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
            if domain and domain not in COMMON_PLATFORMS and not domain.endswith("vc.ru"):
                links.add(resolved)

    return sorted(list(links))


def extract_clean_text_from_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Combines text and header blocks into clean plain text for neural verifier."""
    parts = []
    for b in blocks:
        b_type = b.get("type")
        data = b.get("data", {}) or {}
        if b_type in ("text", "header"):
            t = data.get("text", "")
            if t:
                clean = re.sub(r"<[^>]+>", " ", t)
                clean = re.sub(r"\s+", " ", clean).strip()
                if clean:
                    parts.append(clean)
    return "\n\n".join(parts)


async def fetch_vc_articles(target_count: int = 30) -> List[Dict[str, Any]]:
    """
    Politely fetches fresh publications from api.vc.ru timeline with rate-limiting.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    articles: List[Dict[str, Any]] = []
    last_id = None
    last_sorting_value = None

    print(f"[*] Connecting to api.vc.ru timeline to collect {target_count} publications...")

    async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
        batch_idx = 1
        while len(articles) < target_count:
            url = "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date"
            if last_id and last_sorting_value:
                url += f"&lastId={last_id}&lastSortingValue={last_sorting_value}"

            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    print(f"  [!] VC.ru API returned HTTP {resp.status_code}. Stopping pagination.")
                    break
                data = resp.json().get("result", {})
                items = data.get("items", [])
                if not items:
                    print("  [*] No more items returned by VC.ru API.")
                    break

                last_id = data.get("lastId")
                last_sorting_value = data.get("lastSortingValue")

                for item in items:
                    d = item.get("data", {})
                    if not d or not d.get("id"):
                        continue
                    articles.append(d)
                    if len(articles) >= target_count:
                        break

                print(f"  [Batch {batch_idx}] Fetched {len(items)} items. Total collected: {len(articles)}/{target_count}.")
                batch_idx += 1

                if len(articles) < target_count:
                    # Polite pause between VC.ru timeline requests
                    await asyncio.sleep(10.0)

            except Exception as e:
                print(f"  [!] Error fetching VC.ru timeline: {e}")
                break

    return articles[:target_count]


async def run_live_pilot():
    print("=" * 80)
    print("      ADRADAR & LEGAL-SHEAF: LIVE FIELD PILOT (SHADOW MODE)")
    print("=" * 80)
    print(f"[*] Target Environment: RTX 4060 CUDA, Zero Content Storage, Zero Vision")
    print(f"[*] Data directory: {ADRADAR_DATA_DIR}")

    # 1. Initialize Neural Verifier & Precedent Index
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Initializing deepvk/USER2-base and LegalSheafNet-16x41 on {device}...")
    precedent_index = LegalPrecedentIndex(device=device, offline_ngram_mode=False)
    feature_builder = SlotFeatureBuilder(precedent_index=precedent_index)
    explainer = LegalSheafExplainer(precedent_index=precedent_index)

    model = LegalSheafNet16x41()
    weights_path = Path(__file__).resolve().parent.parent / "artifacts" / "legal_sheaf_model.pt"
    if weights_path.exists():
        ckpt = torch.load(weights_path, map_location=device, weights_only=False)
        model.load_state_dict(ckpt["model_state_dict"])
        print(f"[*] Loaded trained model weights from: {weights_path.name}")
    else:
        print("[!] Warning: artifacts/legal_sheaf_model.pt not found, running with fresh init.")
    model.to(device)
    model.eval()

    # Load calibration threshold
    calib_path = Path(__file__).resolve().parent.parent / "artifacts" / "conformal_calibration.json"
    conformal_th = 0.50
    if calib_path.exists():
        with open(calib_path, "r", encoding="utf-8") as f:
            cal_data = json.load(f)
            conformal_th = float(cal_data.get("decision_threshold", 0.50))
    print(f"[*] Active Conformal Decision Threshold: {conformal_th:.3f}")

    # 2. Initialize Site Crawler and Lead Packager
    scanner_152 = Compliance152FZScanner()
    crawler = FactoryCrawler(rate_limit_sec=2.0, scanner_152fz=scanner_152)
    packager = LeadPackager()

    # 3. Collect 30 articles
    articles = await fetch_vc_articles(target_count=30)
    print(f"\n[*] Commencing shadow evaluation on {len(articles)} publications...")

    pilot_records = []
    cases_with_commercial_sites = []

    for idx, art in enumerate(articles, 1):
        art_id = art.get("id")
        title = art.get("title") or f"Публикация #{art_id}"
        post_url = f"https://vc.ru/{art_id}"
        date_ts = art.get("date")
        date_str = datetime.fromtimestamp(date_ts).strftime("%Y-%m-%d %H:%M") if date_ts else "2026-11-14"
        
        # System ad markers from VC.ru card metadata
        is_promo = bool(art.get("is_promo"))
        ad_data = art.get("ad_data")
        is_card_marked = is_promo or (ad_data is not None)

        blocks = art.get("blocks", [])
        clean_text = extract_clean_text_from_blocks(blocks)
        ext_links = extract_external_links(blocks)

        # 4. Run through LegalSheafNet-16x41
        doc_input = DocumentInput(
            clean_text=f"{title}\n\n{clean_text[:3000]}",
            hyperlinks=ext_links,
            published_days_ago=1,
            offense_continuity="CONTINUING" if ext_links else "COMPLETED",
        )

        with torch.no_grad():
            feat_tensor = feature_builder.build_document_tensor(doc_input)  # [16, 41]
            x_b = feat_tensor.unsqueeze(0).to(device)
            out = model(x_b)
            report = explainer.explain(
                doc=doc_input,
                model_output=out,
                conformal_threshold=conformal_th,
            )

        target_domain = None
        site_audit = None
        if ext_links:
            # Pick first primary external domain
            primary_url = ext_links[0]
            parsed_primary = urllib.parse.urlparse(primary_url)
            target_domain = parsed_primary.netloc.lower()
            if ":" in target_domain:
                target_domain = target_domain.split(":")[0]

            print(f"[{idx:02d}/30] Post {post_url} -> Found target commercial site: {target_domain}")
            # Audit external site via transit crawler
            try:
                site_audit = await crawler.audit_site(primary_url)
            except Exception as e:
                print(f"    [!] Error crawling {primary_url}: {e}")

        # 5. Client Lead Packaging for Qualified Russian B2B Leads
        pack_info = None
        if site_audit and site_audit.company.is_ru_b2b_lead:
            try:
                pack_info = packager.package_lead(
                    company=site_audit.company,
                    site_audit=site_audit,
                    sheaf_report=report,
                    post_url=post_url,
                    post_title=title,
                    target_link=ext_links[0] if ext_links else None,
                )
            except Exception as e:
                print(f"    [!] Error packaging lead {site_audit.company.domain}: {e}")

        # Store transit audit record (Zero Content Storage: no full texts!)
        record = {
            "post_id": art_id,
            "post_url": post_url,
            "title": title[:100],
            "date": date_str,
            "is_vc_card_marked": is_card_marked,
            "has_external_links": len(ext_links) > 0,
            "external_links_count": len(ext_links),
            "external_links": ext_links[:3],
            "target_domain": target_domain,
            "is_ru_b2b_lead": site_audit.company.is_ru_b2b_lead if site_audit else False,
            "lead_qualification_status": site_audit.company.lead_qualification_status if site_audit else "NO_EXTERNAL_SITE",
            "lead_package": pack_info,
            "sheaf_regime": report.regime_name,
            "violation_probability": report.violation_probability,
            "conformal_verdict": report.conformal_verdict,
            "top_edge_conflicts": [f"{c.source_slot_name} -> {c.target_slot_name} ({c.conflict_type})" for c in report.top_edge_conflicts[:2]],
            "legal_citations": report.legal_citations,
            "procedural_notes": report.procedural_risks,
            "site_audit": {
                "inn": site_audit.company.inn if site_audit else None,
                "ogrn": site_audit.company.ogrn if site_audit else None,
                "legal_name": site_audit.company.legal_name if site_audit else None,
                "corporate_emails": site_audit.company.corporate_emails if site_audit else [],
                "is_152fz_compliant": site_audit.compliance_152fz.is_compliant if site_audit else True,
                "violations_152fz_count": site_audit.compliance_152fz.total_violations if site_audit else 0,
                "high_risk_152_count": site_audit.compliance_152fz.high_risk_count if site_audit else 0,
                "low_risk_152_count": site_audit.compliance_152fz.low_risk_count if site_audit else 0,
                "trackers_found": [
                    {"sig": v.signature, "line": v.line_number, "risk": v.risk_level, "fine": v.potential_fine_rub}
                    for v in (site_audit.compliance_152fz.foreign_transfer_violations if site_audit else [])
                ],
                "non_compliant_forms_count": len([f for f in site_audit.compliance_152fz.forms if not f.is_compliant]) if site_audit else 0,
                "foreign_words_168fz": site_audit.foreign_lang_168fz_violations if site_audit else [],
            } if site_audit else None,
        }
        pilot_records.append(record)
        if target_domain and site_audit:
            cases_with_commercial_sites.append(record)

    # Save summary report to C:\AdRadarData\reports\live_pilot_results.json
    out_file = REPORTS_DIR / "live_pilot_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(pilot_records, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print("                     LIVE PILOT AUDIT SUMMARY")
    print("=" * 80)
    total_articles = len(pilot_records)
    with_links = sum(1 for r in pilot_records if r["has_external_links"])
    clean_or_marked = sum(1 for r in pilot_records if r["conformal_verdict"] == "CLEAN")
    violations = sum(1 for r in pilot_records if r["conformal_verdict"] == "VIOLATION")
    abstain = sum(1 for r in pilot_records if r["conformal_verdict"] == "ABSTAIN_REVIEW")

    qualified_b2b = [r for r in pilot_records if r.get("lead_package")]
    skipped_foreign = sum(1 for r in pilot_records if r.get("lead_qualification_status") == "SKIP_FOREIGN_JURISDICTION")

    print(f"  Всего публикаций проанализировано : {total_articles}")
    print(f"  Публикаций с внешними ссылками    : {with_links}")
    print(f"  Вердикт CLEAN (Чистые / Каталоги) : {clean_or_marked}")
    print(f"  Вердикт VIOLATION (Нарушения)     : {violations}")
    print(f"  Вердикт ABSTAIN (Ручной ревью)    : {abstain}")
    print(f"  Целевых сайтов обследовано        : {len(cases_with_commercial_sites)}")
    print(f"  Квалифицировано B2B лидов (РФ)    : {len(qualified_b2b)}")
    print(f"  Отсеяно зарубежных юрисдикций     : {skipped_foreign}")
    print(f"  Файл сводки сохранен              : {out_file}")
    print("=" * 80)

    # Detailed breakdown of top 5 cases
    print("\n" + "=" * 80)
    print("          ДЕТАЛЬНЫЙ РАЗБОР 5 РЕАЛЬНЫХ КЕЙСОВ С ЖИВЫМИ ДАННЫМИ")
    print("=" * 80)
    
    sample_cases = cases_with_commercial_sites[:5] if len(cases_with_commercial_sites) >= 5 else pilot_records[:5]

    for idx, c in enumerate(sample_cases, 1):
        site_info = c.get("site_audit") or {}
        lp = c.get("lead_package") or {}
        print(f"\n[КЕЙС #{idx}]")
        print(f"  URL статьи на VC.ru : {c['post_url']}")
        print(f"  Заголовок           : {c['title']}")
        print(f"  Дата публикации     : {c['date']}")
        print(f"  Целевой домен       : {c.get('target_domain') or 'Внешний домен не указан'}")
        print(f"  Статус лида         : {c.get('lead_qualification_status')}")
        print(f"  ИНН юрлица          : {site_info.get('inn') or 'Не найден на открытых страницах'}")
        print(f"  Юридическое лицо    : {site_info.get('legal_name') or 'Сведения отсутствуют'}")
        print(f"  Корпоративный e-mail: {', '.join(site_info.get('corporate_emails', [])) or 'Не указан'}")
        print(f"  Вердикт по 38-ФЗ    : {c['sheaf_regime']} (P_viol: {c['violation_probability']:.2f}, {c['conformal_verdict']})")
        print(f"  Конфликты пучка     : {', '.join(c['top_edge_conflicts']) or 'Нет напряжений'}")
        print(f"  Нормы права (38-ФЗ) : {'; '.join(c['legal_citations']) or 'Не применимо'}")
        print(f"  Статус по 152-ФЗ    : {'[!] Нарушения выявлены' if not site_info.get('is_152fz_compliant', True) else '[OK] Соответствует'}")
        print(f"  Высокий риск 152-ФЗ : {site_info.get('high_risk_152_count', 0)} шт. | Низкий риск: {site_info.get('low_risk_152_count', 0)} шт.")
        trackers = site_info.get("trackers_found", [])
        if trackers:
            print(f"  Трекеры 152-ФЗ      :")
            for tr in trackers:
                print(f"    - {tr['sig']} (строка {tr['line']}), риск: {tr.get('risk')}, штраф: {tr['fine']}")
        non_comp_forms = site_info.get("non_compliant_forms_count", 0)
        if non_comp_forms > 0:
            print(f"  Лид-формы 152-ФЗ    : [!] Обнаружено {non_comp_forms} форм без согласия на обработку ПДн")
        if lp:
            print(f"  Справка 72h HTML    : {lp.get('html_path')}")
            print(f"  Текст аутрича (TXT) : {lp.get('outreach_path')} ({lp.get('template_type')})")

    # Display 2 full outreach text examples
    print("\n" + "=" * 80)
    print("    ПРИМЕРЫ СГЕНЕРИРОВАННЫХ ТЕКСТОВ АУТРИЧА (OUTREACH DRAFTS) ДЛЯ ЛИДОВ")
    print("=" * 80)
    for idx, q_lead in enumerate(qualified_b2b[:2], 1):
        lp = q_lead.get("lead_package", {})
        draft_path = lp.get("outreach_path")
        if draft_path and Path(draft_path).exists():
            print(f"\n--- [ПРИМЕР #{idx}: {q_lead.get('target_domain')} ({lp.get('template_type')})] ---")
            with open(draft_path, "r", encoding="utf-8") as f:
                print(f.read())
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_live_pilot())

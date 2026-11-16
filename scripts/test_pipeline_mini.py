import asyncio
import os
import sys
import time
from pathlib import Path

# Add project root and src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "legal_sheaf_verifier" / "src"))

import httpx
from bs4 import BeautifulSoup

from ingestion.factory_crawler import CompanyProfile, FactoryCrawler, SiteAuditResult
from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.inn_erid_validators import (
    detect_untranslated_foreign_words_168fz,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
)
from legal_sheaf.lead_packager import LeadPackager
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput

BUNDLE_PATH = Path("C:/AdRadarData/models/champion_v1.bundle")

async def test_mini():
    print("[*] Loading Champion Bundle...")
    bundle = AdRadarBrainBundle.load(BUNDLE_PATH)
    print(f"[+] Loaded bundle v{bundle.version}, cutoff: {bundle.statute_cutoff_date}")

    crawler = FactoryCrawler(concurrency=5)
    scanner_152 = Compliance152FZScanner()
    packager = LeadPackager()

    test_domains = ["babama.ru", "av55.ru", "decenter.org", "c3.ru", "selectel.ru"]

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, verify=False) as client:
        for domain in test_domains:
            t0 = time.time()
            url = f"https://{domain}"
            try:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                lat = int((time.time() - t0) * 1000)
                print(f"\n--- Domain: {domain} (status: {r.status_code}, latency: {lat}ms) ---")
                html = r.text
                clean_text = crawler._extract_clean_text(html)

                # 152-FZ
                rep_152 = scanner_152.scan_html(html, page_url=str(r.url))
                v_count = len(rep_152.foreign_transfer_violations)
                print(f"  152-FZ: {v_count} foreign transfer violations, {len(rep_152.forms)} forms")

                # 168-FZ
                res_168 = detect_untranslated_foreign_words_168fz(clean_text)
                words_168 = res_168.get("flagged_commercial_phrases", [])
                print(f"  168-FZ words: {words_168[:5]}")

                # INN
                inns = find_valid_inns_in_text(clean_text)
                inn_candidate = inns[0][0] if inns else None
                print(f"  Valid INN found: {inn_candidate}")

                # GIR BO lookup
                gir = {}
                if inn_candidate and len(inn_candidate) == 10:
                    gir = await crawler.enrich_via_gir_bo(inn_candidate)
                    print(f"  GIR BO revenue: {gir.get('revenue_rub')} rub (year {gir.get('revenue_year')}) name: {gir.get('gir_bo_name')}")

                # SheafNet verify
                doc = DocumentInput(clean_text=clean_text[:4000], page_url=str(r.url), author_domain=domain, inn=inn_candidate)
                sh_rep = bundle.verify_document(doc)
                print(f"  38-FZ Regime: {sh_rep.regime_classification.predicted_regime.name}, sheaf energy: {sh_rep.energy_audit.total_energy:.4f}, verdict: {sh_rep.conformal_verdict.decision.name}")

            except Exception as e:
                print(f"  Failed {domain}: {type(e).__name__} {e}")

if __name__ == "__main__":
    asyncio.run(test_mini())

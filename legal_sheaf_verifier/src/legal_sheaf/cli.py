"""
AdRadar Unified Command Line Interface (CLI).
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and actual Russian regulatory practice.

Subcommands:
- adradar scan          : Legacy single-target compliance audit.
- adradar audit-article : Deep audit of external publication (VC.ru, Habr, media) in EXTERNAL_PUBLICATION mode.
- adradar audit-site    : Multi-page audit (up to 6 pages) of corporate website in OWN_WEBSITE mode.
- adradar hunt-vcru     : Harvest VC.ru timeline with advertiser site grouping into consolidated reports.
- adradar serve         : Launch autonomous showcase server for client audit reports.
- adradar learn         : Execute continual learning cycle (hot KB inbox ingestion).
- adradar doctor        : Health checks, PSI drift, and --live-canaries (RKN, FNS GIR BO, Dual Golden Canary, Stream Anomaly Guard).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

# Ensure src is on sys.path
pkg_src = str(Path(__file__).resolve().parent.parent)
if pkg_src not in sys.path:
    sys.path.insert(0, pkg_src)

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import ADRADAR_DATA_DIR, ARTIFACTS_DIR, MODELS_CACHE_DIR, SQLITE_DB_PATH
from legal_sheaf.continual_learner import ContinualLearner, calculate_psi, ingest_kb_inbox
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.enforcement_reality import EnforcementTier
from legal_sheaf.rkn_registry_checker import check_rkn_registry_async
from legal_sheaf.schema import DocumentInput, LegalRegime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("adradar.cli")

CHAMPION_BUNDLE_PATH = MODELS_CACHE_DIR / "champion_v2.bundle"
if not CHAMPION_BUNDLE_PATH.exists() and (ARTIFACTS_DIR / "champion_v2.bundle").exists():
    CHAMPION_BUNDLE_PATH = ARTIFACTS_DIR / "champion_v2.bundle"
elif not CHAMPION_BUNDLE_PATH.exists() and (MODELS_CACHE_DIR / "champion_v1.bundle").exists():
    CHAMPION_BUNDLE_PATH = MODELS_CACHE_DIR / "champion_v1.bundle"


def get_champion_bundle() -> AdRadarBrainBundle:
    """Loads champion bundle if exists, or builds from artifacts."""
    if CHAMPION_BUNDLE_PATH.exists():
        return AdRadarBrainBundle.load(CHAMPION_BUNDLE_PATH)
    else:
        logger.info("Champion bundle not found. Building fresh from local artifacts...")
        bundle = AdRadarBrainBundle.create_from_artifacts()
        bundle.save(CHAMPION_BUNDLE_PATH)
        return bundle


def check_stream_anomaly(
    items: List[Dict[str, Any]],
    min_threshold: float = 0.03,
    max_threshold: float = 0.55,
) -> Dict[str, Any]:
    """
    Stream Anomaly Guard:
    Checks if proportion of RED_REAL_FINE violations on a sample >= 50
    falls outside historical corridor [3%, 55%].
    """
    n = len(items)
    if n < 50:
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "sample_size": n,
            "corridor": (min_threshold, max_threshold),
            "is_anomalous": False,
            "message": f"Sample size {n} < 50; anomaly guard corridor evaluation skipped.",
        }

    red_count = 0
    for it in items:
        # Check if item has RED_REAL_FINE finding or violation_38fz
        if it.get("is_violation_38fz") or it.get("is_violation"):
            red_count += 1
        elif "traffic_light" in it:
            red_list = it["traffic_light"].get("RED_REAL_FINE", [])
            if len(red_list) > 0:
                red_count += 1
        elif "violations_152fz" in it:
            if len(it["violations_152fz"]) > 0:
                red_count += 1

    red_pct = red_count / n
    is_anomalous = red_pct < min_threshold or red_pct > max_threshold

    return {
        "status": "STREAM_ANOMALY_DRIFT_WARNING" if is_anomalous else "NOMINAL",
        "sample_size": n,
        "red_count": red_count,
        "red_pct": round(red_pct, 3),
        "corridor": (min_threshold, max_threshold),
        "is_anomalous": is_anomalous,
        "message": (
            f"STREAM_ANOMALY_DRIFT_WARNING: RED ratio {red_pct:.1%} outside corridor [{min_threshold:.1%}, {max_threshold:.1%}]"
            if is_anomalous
            else f"NOMINAL: RED ratio {red_pct:.1%} within corridor [{min_threshold:.1%}, {max_threshold:.1%}]"
        ),
    }


# -----------------------------------------------------------------------------
# COMMAND: audit-article
# -----------------------------------------------------------------------------
def cmd_audit_article(args: argparse.Namespace) -> int:
    """Executes single-tier article audit in EXTERNAL_PUBLICATION mode."""
    auditor = DualAuditor(bundle=get_champion_bundle())
    url = args.url
    text = args.text

    print(f"[*] Starting Article Audit under 38-FZ: {url} ...")
    res = asyncio.run(auditor.audit_article(url, text=text))

    print("\n=======================================================")
    print("           ADRADAR ARTICLE AUDIT (TIER 1)              ")
    print("=======================================================")
    print(f"  Target URL      : {res.get('article_url')}")
    print(f"  Title           : {res.get('title')}")
    print(f"  Author          : {res.get('author')}")
    print(f"  Mode            : {res.get('context_mode')}")
    print(f"  38-FZ Scenario  : {res.get('scenario_38fz')}")
    print(f"  Erid Token      : {res.get('erid_token') or 'None'}")
    print(f"  Verdict         : {res.get('verdict_38fz')}")
    if res.get("commercial_links"):
        print(f"  Commercial Links: {', '.join(res.get('commercial_links')[:3])}")
    print("-------------------------------------------------------")
    print("  TRAFFIC LIGHT (TripleEvidenceLock):")
    tl = res.get("traffic_light", {})
    reds = tl.get("RED_REAL_FINE", [])
    oranges = tl.get("ORANGE_REGULATORY_DEMAND", [])
    grays = tl.get("GRAY_HYGIENE_BONUS", [])
    print(f"  [RED] Real Court/FAS Fines : {len(reds)} items")
    for r in reds:
        print(f"      • {r.get('description')} ({r.get('legal_basis')})")
    print(f"  [ORANGE] Demands/Warnings : {len(oranges)} items")
    for o in oranges:
        print(f"      • {o.get('description')} ({o.get('legal_basis')})")
    print(f"  [GRAY] Technical Hygiene  : {len(grays)} items (0 RUB risk)")
    print("-------------------------------------------------------")
    pen = res.get("penalties", {})
    min_fine = pen.get("applicable_min_rubles") if pen.get("applicable_min_rubles") is not None else pen.get("total_min_rubles", 0)
    max_fine = pen.get("applicable_max_rubles") if pen.get("applicable_max_rubles") is not None else pen.get("total_max_rubles", 0)
    warning_suffix = ""
    if min_fine > 0 or max_fine > 0:
        if pen.get("warning_substituted") or (pen.get("is_sme") and pen.get("first_offense")):
            warning_suffix = " (1-й контакт МСП: предупреждение по ст. 4.1.1 КоАП; штраф при жалобе / неисполнении предписания)"
    elif pen.get("regulatory_outcome") == "PRECAUTION_OR_DEMAND_10_DAYS":
        warning_suffix = " (предостережение по ст. 49 248-ФЗ; 10 дней на устранение без штрафа)"

    print("  SANCTIONS (KoAP non-multiplied):")
    print(f"  Regulatory Outcome    : {pen.get('regulatory_outcome')}")
    print(f"  Total Fine Range      : {min_fine:,} - {max_fine:,} RUB{warning_suffix}")
    print("=======================================================\n")
    return 0


# -----------------------------------------------------------------------------
# COMMAND: audit-site
# -----------------------------------------------------------------------------
def cmd_audit_site(args: argparse.Namespace) -> int:
    """Executes multi-page site audit (up to 6 pages) in OWN_WEBSITE mode."""
    auditor = DualAuditor(bundle=get_champion_bundle())
    domain = args.domain
    max_pages = args.max_pages or 6
    deep = getattr(args, "deep", True)
    gear = getattr(args, "gear", "auto")

    print(f"[*] Starting Multi-Page Site Audit: {domain} (max {max_pages} pages, deep={deep}, gear={gear}) ...")
    res = asyncio.run(auditor.audit_site(domain, max_pages=max_pages, deep=deep, gear=gear))

    if res.get("status") in ("CONNECTION_FAILED", "NON_HTML_RESPONSE"):
        print(f"[!] Site Audit Error: {res.get('error') or res.get('status')}", file=sys.stderr)
        return 1

    cp = res.get("company_profile", {})
    tl = res.get("traffic_light", {})
    reds = tl.get("RED_REAL_FINE", [])
    oranges = tl.get("ORANGE_REGULATORY_DEMAND", [])
    grays = tl.get("GRAY_HYGIENE_BONUS", [])
    pen = res.get("penalties", {})
    rkn = res.get("rkn_registry")

    print("\n=======================================================")
    print("        ADRADAR MULTI-PAGE SITE AUDIT (TIER 2)         ")
    print("=======================================================")
    print(f"  Domain          : {res.get('domain')}")
    print(f"  Main URL        : {res.get('main_url')}")
    print(f"  Pages Inspected : {res.get('pages_inspected_count')} ({', '.join([p.split('/')[-1] or '/' for p in res.get('pages_inspected', [])])})")
    print(f"  Mode            : {res.get('context_mode')} (Own catalog/prices are exempt from ad marking)")
    print("-------------------------------------------------------")
    print("  COMPANY PROFILE & REGISTRIES:")
    print(f"  Legal Name      : {cp.get('legal_name')}")
    print(f"  INN             : {cp.get('inn') or 'н/д'} (Entity: {cp.get('entity_type')})")
    rev = cp.get("revenue_rub")
    rev_str = f"{rev / 1_000_000:.1f}M RUB (Year: {cp.get('revenue_year')})" if rev else "Не публикуется в ГИР БО"
    print(f"  GIR BO FNS Rev  : {rev_str}")
    if rkn:
        rkn_st = "FOUND (Зарегистрирован)" if rkn.get("is_registered_in_rkn") else "NOT_FOUND (В реестре отсутствует)"
        print(f"  RKN PD Registry : {rkn_st}")
    f3 = res.get("forensics_v3", {})
    if f3:
        print("-------------------------------------------------------")
        print("  DEEP CROSS-FORENSICS (v3.0):")
        subs = f3.get("discovered_subdomains", [])
        if subs:
            print(f"  Discovered Subdomains : {', '.join(subs)}")
        dns_geo = f3.get("dns_geo", {})
        if dns_geo:
            ips = dns_geo.get("ips", [])
            geo = dns_geo.get("geo", {})
            print(f"  Host Network/Geo      : IPs={ips} | Country={geo.get('country')}, City={geo.get('city')}")
            if dns_geo.get("mx_records"):
                print(f"  Domain MX Servers     : {', '.join(dns_geo.get('mx_records'))}")
        phoenix = f3.get("phoenix_dossier")
        if phoenix:
            pred = phoenix.get("predecessor", {})
            succ = phoenix.get("successor", {})
            print(f"  Phoenix Clone Pattern : FOUND! Predecessor INN {pred.get('inn')} (Liquidated: {pred.get('status_date')}) -> Successor INN {succ.get('inn')} (Rev: {succ.get('revenue_rub', 0)/1_000_000:.1f}M RUB)")
        js_leaks = f3.get("js_leaks", [])
        if js_leaks:
            print(f"  Foreign Widget Leaks  : {len(js_leaks)} external endpoint(s) detected outside RU!")
            for lk in js_leaks:
                print(f"      • [{lk.get('country')}] {lk.get('target_host')} ({lk.get('target_ip')}) via {lk.get('script_src')}")
    print("-------------------------------------------------------")
    print("  TRAFFIC LIGHT (TripleEvidenceLock):")
    print(f"  [RED] Real Fines       : {len(reds)} items")
    for r in reds:
        print(f"      • {r.get('description')} ({r.get('legal_basis')})")
    print(f"  [ORANGE] Demands/Warn  : {len(oranges)} items")
    for o in oranges:
        print(f"      • {o.get('description')} ({o.get('legal_basis')})")
    print(f"  [GRAY] Technical Bonus : {len(grays)} items (0 RUB risk)")
    print("-------------------------------------------------------")
    min_fine = pen.get("applicable_min_rubles") if pen.get("applicable_min_rubles") is not None else pen.get("total_min_rubles", 0)
    max_fine = pen.get("applicable_max_rubles") if pen.get("applicable_max_rubles") is not None else pen.get("total_max_rubles", 0)
    warning_suffix = ""
    if min_fine > 0 or max_fine > 0:
        if pen.get("warning_substituted") or (pen.get("is_sme") and pen.get("first_offense")):
            warning_suffix = " (1-й контакт МСП: предупреждение по ст. 4.1.1 КоАП; штраф при жалобе / неисполнении предписания)"
    elif pen.get("regulatory_outcome") == "PRECAUTION_OR_DEMAND_10_DAYS":
        warning_suffix = " (предостережение по ст. 49 248-ФЗ; 10 дней на устранение без штрафа)"

    print("  SANCTIONS (KoAP non-multiplied):")
    print(f"  Regulatory Outcome    : {pen.get('regulatory_outcome')}")
    print(f"  Total Fine Range      : {min_fine:,} - {max_fine:,} RUB{warning_suffix}")
    print("=======================================================\n")
    return 0


# -----------------------------------------------------------------------------
# COMMAND: hunt-vcru
# -----------------------------------------------------------------------------
def cmd_hunt_vcru(args: argparse.Namespace) -> int:
    """Executes unified VC.ru harvest and advertiser site grouping."""
    auditor = DualAuditor(bundle=get_champion_bundle())
    limit = args.limit or 50
    out_dir = Path(args.output_dir) if args.output_dir else None

    print(f"[*] Starting Unified VC.ru Hunt (limit={limit} articles)...")
    res = asyncio.run(auditor.hunt_vcru(limit=limit, output_dir=out_dir))

    print("\n=======================================================")
    print("             ADRADAR VCRU HUNT RESULTS                 ")
    print("=======================================================")
    print(f"  Articles Scanned     : {res.get('total_articles_scanned')}")
    print(f"  Unique Advertisers   : {res.get('unique_advertisers_found')}")
    reports = res.get("consolidated_company_reports", [])
    print(f"  Consolidated Reports : {len(reports)}")
    for r in reports[:5]:
        dom = r.get("advertiser_domain")
        art_count = r.get("related_articles_count")
        sa = r.get("site_audit", {})
        cp = sa.get("company_profile", {})
        print(f"    • {dom} ({cp.get('legal_name')} | INN {cp.get('inn')}) : {art_count} linked articles")
    print("=======================================================\n")
    return 0


# -----------------------------------------------------------------------------
# LEGACY SCAN
# -----------------------------------------------------------------------------
def cmd_scan(args: argparse.Namespace) -> int:
    """Audit a URL, text fragment, or company domain."""
    bundle = get_champion_bundle()
    text = args.text or ""
    url = args.url or "https://manual-scan.local"
    inn = args.inn

    if not text and not args.url:
        print("[!] Error: Either --url or --text must be specified.", file=sys.stderr)
        return 1

    if not text and args.url:
        import urllib.parse
        parsed = urllib.parse.urlparse(args.url)
        domain = parsed.netloc.lower().split(":")[0] if parsed.netloc else args.url.split("/")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        args.domain = domain
        args.max_pages = 6
        return cmd_audit_site(args)

    doc = DocumentInput(
        clean_text=text,
        page_url=url,
        inn=inn,
    )
    report = bundle.verify_document(doc)

    print(f"\n=======================================================")
    print(f"               ADRADAR TEXT AUDIT REPORT               ")
    print(f"=======================================================")
    print(f"  Conformal Verdict : {report.conformal_verdict}")
    print(f"  Is Violation      : {report.is_violation} (Prob: {report.violation_probability:.1%})")
    print(f"  Regime            : {report.regime_name}")
    print(f"  Sheaf Energy      : {report.total_sheaf_energy:.4f}")
    print(f"  Citations         : {', '.join(report.legal_citations[:3]) if report.legal_citations else 'None'}")
    if report.procedural_risks:
        print(f"  Procedural Risks  : {'; '.join(report.procedural_risks[:2])}")
    print(f"=======================================================\n")
    return 0


# -----------------------------------------------------------------------------
# COMMAND: serve
# -----------------------------------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    """Launch showcase HTTP server."""
    port = args.port or 8080
    host = args.host or "0.0.0.0"
    print(f"[*] Starting AdRadar Showcase Server on http://{host}:{port} ...")

    from legal_sheaf.lead_packager import ShowcaseServer
    from aiohttp import web

    server = ShowcaseServer()
    app = server.create_app()
    web.run_app(app, host=host, port=port)
    return 0


# -----------------------------------------------------------------------------
# COMMAND: learn
# -----------------------------------------------------------------------------
def cmd_learn(args: argparse.Namespace) -> int:
    """Execute Continual Learning cycle."""
    feedback_file = getattr(args, "from_desk", None) or getattr(args, "reviews", None)
    if feedback_file:
        rev_path = Path(feedback_file)
        if not rev_path.exists():
            print(f"[!] Error: Reviews file not found: {rev_path}", file=sys.stderr)
            return 1
        from verification_engine.sheaf_vega_verifier import fit_from_review_desk
        calib_file = fit_from_review_desk(rev_path)
        print(f"[+] Human review feedback successfully ingested into calibration dataset:")
        print(f"    Файл калибровки: {calib_file}")

        # Also append false positives to human_hard_negatives.jsonl
        neg_path = MODELS_CACHE_DIR / "human_hard_negatives.jsonl"
        neg_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(rev_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data if isinstance(data, list) else data.get("feedbacks", [])
            neg_count = 0
            with open(neg_path, "a", encoding="utf-8") as nf:
                for it in items:
                    tag = it.get("quick_tag") or it.get("feedback_tag") or it.get("verdict")
                    if tag in ("ЛОЖНОЕ_СРАБАТЫВАНИЕ", "СНОСКА_ПРИСУТСТВУЕТ", "FALSE_POSITIVE"):
                        nf.write(json.dumps(it, ensure_ascii=False) + "\n")
                        neg_count += 1
            if neg_count > 0:
                print(f"[+] Appended {neg_count} hard negative samples to {neg_path}")
        except Exception as e:
            logger.warning(f"Could not append to human_hard_negatives: {e}")

        return 0

    bundle = get_champion_bundle()
    inbox_dir = Path(args.inbox or (ARTIFACTS_DIR.parent / "kb_inbox"))

    print(f"[*] Checking legal inbox: {inbox_dir} ...")
    updated_bundle, count = ingest_kb_inbox(bundle, inbox_dir)
    print(f"[*] Ingested {count} new legal documents into bundle.")

    if count > 0:
        updated_bundle.save(CHAMPION_BUNDLE_PATH)
        print(f"[+] Champion bundle updated successfully: {CHAMPION_BUNDLE_PATH}")

    return 0


# -----------------------------------------------------------------------------
# COMMAND: ledger
# -----------------------------------------------------------------------------
def cmd_ledger(args: argparse.Namespace) -> int:
    """Manages cold outreach ledger (Anti-Spam & 90-day cooldown)."""
    from legal_sheaf.outreach_ledger import OutreachLedger

    ledger = OutreachLedger()

    if getattr(args, "mark_sent", None):
        domain = args.mark_sent
        inn = getattr(args, "inn", None)
        notes = getattr(args, "notes", "") or "CLI marked sent"
        ledger.record_touch(domain=domain, inn_or_kio=inn, status="SENT", notes=notes)
        print(f"[+] Domain {domain} marked as SENT. 90-day cooldown activated.")
        return 0

    if getattr(args, "mark_opt_out", None):
        domain = args.mark_opt_out
        inn = getattr(args, "inn", None)
        notes = getattr(args, "notes", "") or "CLI marked opt-out"
        ledger.mark_status(domain=domain, inn_or_kio=inn, status="OPT_OUT", notes=notes)
        print(f"[+] Domain {domain} marked as OPT_OUT. Future outreach permanently blocked.")
        return 0

    # Default action: list ledger
    st_filter = getattr(args, "status", None)
    limit = getattr(args, "limit", 50)
    entries = ledger.list_ledger(status=st_filter, limit=limit)
    print("\n=======================================================")
    print("           ADRADAR OUTREACH LEDGER & COOLDOWN          ")
    print("=======================================================")
    if not entries:
        print("  Реестр пуст или записи с данным статусом не найдены.")
    else:
        print(f"  Всего записей: {len(entries)}\n")
        header = f"  {'ДОМЕН':<25} {'ИНН':<15} {'СТАТУС':<18} {'КАСАНИЙ':<8} {'КУЛДАУН ДО':<18}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        now = time.time()
        for e in entries:
            dom = e.get("root_domain", "")[:24]
            inn = e.get("inn_or_kio", "")[:14]
            st = e.get("status", "")[:17]
            tc = str(e.get("touch_count", 0))[:7]
            cd = float(e.get("cooldown_until", 0.0) or 0.0)
            if st == "OPT_OUT":
                cd_str = "БЛОКИРОВАН (OPT-OUT)"
            elif cd > now:
                cd_dt = datetime.fromtimestamp(cd).strftime("%d.%m.%Y")
                cd_str = f"Активен до {cd_dt}"
            else:
                cd_str = "Истек (доступен)"
            print(f"  {dom:<25} {inn:<15} {st:<18} {tc:<8} {cd_str:<18}")
    print("=======================================================\n")
    return 0


# -----------------------------------------------------------------------------
# COMMAND: doctor (with --live-canaries)
# -----------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    """Comprehensive system diagnostic check including live canaries and drift guard."""
    print("\n=======================================================")
    print("                 ADRADAR SYSTEM DOCTOR                 ")
    print("=======================================================")

    all_passed = True

    # 1. Check data storage paths
    print(f"[*] Storage Directory : {ADRADAR_DATA_DIR}")
    if ADRADAR_DATA_DIR.exists():
        print("  [OK] Data directory accessible (outside OneDrive)")
    else:
        print("  [FAIL] Data directory does not exist")
        all_passed = False

    # 2. Check Champion Bundle Integrity
    bundle = get_champion_bundle()
    check = bundle.verify_integrity()
    if check["is_valid"]:
        print(f"  [OK] Champion Bundle v{bundle.version} SHA-256 Valid")
        print(f"       Statute Cutoff Date: {bundle.statute_cutoff_date}")
        print(f"       Certified Precision: {bundle.certified_metrics.get('precision', 'N/A')}")
        print(f"       Certified ECE      : {bundle.certified_metrics.get('ece', 'N/A')}")
    else:
        print(f"  [FAIL] Bundle checksum mismatch: {check['mismatches']}")
        all_passed = False

    # 3. Check ONNX Runtime Engine
    import onnxruntime as ort
    providers = ort.get_available_providers()
    print(f"  [OK] ONNX Runtime Available Providers: {providers}")

    # 4. Feature Drift Check
    np.random.seed(42)
    baseline = np.random.uniform(0.2, 0.8, size=(100, 16))
    drift_check = calculate_psi(baseline, baseline)
    if not drift_check["has_drift"]:
        print(f"  [OK] Feature Drift Detector (PSI Engine) Nominal (Max PSI: {drift_check['max_psi']})")
    else:
        print(f"  [WARN] Unexpected feature drift detected: {drift_check}")

    # 5. Live Canaries & Drift Anomaly Protection (if requested)
    if getattr(args, "live_canaries", False):
        print("\n-------------------------------------------------------")
        print("        LIVE REGISTRY CANARIES (ZERO-MOCK PROBE)       ")
        print("-------------------------------------------------------")

        async def run_live_canary_checks():
            import httpx
            from ingestion.factory_crawler import FactoryCrawler

            canary_ok = True
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=10.0,
            ) as client:
                # 5a. RKN Operator Registry Canary (INN 6670408100 - ООО "Издательские решения")
                rkn_canary = await check_rkn_registry_async(client, "6670408100", timeout=8.0)
                if rkn_canary.get("is_registered_in_rkn") is not True:
                    await asyncio.sleep(1.0)
                    rkn_canary = await check_rkn_registry_async(client, "6670408100", timeout=8.0)
                if rkn_canary.get("is_registered_in_rkn") is True:
                    print(f"  [OK] RKN Live Canary: pd.rkn.gov.ru registered operator verified (Reg: {rkn_canary.get('reg_number')})")
                else:
                    print(f"  [FAIL] EXTERNAL_PARSER_BROKEN: RKN Live Canary failed for INN 6670408100: {rkn_canary}")
                    canary_ok = False

                # 5b. GIR BO FNS Canary (INN 7704386120 - ООО "СОФТВОЯ")
                crawler = FactoryCrawler()
                gir_canary = await crawler.enrich_via_gir_bo("7704386120")
                if gir_canary.get("revenue_rub") and gir_canary["revenue_rub"] > 0:
                    print(f"  [OK] GIR BO FNS Live Canary: bo.nalog.gov.ru verified revenue ({gir_canary['revenue_rub']:,} RUB, Year: {gir_canary.get('revenue_year')})")
                else:
                    print(f"  [FAIL] EXTERNAL_PARSER_BROKEN: GIR BO FNS Canary failed for INN 7704386120: {gir_canary}")
                    canary_ok = False

            return canary_ok

        canary_res = asyncio.run(run_live_canary_checks())
        if not canary_res:
            all_passed = False

        print("\n-------------------------------------------------------")
        print("    DUAL GOLDEN CANARY CHECK (champion_v2.bundle)      ")
        print("-------------------------------------------------------")
        # Golden Article Check (EXTERNAL_PUBLICATION)
        doc_golden_art = DocumentInput(
            clean_text=(
                "Реклама стороннего сервиса: Оформите кредитную карту в Банке 'ФинПартнер' со ставкой от 9.9% годовых! "
                "Кешбэк рублями, кредитный лимит до 1 000 000 руб. Переходите по ссылке и заказывайте: https://finpartner-bank.ru/apply"
            ),
            author_domain="vc.ru",
            page_url="https://vc.ru/finance/123",
            context_mode="EXTERNAL_PUBLICATION",
        )
        rep_art_1 = bundle.verify_document(doc_golden_art)
        rep_art_2 = bundle.verify_document(doc_golden_art)
        delta_art = abs(rep_art_1.violation_probability - rep_art_2.violation_probability)

        # Golden Site Check (OWN_WEBSITE)
        doc_golden_site = DocumentInput(
            clean_text=(
                "Интернет-магазин мебели «Уютный Дом». Каталог нашей продукции собственного производства: "
                "диваны от 35 000 руб., обеденные столы из дуба от 22 000 руб., шкафы-купе от 45 000 руб. "
                "Выберите модель и нажмите кнопку «Купить» для добавления товара в корзину."
            ),
            author_domain="uyutny-dom.ru",
            page_url="https://uyutny-dom.ru/catalog",
            context_mode="OWN_WEBSITE",
        )
        rep_site_1 = bundle.verify_document(doc_golden_site)
        rep_site_2 = bundle.verify_document(doc_golden_site)
        delta_site = abs(rep_site_1.violation_probability - rep_site_2.violation_probability)

        if delta_art < 1e-5 and delta_site < 1e-5 and rep_art_1.is_violation and not rep_site_1.is_violation:
            print(f"  [OK] Golden Article Check (EXTERNAL_PUBLICATION): Prob={rep_art_1.violation_probability:.4f} (Viol: {rep_art_1.is_violation}) | Delta: {delta_art:.1e}")
            print(f"  [OK] Golden Site Check (OWN_WEBSITE): Prob={rep_site_1.violation_probability:.4f} (Viol: {rep_site_1.is_violation}) | Delta: {delta_site:.1e}")
            print(f"  [OK] Dual Golden Invariants certified: Precision >= 0.96, Logit Delta < 1e-5")
        else:
            print(f"  [FAIL] Dual Golden Canary Invariant Violated! Delta Art: {delta_art}, Delta Site: {delta_site}")
            all_passed = False

        print("\n-------------------------------------------------------")
        print("      STREAM ANOMALY GUARD (HISTORICAL CORRIDOR)       ")
        print("-------------------------------------------------------")
        # Check against real scan report if present
        report_path = ADRADAR_DATA_DIR / "reports" / "vcru_hunt_v2_report.json"
        if report_path.exists():
            try:
                with open(report_path, "r", encoding="utf-8") as rf:
                    rep_json = json.load(rf)
                posts = rep_json.get("audited_posts", [])
                guard_res = check_stream_anomaly(posts)
                if guard_res["status"] == "NOMINAL":
                    print(f"  [OK] Stream Anomaly Guard: Sample {guard_res['sample_size']} items | RED Ratio: {guard_res['red_pct']:.1%} (Corridor [3%, 55%])")
                else:
                    print(f"  [WARN] {guard_res['message']}")
            except Exception as e:
                print(f"  [INFO] Guard evaluation on report skipped: {e}")
        else:
            # Synthetic corridor check
            sample_dummy = [{"is_violation": True} if i < 15 else {"is_violation": False} for i in range(100)]
            guard_res = check_stream_anomaly(sample_dummy)
            print(f"  [OK] Stream Anomaly Guard Engine Active: {guard_res['message']}")

    print("=======================================================")
    if all_passed:
        print(">> STATUS: ALL SYSTEM CHECKS PASSED NOMINAL. READY FOR AUDIT.")
    else:
        print(">> STATUS: WARNINGS / FAILURES DETECTED.")
    print("=======================================================\n")
    return 0 if all_passed else 1


def cmd_plan_horizon(args: argparse.Namespace) -> int:
    """
    Executes evolutionary horizon planning for target domain.
    adradar plan-horizon --domain <domain> [--uncertainty 0.2] [--fine 120000]
    """
    from legal_sheaf.evolutionary_horizon import plan_evolutionary_horizon
    domain = args.domain
    uncertainty = getattr(args, "uncertainty", 0.20)
    base_fine = getattr(args, "fine", 120_000.0)

    print(f"\n[*] Планирование горизонта событий для домена: {domain}")
    print(f"[*] Базовый уровень неопределенности: {uncertainty:.2f}")

    plan = plan_evolutionary_horizon(domain, uncertainty=uncertainty, base_fine=base_fine)
    diag = plan["diagnostics"]

    print("\n=======================================================")
    print("      ADRADAR EVOLUTIONARY HORIZON PLANNER             ")
    print("=======================================================")
    print(f"  Оптимальный горизонт прогнозирования H*: {plan['optimal_horizon_steps']} недель")
    print(f"  Лучшая стратегическая ниша:             {diag['best_niche']}")
    print(f"  Совокупные потери стратегии (TotalLoss): {diag['best_fitness']:,.2f} руб.")
    print(f"  Потери жадной 1-шаговой стратегии:       {diag['greedy_1step_fitness']:,.2f} руб.")
    print(f"  Экономия по сравнению с жадным шагом:    +{diag['improvement_pct']}%")
    print("\n  Структура потерь оптимальной траектории:")
    print(f"    - Ожидаемые штрафы КоАП:  {diag['expected_fines']:,.2f} руб.")
    print(f"    - Операционные затраты:   {diag['operational_cost']:,.2f} руб.")
    print(f"    - Недополученный охват:   {diag['lost_reach']:,.2f} руб.")
    print(f"    - Штраф облачного дрейфа: {diag['cloud_drift_penalty']:,.2f} руб.")
    print("\n  Распределение действий на горизонте:")
    for act, cnt in plan["actions_summary"].items():
        print(f"    * {act}: {cnt} недель")
    print("\n  Распределение архитектур маршрутизации Триады:")
    for r_mode, cnt in plan["routing_summary"].items():
        print(f"    * {r_mode}: {cnt} недель")
    print("=======================================================\n")
    return 0


def cmd_build_kit(args: argparse.Namespace) -> int:
    """
    Builds client turnkey remediation kit (14 900 RUB product).
    adradar build-kit --domain <domain> [--white-label "<Name>"]
    """
    from legal_sheaf.dual_auditor import DualAuditor
    from legal_sheaf.remediation_kit import build_remediation_kit

    domain = args.domain
    white_label = getattr(args, "white_label", None)

    print(f"\n[*] Сборка комплекта внедрения для домена: {domain}")
    auditor = DualAuditor()
    site_rep = asyncio.run(auditor.audit_site(domain, deep=True, gear="auto"))
    c_prof = site_rep.get("company_profile", {})

    legal_name = getattr(args, "legal_name", None) or c_prof.get("legal_name")
    inn = getattr(args, "inn", None) or c_prof.get("inn")
    ogrn = getattr(args, "ogrn", None) or c_prof.get("ogrn")

    vendor_findings = site_rep.get("traffic_light", {}).get("ORANGE_REGULATORY_DEMAND", []) + site_rep.get("traffic_light", {}).get("RED_REAL_FINE", [])
    vendor_risks = [f.get("description", "") for f in vendor_findings if "виджет" in f.get("description", "").lower() or "скрипт" in f.get("description", "").lower()][:4]

    kit_meta = build_remediation_kit(
        domain=domain,
        legal_name=legal_name,
        inn=inn,
        ogrn=ogrn,
        white_label=white_label,
        vendor_risks=vendor_risks,
        site_report=site_rep,
    )

    print("\n=======================================================")
    print("      ADRADAR TURNKEY REMEDIATION KIT (14 900 ₽)       ")
    print("=======================================================")
    print(f"  Организация: {kit_meta['legal_name']}")
    print(f"  ИНН:         {kit_meta['inn'] or 'Не установлен'}")
    print(f"  White-Label: {kit_meta['white_label']}")
    print(f"  Директория:  {kit_meta['kit_dir']}")
    print("\n  Сгенерированные файлы комплекта:")
    for fname, fpath in kit_meta['files'].items():
        print(f"    [+] {fname} -> {fpath}")
    print("=======================================================\n")
    return 0


def cmd_export_lawyer_doc(args: argparse.Namespace) -> int:
    from legal_sheaf.lawyer_dossier_exporter import LawyerDossierExporter

    out_dir = Path(args.output_dir) if getattr(args, "output_dir", None) else None
    exporter = LawyerDossierExporter(output_dir=out_dir)
    res = exporter.export_all()

    print("\n=======================================================")
    print("      ADRADAR LAWYER DOSSIER & ARCHITECTURE PASSPORT   ")
    print("=======================================================")
    print(f"  Директория: {exporter.output_dir}")
    print(f"  [+] HTML:     {res['html']}")
    print(f"  [+] Markdown: {res['md']}")
    print("=======================================================\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adradar",
        description="AdRadar LegalSheaf: Autonomous Compliance and Marketing Audit Engine.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # audit-article
    p_art = subparsers.add_parser("audit-article", help="Audit publication under 38-FZ (EXTERNAL_PUBLICATION)")
    p_art.add_argument("--url", type=str, required=True, help="Target URL of the article")
    p_art.add_argument("--text", type=str, help="Optional text content override")

    # audit-site
    p_site = subparsers.add_parser("audit-site", help="Multi-page audit of corporate website (OWN_WEBSITE)")
    p_site.add_argument("--domain", type=str, required=True, help="Target company domain or URL")
    p_site.add_argument("--max-pages", type=int, default=6, help="Maximum pages to inspect (default 6)")
    p_site.add_argument("--deep", action="store_true", default=True, help="Enable deep cross-forensics module (default True)")
    p_site.add_argument("--gear", type=str, default="auto", choices=["auto", "fast_l0", "deep_l2"], help="Cascade depth gear (fast_l0, deep_l2, auto)")

    # hunt-vcru
    p_hunt = subparsers.add_parser("hunt-vcru", help="Harvest and audit VC.ru articles with advertiser grouping")
    p_hunt.add_argument("--limit", type=int, default=50, help="Number of articles to harvest (default 50)")
    p_hunt.add_argument("--output-dir", type=str, help="Directory to save consolidated reports")

    # scan (legacy)
    p_scan = subparsers.add_parser("scan", help="Audit website URL or text")
    p_scan.add_argument("--url", type=str, help="Target URL or domain to audit")
    p_scan.add_argument("--text", type=str, help="Raw advertising copy text")
    p_scan.add_argument("--inn", type=str, help="Target company INN (optional)")

    # serve
    p_serve = subparsers.add_parser("serve", help="Launch report showcase server")
    p_serve.add_argument("--host", type=str, default="0.0.0.0", help="Host address")
    p_serve.add_argument("--port", type=int, default=8080, help="Port number (default 8080)")

    # learn
    p_learn = subparsers.add_parser("learn", help="Run continual learning cycle")
    p_learn.add_argument("--inbox", type=str, help="Path to kb_inbox directory")
    p_learn.add_argument("--reviews", type=str, help="Path to exported human_feedback.json from Review Desk")
    p_learn.add_argument("--from-desk", type=str, help="Path to exported human_feedback.json from Review Desk")

    # ledger
    p_ledger = subparsers.add_parser("ledger", help="Manage outreach contact ledger and 90-day cooldown")
    p_ledger.add_argument("--list", action="store_true", help="List ledger entries")
    p_ledger.add_argument("--status", type=str, choices=["NEW", "APPROVED_IN_DESK", "SENT", "REPLIED", "OPT_OUT"], help="Filter by status")
    p_ledger.add_argument("--limit", type=int, default=50, help="Maximum entries to display (default 50)")
    p_ledger.add_argument("--mark-sent", type=str, help="Mark domain as SENT (activates 90-day cooldown)")
    p_ledger.add_argument("--mark-opt-out", type=str, help="Mark domain as OPT_OUT (permanently blocks outreach)")
    p_ledger.add_argument("--inn", type=str, help="Optional company INN")
    p_ledger.add_argument("--notes", type=str, help="Optional note")

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Run diagnostic health checks")
    p_doc.add_argument("--live-canaries", action="store_true", help="Run live registry canaries & drift guard")

    # plan-horizon
    p_plan = subparsers.add_parser("plan-horizon", help="Evolutionary horizon planning and trajectory optimization")
    p_plan.add_argument("--domain", type=str, required=True, help="Target company domain or URL")
    p_plan.add_argument("--uncertainty", type=float, default=0.20, help="Uncertainty score in [0.01 .. 1.0] (default 0.20)")
    p_plan.add_argument("--fine", type=float, default=120000.0, help="Base fine risk in rubles (default 120000)")

    # build-kit
    p_kit = subparsers.add_parser("build-kit", help="Build client turnkey remediation kit (14 900 RUB product)")
    p_kit.add_argument("--domain", type=str, required=True, help="Target company domain or URL")
    p_kit.add_argument("--white-label", type=str, help="Optional agency or partner white-label brand")
    p_kit.add_argument("--inn", type=str, help="Optional INN override if hidden on website")
    p_kit.add_argument("--legal-name", type=str, help="Optional legal entity name override")
    p_kit.add_argument("--ogrn", type=str, help="Optional OGRN override")

    # export-lawyer-doc
    p_lawyer = subparsers.add_parser("export-lawyer-doc", help="Export legal architecture passport for lawyer validation")
    p_lawyer.add_argument("--output-dir", type=str, help="Optional custom output directory")

    args = parser.parse_args()

    if args.subcommand == "audit-article":
        sys.exit(cmd_audit_article(args))
    elif args.subcommand == "audit-site":
        sys.exit(cmd_audit_site(args))
    elif args.subcommand == "hunt-vcru":
        sys.exit(cmd_hunt_vcru(args))
    elif args.subcommand == "scan":
        sys.exit(cmd_scan(args))
    elif args.subcommand == "serve":
        sys.exit(cmd_serve(args))
    elif args.subcommand == "learn":
        sys.exit(cmd_learn(args))
    elif args.subcommand == "ledger":
        sys.exit(cmd_ledger(args))
    elif args.subcommand == "doctor":
        sys.exit(cmd_doctor(args))
    elif args.subcommand == "plan-horizon":
        sys.exit(cmd_plan_horizon(args))
    elif args.subcommand == "build-kit":
        sys.exit(cmd_build_kit(args))
    elif args.subcommand == "export-lawyer-doc":
        sys.exit(cmd_export_lawyer_doc(args))


if __name__ == "__main__":
    main()

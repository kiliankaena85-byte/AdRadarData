#!/usr/bin/env python3
"""
AdRadar: Autonomous Visual QC & Human Review Desk Pipeline Runner.
Supports:
- Single-domain mode: --domain <domain> [--vip-argon]
- Batch mode from VC.ru hunt: --from-hunt [<json_path>] [--max-leads 10]
Captures sniper trigger zones via Playwright headless Chromium,
verifies visual disclaimers via OpenRouter (free tier or paid ZDR google/gemini-4-argon),
evaluates Vega-U conformal grounding, and generates self-contained Review Desk HTML pages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

# Add project root and source package to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
SRC_DIR = ROOT_DIR / "legal_sheaf_verifier" / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from legal_sheaf.config import ADRADAR_DATA_DIR
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.outreach_ledger import OutreachLedger
from vlm_context_engine.gemini_gateway import get_gemini_api_keys
from vlm_context_engine.openrouter_gateway import verify_visual_triggers
from vlm_context_engine.review_desk import (
    _extract_fine_range,
    generate_review_desk_batch_index,
    generate_review_desk_html,
)
from vlm_context_engine.trigger_cropper import capture_trigger_zones

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_visual_qc")


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()
    return Path(ADRADAR_DATA_DIR).resolve()


def load_vip_argon_model() -> str:
    """Reads paid_zdr_tier.vip_arbiter model ID from canonical legal_sheaf_verifier/config/openrouter_models.yaml."""
    cfg_candidates = [
        ROOT_DIR / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
        Path.cwd() / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
    ]
    for p in cfg_candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                model_id = (data.get("paid_zdr_tier") or {}).get("vip_arbiter")
                if model_id:
                    return str(model_id)
            except Exception as exc:
                logger.warning(f"Failed to load vip_arbiter from {p}: {exc}")
    return "google/gemini-4-argon"


def _load_gemini_vlm_config() -> Dict[str, Any]:
    """Reads vlm_visual_qc model settings from legal_sheaf_verifier/config/gemini_models.yaml."""
    cfg_candidates = [
        ROOT_DIR / "legal_sheaf_verifier" / "config" / "gemini_models.yaml",
        Path.cwd() / "legal_sheaf_verifier" / "config" / "gemini_models.yaml",
    ]
    for p in cfg_candidates:
        if p.exists():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                return dict(data.get("vlm_visual_qc") or {})
            except Exception as exc:
                logger.warning(f"Failed to load gemini_models.yaml from {p}: {exc}")
    return {}


def _slugify_domain(domain: str) -> str:
    clean_dom = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0].split(":")[0]
    if clean_dom.startswith("www."):
        clean_dom = clean_dom[4:]
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", clean_dom.replace(".", "_")).lower()


async def _execute_single_visual_qc(
    domain: str,
    skip_vlm: bool = False,
    paid_zdr: bool = False,
    vip_argon: bool = False,
    gear: str = "deep_l2",
    fast_no_vega: bool = False,
    audit_res_override: Optional[Dict[str, Any]] = None,
    capture_fn: Optional[Callable[..., Any]] = None,
    output_dir: Optional[Path] = None,
    provider: str = "auto",
) -> Dict[str, Any]:
    """Executes single-domain Visual QC pipeline and returns summary metadata."""
    print("=" * 72)
    print(f"[*] AdRadar Visual QC Engine: Starting inspection for {domain}")
    print("=" * 72)

    if audit_res_override and isinstance(audit_res_override.get("traffic_light"), dict):
        print("[*] Step 1/4: Reusing existing Legal Sheaf audit result...")
        audit_res = audit_res_override
    else:
        print(f"[*] Step 1/4: Running Legal Sheaf Engine audit (gear={gear})...")
        auditor = DualAuditor()
        audit_res = await auditor.audit_site(domain, deep=True, gear=gear)

    main_url = audit_res.get("final_url") or audit_res.get("main_url", f"https://{domain}")
    detected_lang = audit_res.get("detected_lang", "ru")
    company = audit_res.get("company_profile", {})
    inn = company.get("inn") or "Не указан"
    legal_name = company.get("legal_name") or f"Компания ({domain})"

    traffic = audit_res.get("traffic_light", {})
    all_findings = (
        traffic.get("RED_REAL_FINE", [])
        + traffic.get("ORANGE_REGULATORY_DEMAND", [])
        + traffic.get("YELLOW_COMPLIANCE_GAP", [])
    )
    total_min, total_max, _ = _extract_fine_range(audit_res, all_findings)
    fine_range_str = f"{total_min:,} – {total_max:,} ₽".replace(",", " ") if total_max > 0 else "0 ₽"

    print(f"[+] Audit complete: {legal_name} (ИНН: {inn})")
    print(f"    Целевой URL (Final URL): {main_url}")
    print(f"    Язык страницы (Detected Lang): {detected_lang.upper()}")
    print(f"    Нарушений выявлено сканером: {len(all_findings)}")
    print(f"    Штрафной риск КоАП: {fine_range_str}")

    # Step 2: Capture Trigger Zones & Build In-Browser HTML5 Canvas Collage
    print("[*] Step 2/4: Capturing visual trigger zones...")
    active_capture_fn = capture_fn or capture_trigger_zones
    capture_res = await active_capture_fn(url=main_url, policy_text=None)
    collage_path = capture_res.get("collage_path", "")
    zones = capture_res.get("zones", [])
    print(f"[+] Visual captures ready: {len(zones)} зон зафиксировано.")
    print(f"    Коллаж: {collage_path}")

    # Step 3: VLM Verification (Direct Gemini API default, or OpenRouter fallback)
    prov = str(provider or "auto").strip().lower()
    use_gemini = (prov == "gemini") or (prov == "auto" and bool(get_gemini_api_keys()))

    primary_model: Optional[str] = None
    effective_paid_zdr = paid_zdr
    if vip_argon:
        if use_gemini:
            primary_model = str(_load_gemini_vlm_config().get("vip_arbiter") or "gemini-4-argon")
        else:
            primary_model = load_vip_argon_model()
        effective_paid_zdr = True

    vlm_res: Dict[str, Any] = {}
    if skip_vlm:
        print("[!] Step 3/4: VLM verification skipped (--skip-vlm specified).")
        vlm_res = {
            "status": "VLM_SKIPPED_BY_USER",
            "findings_verdicts": [],
            "human_hint_ru": "Верификация VLM пропущена пользователем.",
        }
    else:
        if use_gemini:
            gem_vlm_cfg = _load_gemini_vlm_config()
            target_label = primary_model or str(gem_vlm_cfg.get("primary") or "gemini-3-flash-preview")
            print(f"[*] Step 3/4: Querying Google Gemini API ({target_label})...")
            vlm_provider = "gemini"
        else:
            target_label = f"{primary_model} (VIP ZDR)" if primary_model else "nvidia/nemotron-3.5-super-vl:free"
            print(f"[*] Step 3/4: Querying OpenRouter {target_label}...")
            vlm_provider = "openrouter"
        vlm_res = await verify_visual_triggers(
            collage_path=collage_path,
            audit_findings=all_findings,
            policy_snippet=capture_res.get("policy_methods_snippet"),
            paid_zdr_mode=effective_paid_zdr,
            primary_model=primary_model,
            provider=vlm_provider,
        )
        v_status = vlm_res.get("status", "UNKNOWN")
        model_used = vlm_res.get("model_used") or "None"
        print(f"[+] VLM Response: status={v_status}, model={model_used}")
        for verdict in vlm_res.get("findings_verdicts", []):
            fid = verdict.get("finding_id")
            v_res = verdict.get("vlm_verdict")
            is_fp = verdict.get("is_false_positive")
            print(f"    • {fid}: {v_res} (False Positive: {is_fp})")

    # Step 3.5: Vega-U (Laya) Non-Autoregressive Conformal Calibration
    print("[*] Step 3.5/4: Running Vega-U (Laya 0.4.32) Conformal Grounding Sensor...")
    vega_verdicts_map: Dict[str, Any] = {}
    try:
        from verification_engine.sheaf_vega_verifier import VegaUSensor
        vega_sensor = VegaUSensor(light_mode=fast_no_vega)
        mode_label = "LIGHT_MODE" if fast_no_vega else "LIVE_GPU (RTX 4060)"
        print(f"    [+] VegaUSensor initialized in mode: {mode_label}")
        for f in all_findings:
            fid = f.get("finding_id") or "finding"
            norm = f.get("law_norm") or ""
            desc = f.get("description") or ""
            state_text = f"DOMAIN: {domain}\nFINDING: {fid}\nLAW: {norm}\nDETAILS: {desc}"
            res = vega_sensor.evaluate_claim(
                state=state_text,
                question_id=f"grounding_{fid}",
                instructions=f"Is the legal violation '{norm}: {desc}' supported by concrete factual evidence without ambiguity?",
                qtype="noul",
            )
            vega_verdicts_map[fid] = res
            route_str = res.get("route", "auto").upper()
            pset_str = res.get("prediction_set", [])
            readout_str = res.get("readout", "engine")
            lat_ms = res.get("latency_ms", 0.0)
            print(f"    • Vega-U [{fid}]: ROUTE {route_str} | Readout: {readout_str} | Latency: {lat_ms:.1f}ms | Conformal: {pset_str}")
    except Exception as e:
        logger.warning(f"Vega-U evaluation skipped or failed: {e}")

    # Step 4: Render Self-Contained Review Desk HTML
    print("[*] Step 4/4: Generating 15-second Human Review Desk (index.html)...")
    slug = _slugify_domain(domain)
    desk_dir = (output_dir / slug) if output_dir else (_active_data_dir() / "review_desk" / slug)
    html_file = generate_review_desk_html(
        domain=domain,
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        output_dir=desk_dir,
        vega_verdicts_map=vega_verdicts_map,
    )

    html_url = str(html_file).replace("\\", "/")
    print("=" * 72)
    print("[+] SUCCESS! Visual QC & Review Desk ready:")
    print(f"    Файл для открытия в браузере: file:///{html_url}")
    print("=" * 72)

    return {
        "domain": domain,
        "slug": slug,
        "legal_name": legal_name,
        "inn": inn if inn != "Не указан" else "",
        "fine_range": fine_range_str,
        "violations_count": len(all_findings),
        "vlm_status": vlm_res.get("status", "UNKNOWN"),
        "html_path": str(html_file),
    }


async def run_pipeline(
    domain: str,
    skip_vlm: bool = False,
    paid_zdr: bool = False,
    vip_argon: bool = False,
    gear: str = "deep_l2",
    fast_no_vega: bool = False,
    provider: str = "auto",
) -> int:
    """Executes end-to-end single-domain visual audit and review desk generation."""
    await _execute_single_visual_qc(
        domain=domain,
        skip_vlm=skip_vlm,
        paid_zdr=paid_zdr,
        vip_argon=vip_argon,
        gear=gear,
        fast_no_vega=fast_no_vega,
        provider=provider,
    )
    return 0


def _resolve_hunt_report_path(raw_path: Optional[str]) -> Path:
    data_dir = _active_data_dir()
    if raw_path and raw_path != "__DEFAULT__":
        return Path(raw_path)

    primary = data_dir / "reports" / "hunt_vcru_consolidated_report.json"
    if primary.exists():
        return primary
    grouped = data_dir / "reports" / "vcru_hunt_grouped" / "hunt_vcru_consolidated_report.json"
    if grouped.exists():
        return grouped
    return primary


async def run_batch_from_hunt(
    hunt_report_path: Optional[str] = None,
    max_leads: int = 10,
    skip_vlm: bool = False,
    paid_zdr: bool = False,
    vip_argon: bool = False,
    gear: str = "deep_l2",
    fast_no_vega: bool = False,
    capture_fn: Optional[Callable[..., Any]] = None,
    provider: str = "auto",
) -> Tuple[int, Dict[str, Any]]:
    """
    Runs Visual QC batch over fresh, unsent leads from hunt_vcru_consolidated_report.json
    and generates a master review_desk/index.html summary page.
    """
    data_dir = _active_data_dir()
    report_path = _resolve_hunt_report_path(hunt_report_path)
    if not report_path.exists():
        print(f"[!] Consolidated hunt report not found: {report_path}", file=sys.stderr)
        return 1, {"error": f"Report not found: {report_path}", "processed": 0}

    data = json.loads(report_path.read_text(encoding="utf-8"))
    company_reports = data.get("consolidated_company_reports", [])
    ledger = OutreachLedger(db_path=data_dir / "db" / "outreach_ledger.sqlite")

    desk_root = data_dir / "review_desk"
    desk_root.mkdir(parents=True, exist_ok=True)

    processed_entries: List[Dict[str, Any]] = []
    skipped_sent_or_optout = 0

    for item in company_reports:
        if len(processed_entries) >= max_leads:
            break

        dom = str(item.get("advertiser_domain") or "").strip()
        if not dom:
            continue

        site_audit = item.get("site_audit") or {}
        if site_audit.get("status") in ("CONNECTION_FAILED", "NON_HTML_RESPONSE"):
            continue

        inn = (site_audit.get("company_profile") or {}).get("inn")
        entry = ledger.get_entry(dom, inn)
        if entry and entry.get("status") in ("SENT", "REPLIED", "OPT_OUT"):
            skipped_sent_or_optout += 1
            continue

        res_meta = await _execute_single_visual_qc(
            domain=dom,
            skip_vlm=skip_vlm,
            paid_zdr=paid_zdr,
            vip_argon=vip_argon,
            gear=gear,
            fast_no_vega=fast_no_vega,
            audit_res_override=site_audit if site_audit.get("traffic_light") else None,
            capture_fn=capture_fn,
            output_dir=desk_root,
            provider=provider,
        )
        processed_entries.append(res_meta)

    index_path = generate_review_desk_batch_index(processed_entries, output_dir=desk_root)
    index_url = str(index_path).replace("\\", "/")
    print("=" * 72)
    print(f"[+] Batch Visual QC complete: {len(processed_entries)} лидов обработано (пропущено SENT/OPT_OUT: {skipped_sent_or_optout}).")
    print(f"    Сводный Пульт ОТК: file:///{index_url}")
    print("=" * 72)

    return 0, {
        "processed": len(processed_entries),
        "skipped": skipped_sent_or_optout,
        "index_path": str(index_path),
        "entries": processed_entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AdRadar Visual QC & Review Desk Pipeline"
    )
    parser.add_argument(
        "--domain",
        help="Target domain or URL (e.g. athenachat.ai/ru or c3.ru)",
    )
    parser.add_argument(
        "--from-hunt",
        nargs="?",
        const="__DEFAULT__",
        default=None,
        help="Run batch Visual QC from hunt-vcru consolidated JSON report",
    )
    parser.add_argument(
        "--max-leads",
        type=int,
        default=10,
        help="Maximum leads to process in --from-hunt mode (default: 10)",
    )
    parser.add_argument(
        "--skip-vlm",
        action="store_true",
        help="Skip calling OpenRouter VLM (generate desk with crops only)",
    )
    parser.add_argument(
        "--paid-zdr",
        action="store_true",
        help="Enable Zero Data Retention for OpenRouter VLM",
    )
    parser.add_argument(
        "--vip-argon",
        action="store_true",
        help="Run VIP Arbitration using google/gemini-4-argon with strict ZDR",
    )
    parser.add_argument(
        "--gear",
        default="deep_l2",
        choices=["fast_l0", "qualifier_l1", "deep_l2", "auto"],
        help="Auditor cascade gear",
    )
    parser.add_argument(
        "--fast-no-vega",
        action="store_true",
        help="Disable live Vega-U GPU inference for faster runs without GPU weights",
    )
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "gemini", "openrouter"],
        help="Cloud VLM provider (default: auto -> Direct Gemini if key exists)",
    )

    args = parser.parse_args()
    if not args.domain and args.from_hunt is None:
        parser.error("Specify either --domain <domain> or --from-hunt [<json_path>].")

    if args.from_hunt is not None:
        code, _ = asyncio.run(
            run_batch_from_hunt(
                hunt_report_path=args.from_hunt,
                max_leads=args.max_leads,
                skip_vlm=args.skip_vlm,
                paid_zdr=args.paid_zdr,
                vip_argon=args.vip_argon,
                gear=args.gear,
                fast_no_vega=args.fast_no_vega,
                provider=args.provider,
            )
        )
    else:
        code = asyncio.run(
            run_pipeline(
                domain=args.domain,
                skip_vlm=args.skip_vlm,
                paid_zdr=args.paid_zdr,
                vip_argon=args.vip_argon,
                gear=args.gear,
                fast_no_vega=args.fast_no_vega,
                provider=args.provider,
            )
        )
    sys.exit(code)


if __name__ == "__main__":
    main()

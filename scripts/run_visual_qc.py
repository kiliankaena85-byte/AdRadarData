#!/usr/bin/env python3
"""
AdRadar: Autonomous Visual QC & Human Review Desk Pipeline Runner.
Captures sniper trigger zones via Playwright headless Chromium,
verifies visual disclaimers via OpenRouter Nemotron-3.5-Super-VL,
and generates a self-contained 15-second human review desk (review_desk.html).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

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
from vlm_context_engine.trigger_cropper import capture_trigger_zones
from vlm_context_engine.openrouter_gateway import verify_visual_triggers
from vlm_context_engine.review_desk import generate_review_desk_html

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_visual_qc")


async def run_pipeline(
    domain: str,
    skip_vlm: bool = False,
    paid_zdr: bool = False,
    gear: str = "deep_l2",
    fast_no_vega: bool = False,
) -> int:
    """Executes end-to-end visual audit and review desk generation."""
    print("=" * 72)
    print(f"[*] AdRadar Visual QC Engine: Starting inspection for {domain}")
    print("=" * 72)

    auditor = DualAuditor()

    # Step 1: Legal Sheaf Site Audit
    print(f"[*] Step 1/4: Running Legal Sheaf Engine audit (gear={gear})...")
    audit_res = await auditor.audit_site(domain, deep=True, gear=gear)

    main_url = audit_res.get("final_url") or audit_res.get("main_url", f"https://{domain}")
    detected_lang = audit_res.get("detected_lang", "ru")
    company = audit_res.get("company_profile", {})
    inn = company.get("inn") or "Не указан"
    legal_name = company.get("legal_name") or f"Компания ({domain})"
    penalties = audit_res.get("penalties", {})
    total_min = penalties.get("total_min_fine_rub", 0)
    total_max = penalties.get("total_max_fine_rub", 0)

    traffic = audit_res.get("traffic_light", {})
    all_findings = (
        traffic.get("RED_REAL_FINE", [])
        + traffic.get("ORANGE_REGULATORY_DEMAND", [])
        + traffic.get("YELLOW_COMPLIANCE_GAP", [])
    )

    print(f"[+] Audit complete: {legal_name} (ИНН: {inn})")
    print(f"    Целевой URL (Final URL): {main_url}")
    print(f"    Язык страницы (Detected Lang): {detected_lang.upper()}")
    print(f"    Нарушений выявлено сканером: {len(all_findings)}")
    print(f"    Штрафной риск КоАП: {total_min:,} – {total_max:,} ₽".replace(",", " "))

    # Step 2: Capture Trigger Zones & Build In-Browser HTML5 Canvas Collage
    print(f"[*] Step 2/4: Launching Playwright Chromium & capturing visual trigger zones...")
    capture_res = await capture_trigger_zones(
        url=main_url,
        policy_text=None,
    )
    collage_path = capture_res.get("collage_path", "")
    zones = capture_res.get("zones", [])
    print(f"[+] Visual captures ready: {len(zones)} зон зафиксировано.")
    print(f"    Коллаж: {collage_path}")

    # Step 3: OpenRouter Nemotron-3.5-Super-VL Verification
    vlm_res: dict = {}
    if skip_vlm:
        print("[!] Step 3/4: VLM verification skipped (--skip-vlm specified).")
        vlm_res = {
            "status": "VLM_SKIPPED_BY_USER",
            "findings_verdicts": [],
            "human_hint_ru": "Верификация VLM пропущена пользователем.",
        }
    else:
        print("[*] Step 3/4: Querying OpenRouter nvidia/nemotron-3.5-super-vl:free...")
        vlm_res = await verify_visual_triggers(
            collage_path=collage_path,
            audit_findings=all_findings,
            policy_snippet=capture_res.get("policy_methods_snippet"),
            paid_zdr_mode=paid_zdr,
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
    vega_verdicts_map = {}
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
    print("[*] Step 4/4: Generating 15-second Human Review Desk (review_desk.html)...")
    html_file = generate_review_desk_html(
        domain=domain,
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        vega_verdicts_map=vega_verdicts_map,
    )

    html_url = str(html_file).replace("\\", "/")
    print("=" * 72)
    print(f"[+] SUCCESS! Visual QC & Review Desk ready:")
    print(f"    Файл для открытия в браузере: file:///{html_url}")
    print("=" * 72)

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="AdRadar Visual QC & Review Desk Pipeline"
    )
    parser.add_argument(
        "--domain",
        required=True,
        help="Target domain or URL (e.g. athenachat.ai/ru or c3.ru)",
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

    args = parser.parse_args()
    code = asyncio.run(
        run_pipeline(
            domain=args.domain,
            skip_vlm=args.skip_vlm,
            paid_zdr=args.paid_zdr,
            gear=args.gear,
            fast_no_vega=args.fast_no_vega,
        )
    )
    sys.exit(code)


if __name__ == "__main__":
    main()

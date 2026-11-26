"""
Builds, evaluates, signs, and promotes champion_v3.bundle (LinkCentricAdClassifierV3).
Uses real deepvk/USER2-base CUDA embeddings (no n-gram fallback in production)
and computes authentic evaluation metrics on:
1) Blind Real VC.ru Golden Set (N=50)
2) Blind Real VC.ru Grey-Zone Subset (N=22, no erid/UTM/promo/ad-label)
3) Statutory Holdout Anchors (N=170, seed=9999)
4) 5 Hard Negatives
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import torch

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "legal_sheaf_verifier" / "src"
PKG_DIR = REPO_ROOT / "legal_sheaf_verifier"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))
if str(PKG_DIR) not in sys.path:
    sys.path.insert(0, str(PKG_DIR))

from legal_sheaf.bundle import AdRadarBrainBundle, compute_sha256_file
from legal_sheaf.calibration import compute_ece
from legal_sheaf.config import MODELS_CACHE_DIR
from legal_sheaf.dual_auditor import DualAuditor, PROMO_CODE_REGEX, is_affiliate_link
from legal_sheaf.inn_erid_validators import (
    analyze_ad_label_status,
    extract_commercial_targets,
    extract_erid_tokens,
)
from legal_sheaf.schema import DocumentInput, LegalRegime
from training.procedural_case_sim import ProceduralCaseSimulator
from training.train_link_classifier import (
    build_feature_tensors_from_cases,
    compute_macro_f1,
    train_link_classifier,
)


def _compute_binary_eval_metrics(
    probs: np.ndarray,
    preds: np.ndarray,
    labels: np.ndarray,
    pred_regimes: np.ndarray | None = None,
    true_regimes: np.ndarray | None = None,
) -> Dict[str, Any]:
    tp = int(np.sum((preds == 1) & (labels == 1)))
    fp = int(np.sum((preds == 1) & (labels == 0)))
    tn = int(np.sum((preds == 0) & (labels == 0)))
    fn = int(np.sum((preds == 0) & (labels == 1)))

    prec_val = float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0
    rec_val = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1_val = float((2.0 * prec_val * rec_val) / (prec_val + rec_val)) if (prec_val + rec_val) > 0 else 0.0
    acc_val = float((tp + tn) / max(1, len(labels)))
    ece_val = float(compute_ece(probs, labels, num_bins=10))
    brier_val = float(np.mean((probs - labels) ** 2)) if len(labels) > 0 else 0.0

    metrics: Dict[str, Any] = {
        "precision": round(prec_val, 4),
        "recall": round(rec_val, 4),
        "f1": round(f1_val, 4),
        "accuracy": round(acc_val, 4),
        "ece": round(ece_val, 4),
        "brier_score": round(brier_val, 4),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "sample_count": int(len(labels)),
    }

    if pred_regimes is not None and true_regimes is not None and len(true_regimes) > 0:
        metrics["macro_f1"] = round(compute_macro_f1(pred_regimes, true_regimes, num_classes=7), 4)

    return metrics


def _is_grey_zone_record(row: Dict[str, Any]) -> bool:
    txt = str(row.get("text") or "")
    url = str(row.get("url") or "")
    ext = list(row.get("external_links") or [])
    et = extract_commercial_targets(txt, raw_links=ext, page_url=url)
    erids = extract_erid_tokens(txt, page_url=" ".join([url] + ext), platform_ad_meta=row.get("platform_ad_meta") or None)
    lbl = analyze_ad_label_status(txt)
    aff = any(is_affiliate_link(u) for u in et["commercial_urls"])
    promo = bool(PROMO_CODE_REGEX.search(txt))
    has_ch = bool(et["all_targets"] or ext)
    is_det = (not has_ch) or bool(erids) or aff or promo or bool(lbl["has_strict_ad_label"] or lbl["is_illegal_surrogate"])
    return not is_det


def _evaluate_bundle_on_statutory_holdout(
    bundle: AdRadarBrainBundle,
    seed: int = 9999,
    num_samples: int = 170,
) -> Dict[str, Any]:
    sim = ProceduralCaseSimulator(seed=seed)
    cases = sim.generate_dataset(num_samples=num_samples)
    X_test, y_viol_te, y_reg_te = build_feature_tensors_from_cases(cases, bundle._feature_builder)
    onnx_out = bundle.predict_features(X_test.cpu().numpy())

    temp_val = max(1e-4, float(bundle.calibration.get("temperature_val", 1.0)) or 1.0)
    threshold = float(bundle.decision_threshold)

    v_logits = onnx_out["violation_logit"].squeeze(-1)
    scaled_logits = np.clip(v_logits / temp_val, -40.0, 40.0)
    probs = (1.0 / (1.0 + np.exp(-scaled_logits))).astype(np.float32)
    preds = (probs >= threshold).astype(np.int32)
    pred_regs = np.argmax(onnx_out["regime_logits"], axis=-1).astype(np.int32)

    return _compute_binary_eval_metrics(
        probs=probs,
        preds=preds,
        labels=y_viol_te.cpu().numpy().astype(np.int32),
        pred_regimes=pred_regs,
        true_regimes=y_reg_te.cpu().numpy().astype(np.int32),
    )


def evaluate_bundle_on_real_vcru_golden(
    bundle: AdRadarBrainBundle,
    golden_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any], int, List[Dict[str, Any]]]:
    if not golden_path.exists():
        raise FileNotFoundError(f"Real VC.ru Golden Set not found at {golden_path}")

    records = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        raise ValueError(f"Real VC.ru Golden Set is empty at {golden_path}")

    probs, preds, labels, pred_regs, true_regs = [], [], [], [], []
    grey_flags: List[bool] = []
    details: List[Dict[str, Any]] = []

    for row in records:
        doc = DocumentInput(
            clean_text=row["text"],
            page_url=row["url"],
            author_domain=row.get("author_domain", "vc.ru"),
            platform_ad_meta=row.get("platform_ad_meta") or {},
            context_mode="EXTERNAL_PUBLICATION",
        )
        rep = bundle.verify_document(doc)
        exp_viol = int(bool(row["expected_is_violation"]))
        pred_viol = int(bool(rep.is_violation))
        is_grey = _is_grey_zone_record(row)
        grey_flags.append(is_grey)

        exp_reg_name = row.get("expected_regime", "CLEAN_NON_AD")
        exp_reg_id = int(row.get("expected_regime_id", int(LegalRegime[exp_reg_name].value)))

        probs.append(float(rep.violation_probability))
        preds.append(pred_viol)
        labels.append(exp_viol)
        pred_regs.append(int(rep.regime.value))
        true_regs.append(exp_reg_id)
        details.append(
            {
                "post_id": row["post_id"],
                "zone": "GREY" if is_grey else "DET",
                "expected": bool(exp_viol),
                "predicted": bool(pred_viol),
                "prob": round(float(rep.violation_probability), 4),
                "conformal": rep.conformal_verdict,
                "expected_regime": exp_reg_name,
                "predicted_regime": rep.regime_name,
                "match": exp_viol == pred_viol,
            }
        )

    probs_arr = np.array(probs, dtype=np.float32)
    preds_arr = np.array(preds, dtype=np.int32)
    labels_arr = np.array(labels, dtype=np.int32)
    pred_regs_arr = np.array(pred_regs, dtype=np.int32)
    true_regs_arr = np.array(true_regs, dtype=np.int32)

    all_metrics = _compute_binary_eval_metrics(
        probs=probs_arr,
        preds=preds_arr,
        labels=labels_arr,
        pred_regimes=pred_regs_arr,
        true_regimes=true_regs_arr,
    )

    grey_mask = np.array(grey_flags, dtype=bool)
    grey_metrics = _compute_binary_eval_metrics(
        probs=probs_arr[grey_mask],
        preds=preds_arr[grey_mask],
        labels=labels_arr[grey_mask],
        pred_regimes=pred_regs_arr[grey_mask],
        true_regimes=true_regs_arr[grey_mask],
    )
    return all_metrics, grey_metrics, len(records), details


async def _run_dual_auditor_comparison(bundle: AdRadarBrainBundle, golden_path: Path, use_vega: bool) -> None:
    records = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sensor = None
    if use_vega:
        from verification_engine.sheaf_vega_verifier import VegaUSensor
        sensor = VegaUSensor(light_mode=False)

    auditor = DualAuditor(bundle=bundle, vega_sensor=sensor, enable_vega_grey_zone=use_vega)
    correct_all, correct_grey, total_grey = 0, 0, 0
    mismatches = []
    for row in records:
        res = await auditor.audit_article(
            url=row["url"],
            text=row["text"],
            raw_links=row.get("external_links") or [],
            title_hint=row.get("title"),
            platform_ad_meta_hint=row.get("platform_ad_meta") or {},
        )
        exp_v = bool(row["expected_is_violation"])
        pred_v = bool(res["is_violation_38fz"])
        is_grey = _is_grey_zone_record(row)
        if is_grey:
            total_grey += 1
        if exp_v == pred_v:
            correct_all += 1
            if is_grey:
                correct_grey += 1
        else:
            mismatches.append((row["post_id"], "GREY" if is_grey else "DET", row["expected_regime"], res["scenario_38fz"], res["violation_probability"], res.get("vega_grey_zone_resolved")))
    mode_lbl = "Tier1 + Vega-U" if use_vega else "Tier1 DualAuditor"
    print(f"\n[*] {mode_lbl} on Golden Set: All={correct_all}/{len(records)} ({correct_all/len(records):.1%}) | Grey={correct_grey}/{total_grey} ({correct_grey/max(1,total_grey):.1%})")
    for m in mismatches:
        print("  MISMATCH:", m)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and evaluate champion_v3.bundle with deepvk/USER2-base")
    parser.add_argument("--retrain", action="store_true", help="Force retrain LinkCentricAdClassifierV3")
    parser.add_argument("--skip-retrain", action="store_true", help="Reuse existing checkpoint if trained with USER2-base")
    parser.add_argument("--epochs", type=int, default=35, help="Training epochs")
    parser.add_argument("--device", type=str, default="cuda", help="Compute device (cuda/cpu)")
    parser.add_argument("--eval-vega", action="store_true", help="Also run end-to-end DualAuditor + live Vega-U eval")
    args = parser.parse_args()

    art_dir = PKG_DIR / "artifacts"
    kb_dir = PKG_DIR / "legal_kb_seed"
    golden_real_path = PKG_DIR / "tests" / "golden_set_real_vcru.jsonl"
    ckpt_path = art_dir / "link_classifier_v3.pt"

    need_retrain = args.retrain
    if not need_retrain and not args.skip_retrain:
        if not ckpt_path.exists():
            need_retrain = True
        else:
            ckpt_probe = torch.load(ckpt_path, map_location="cpu", weights_only=False)
            ckpt_meta = ckpt_probe.get("metrics") or {}
            if ckpt_meta.get("encoder_model") != "deepvk/USER2-base" or ckpt_meta.get("use_fast_ngram_fallback", True):
                need_retrain = True

    train_metrics: Dict[str, Any] = {}
    if need_retrain:
        train_metrics = train_link_classifier(
            epochs=args.epochs,
            device_name=args.device,
            artifacts_dir=str(art_dir),
            use_fast_ngram_fallback=False,
        )
    elif ckpt_path.exists():
        ckpt_probe = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        train_metrics = dict(ckpt_probe.get("metrics") or {})

    print("[*] Building champion_v3.bundle with live deepvk/USER2-base...")
    bundle_v3 = AdRadarBrainBundle.create_from_artifacts(
        artifacts_dir=art_dir,
        kb_seed_dir=kb_dir,
        bundle_version="3.0.0",
        use_fast_ngram_fallback=False,
        encoder_device=args.device,
        certified_metrics=train_metrics or None,
    )

    statutory_eval_metrics = _evaluate_bundle_on_statutory_holdout(bundle_v3, seed=9999, num_samples=170)
    real_vcru_eval_metrics, grey_eval_metrics, real_count, real_details = evaluate_bundle_on_real_vcru_golden(
        bundle_v3, golden_real_path
    )

    # Validate 5 Hard Negatives
    sim = ProceduralCaseSimulator(seed=2026)
    print("\n[*] Validating 5 Hard Negatives on champion_v3:")
    hard_neg_archetypes = [
        (20, "Clinic Mandatory Informing (Art. 79 323-FZ)"),
        (21, "Corporate HR Vacancies (Art. 2 38-FZ)"),
        (22, "B2B Industrial Plant ISO/OEM (168-FZ)"),
        (23, "RAS Dictionary Words & Trademark (168-FZ)"),
        (24, "Uniform Dealer List / Where to Buy (Art. 2 38-FZ)"),
    ]
    for arch_id, desc in hard_neg_archetypes:
        doc, _ = sim.generate_case(arch_id)
        report = bundle_v3.verify_document(doc)
        is_clean = not report.is_violation
        print(
            f"  - Archetype {arch_id} [{desc}]: is_violation={report.is_violation} "
            f"(p={report.violation_probability:.4f}) -> {'PASS (CLEAN)' if is_clean else 'FAIL'}"
        )
        assert is_clean, f"Hard negative {desc} failed: falsely flagged as violation!"

    bundle_v3.manifest["synthetic_eval_metrics"] = statutory_eval_metrics
    bundle_v3.manifest["real_vcru_eval_metrics"] = real_vcru_eval_metrics
    bundle_v3.manifest["real_vcru_grey_eval_metrics"] = grey_eval_metrics
    bundle_v3.manifest["real_human_labeled_count"] = int(real_count)
    bundle_v3.manifest["certified_metrics"] = dict(real_vcru_eval_metrics)
    bundle_v3.manifest["encoder_model"] = bundle_v3._feature_builder.encoder_model_name
    bundle_v3.manifest["encoder_device"] = str(bundle_v3._feature_builder.device)
    bundle_v3.manifest["use_fast_ngram_fallback"] = bool(bundle_v3._feature_builder.use_fast_ngram_fallback)

    v3_artifact_path = art_dir / "champion_v3.bundle"
    bundle_v3.save(v3_artifact_path)
    print(f"\n[*] Saved artifact: {v3_artifact_path} ({v3_artifact_path.stat().st_size:,} bytes)")

    MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    v3_cache_path = MODELS_CACHE_DIR / "champion_v3.bundle"
    shutil.copy2(v3_artifact_path, v3_cache_path)
    print(f"[*] Promoted to model cache: {v3_cache_path} ({v3_cache_path.stat().st_size:,} bytes)")

    reloaded = AdRadarBrainBundle.load(v3_cache_path, verify_hash=True)
    integrity = reloaded.verify_integrity()
    sha256_hash = compute_sha256_file(v3_cache_path)

    print("\n=== CHAMPION V3 HONEST EVALUATION & PROMOTION ===")
    print("Statutory Holdout (N=170):", json.dumps(statutory_eval_metrics, ensure_ascii=False))
    print(f"Real VC.ru Golden (N={real_count}):", json.dumps(real_vcru_eval_metrics, ensure_ascii=False))
    print(f"Real VC.ru Grey-Zone (N={grey_eval_metrics['sample_count']}):", json.dumps(grey_eval_metrics, ensure_ascii=False))
    for d in real_details:
        if not d["match"] or d["zone"] == "GREY":
            tag = "OK" if d["match"] else "MISMATCH"
            print(f"  [{tag}][{d['zone']}] post={d['post_id']} exp={d['expected']} pred={d['predicted']} prob={d['prob']} conf={d['conformal']} reg={d['predicted_regime']}")
    print(f"Bundle Version: {reloaded.version} | Integrity: {integrity['is_valid']} | ONNX Bytes: {len(reloaded.onnx_bytes):,}")
    print(f"File SHA-256: {sha256_hash}")
    print("===================================================\n")

    if args.eval_vega:
        asyncio.run(_run_dual_auditor_comparison(reloaded, golden_real_path, use_vega=False))
        asyncio.run(_run_dual_auditor_comparison(reloaded, golden_real_path, use_vega=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

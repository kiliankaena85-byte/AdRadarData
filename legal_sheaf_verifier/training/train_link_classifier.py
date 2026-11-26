"""
Complete Training Pipeline for LinkCentricAdClassifierV3 (AdRadar v3.0).
Real-First training on non-golden VC.ru articles + minimal statutory hard-negative anchors.
Runs 5-fold Stratified CV on real VC.ru articles, computes train normalization buffers,
trains with sample-weighted Focal BCE + Regime CE, fits Split-Conformal calibration,
and exports link_classifier_v3.pt + link_classifier_v3.onnx.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

pkg_src = str(Path(__file__).resolve().parent.parent / "src")
pkg_root = str(Path(__file__).resolve().parent.parent)
if pkg_src not in sys.path:
    sys.path.insert(0, pkg_src)
if pkg_root not in sys.path:
    sys.path.insert(0, pkg_root)

import numpy as np
import torch
import torch.nn as nn
from sklearn.model_selection import StratifiedKFold
from torch.utils.data import DataLoader, TensorDataset

from legal_sheaf.calibration import ConformalCalibrator, compute_ece
from legal_sheaf.config import DEFAULT_DATA_DIR
from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.link_classifier import LinkCentricAdClassifierV3, LinkClassifierLoss
from legal_sheaf.link_features import LinkCentricFeatureExtractor
from legal_sheaf.onnx_runtime import export_link_classifier_to_onnx
from legal_sheaf.schema import DocumentInput, LegalRegime
from training.procedural_case_sim import ProceduralCaseSimulator

_HUMAN_LABEL_MAP: Dict[str, Tuple[int, LegalRegime]] = {
    "UNMARKED_AD_NO_ERID": (1, LegalRegime.UNMARKED_AD_NO_ERID),
    "INCOMPLETE_MARKING_LABEL_OR_INN": (1, LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN),
    "FOREIGN_LANG_168FZ": (1, LegalRegime.FOREIGN_LANG_168FZ),
    "BANNED_OR_SPECIAL_REGIME": (1, LegalRegime.BANNED_OR_SPECIAL_REGIME),
    "CLEAN_MARKED_AD": (0, LegalRegime.CLEAN_MARKED_AD),
    "CLEAN_NON_AD": (0, LegalRegime.CLEAN_NON_AD),
    "OVER_MARKED_NON_AD": (0, LegalRegime.OVER_MARKED_NON_AD),
}


def _load_safe_real_vcru_cases(
    real_data_path: Optional[str] = None,
    oversample_factor: int = 3,
    cal_fraction: float = 0.25,
) -> Tuple[List[Tuple[DocumentInput, Dict[str, Any]]], List[Tuple[DocumentInput, Dict[str, Any]]], Dict[str, Any]]:
    """
    Loads real VC.ru labeled records while strictly excluding:
    1) Every post_id and URL present in tests/golden_set_real_vcru.jsonl (zero data leakage),
    2) Any unresolved / NEEDS_MICRO_Q / SKIP rows.
    Splits unique articles into stratified train & calibration pools BEFORE applying
    per-source weights.
    """
    golden_path = Path(__file__).resolve().parent.parent / "tests" / "golden_set_real_vcru.jsonl"
    golden_ids = set()
    golden_urls = set()
    if golden_path.exists():
        for line in golden_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                g = json.loads(line)
                if g.get("post_id") is not None:
                    golden_ids.add(int(g["post_id"]))
                if g.get("url"):
                    golden_urls.add(str(g["url"]).strip().lower().rstrip("/"))
            except Exception:
                continue

    data_dir = Path(os.environ.get("ADRADAR_DATA_DIR", DEFAULT_DATA_DIR)).resolve()
    candidate = Path(real_data_path) if real_data_path else (data_dir / "data" / "vcru_human_labeled.jsonl")
    stats: Dict[str, Any] = {
        "real_file_path": str(candidate),
        "real_file_exists": bool(candidate.exists()),
        "raw_non_skip_rows": 0,
        "filtered_golden_overlap": 0,
        "safe_real_unique": 0,
        "human_verified_unique_w3": 0,
        "unanimous_consensus_unique_w2": 0,
        "train_real_weighted": 0,
        "cal_real_weighted": 0,
        "oversample_weight": int(oversample_factor),
    }
    if not candidate.exists():
        return [], [], stats

    parsed: List[Tuple[DocumentInput, Dict[str, Any], int, str]] = []
    seen_real_keys = set()
    skipped_overlap = 0
    for line in candidate.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if "document" in item and "target" in item:
            doc = DocumentInput(**item["document"])
            stats["raw_non_skip_rows"] += 1
            p_url = str(doc.page_url or "").strip().lower().rstrip("/")
            if p_url and p_url in golden_urls:
                skipped_overlap += 1
                continue
            if p_url in seen_real_keys:
                continue
            seen_real_keys.add(p_url)
            parsed.append((doc, item["target"], max(1, int(oversample_factor)), "HUMAN_VERIFIED"))
            continue

        lbl = str(item.get("human_label") or "").strip()
        lbl_source = str(item.get("label_source") or "HUMAN_VERIFIED").strip()
        if not lbl or lbl == "SKIP" or lbl not in _HUMAN_LABEL_MAP or lbl_source == "PENDING_HUMAN_MICRO_Q":
            continue
        stats["raw_non_skip_rows"] += 1
        pid = item.get("post_id")
        p_url = str(item.get("url") or "").strip().rstrip("/")
        p_url_norm = p_url.lower()
        if (pid is not None and int(pid) in golden_ids) or (p_url_norm and p_url_norm in golden_urls):
            skipped_overlap += 1
            continue

        dedup_k = f"id:{int(pid)}" if pid is not None else f"url:{p_url_norm}"
        if dedup_k in seen_real_keys:
            continue
        seen_real_keys.add(dedup_k)

        raw_w = item.get("sample_weight")
        if raw_w is not None:
            item_weight = max(1, int(math.ceil(float(raw_w))))
        elif lbl_source == "DEEP_BOOST_VERIFIED":
            item_weight = 3
        elif lbl_source == "UNANIMOUS_CONSENSUS":
            item_weight = 2
        else:
            item_weight = max(1, int(oversample_factor))

        if lbl_source in ("UNANIMOUS_CONSENSUS", "DEEP_BOOST_VERIFIED"):
            stats["unanimous_consensus_unique_w2"] += 1
        else:
            stats["human_verified_unique_w3"] += 1

        is_viol, reg_enum = _HUMAN_LABEL_MAP[lbl]
        text_val = str(item.get("text") or item.get("text_window") or "").strip()
        if not text_val:
            continue
        doc = DocumentInput(
            clean_text=text_val,
            page_url=p_url or f"https://vc.ru/p/{pid or 0}",
            author_domain="vc.ru",
            platform_ad_meta=item.get("platform_ad_meta") or {"is_promo": bool(item.get("is_promo"))},
            context_mode="EXTERNAL_PUBLICATION",
        )
        target = {
            "is_violation": is_viol,
            "regime": int(reg_enum.value),
        }
        parsed.append((doc, target, item_weight, lbl_source))

    stats["filtered_golden_overlap"] = skipped_overlap
    stats["safe_real_unique"] = len(parsed)
    if skipped_overlap:
        print(f"[*] Anti-leakage filter excluded {skipped_overlap} golden_set overlapping case(s).")

    if not parsed:
        return [], [], stats

    rng = np.random.default_rng(42)
    pos_items = [p for p in parsed if int(p[1]["is_violation"]) == 1]
    neg_items = [p for p in parsed if int(p[1]["is_violation"]) == 0]

    def _split_bucket(bucket: List[Tuple[DocumentInput, Dict[str, Any], int, str]]):
        if len(bucket) < 2:
            return bucket, []
        idx = rng.permutation(len(bucket))
        n_c = max(1, int(round(len(bucket) * cal_fraction)))
        n_c = min(n_c, len(bucket) - 1)
        c_part = [bucket[int(i)] for i in idx[:n_c]]
        t_part = [bucket[int(i)] for i in idx[n_c:]]
        return t_part, c_part

    if len(parsed) >= 4:
        tr_pos, cal_pos = _split_bucket(pos_items)
        tr_neg, cal_neg = _split_bucket(neg_items)
        tr_raw = tr_pos + tr_neg
        cal_raw = cal_pos + cal_neg
        if not cal_raw:
            cal_raw = tr_raw[:1]
            tr_raw = tr_raw[1:] or tr_raw
    else:
        tr_raw = parsed
        cal_raw = parsed

    tr_weighted: List[Tuple[DocumentInput, Dict[str, Any]]] = []
    cal_weighted: List[Tuple[DocumentInput, Dict[str, Any]]] = []
    for doc, tgt, w, _ in tr_raw:
        for _ in range(w):
            tr_weighted.append((doc, tgt))
    for doc, tgt, w, _ in cal_raw:
        for _ in range(w):
            cal_weighted.append((doc, tgt))

    stats["train_real_weighted"] = len(tr_weighted)
    stats["cal_real_weighted"] = len(cal_weighted)
    print(
        f"[+] Mixed safe VC.ru ensemble dataset "
        f"(HUMAN w=3: {stats['human_verified_unique_w3']}, AUTO_GOLD/DEEP w>=2: {stats['unanimous_consensus_unique_w2']}): "
        f"train_unique={len(tr_raw)} (weighted={len(tr_weighted)}), "
        f"cal_unique={len(cal_raw)} (weighted={len(cal_weighted)})."
    )
    return tr_weighted, cal_weighted, stats


def set_seed(seed: int = 42) -> None:
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def build_feature_tensors_from_cases(
    cases: List[Tuple[DocumentInput, Dict[str, Any]]],
    extractor: LinkCentricFeatureExtractor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Extracts deduplicated batched [N, 48] features and target tensors."""
    if not cases:
        return (
            torch.zeros((0, 48), dtype=torch.float32),
            torch.zeros((0,), dtype=torch.float32),
            torch.zeros((0,), dtype=torch.long),
        )

    docs = [doc for doc, _ in cases]
    # Deduplicate identical DocumentInput objects (from oversampling) to encode once
    key_to_idx: Dict[Tuple[str, str], int] = {}
    unique_docs: List[DocumentInput] = []
    map_idx: List[int] = []
    for doc in docs:
        k = (
            doc.page_url or "",
            doc.clean_text[:200] + doc.clean_text[-120:],
            int(doc.published_days_ago or 0),
            doc.context_mode,
            json.dumps(doc.platform_ad_meta or {}, sort_keys=True),
        )
        if k not in key_to_idx:
            key_to_idx[k] = len(unique_docs)
            unique_docs.append(doc)
        map_idx.append(key_to_idx[k])

    unique_X = extractor.build_batch_features(unique_docs).cpu()
    X = unique_X[torch.tensor(map_idx, dtype=torch.long)]
    y_viol = torch.tensor([float(t["is_violation"]) for _, t in cases], dtype=torch.float32)
    y_regime = torch.tensor([int(t["regime"]) for _, t in cases], dtype=torch.long)
    return X, y_viol, y_regime


def compute_macro_f1(preds: np.ndarray, targets: np.ndarray, num_classes: int = 7) -> float:
    f1s = []
    for c in range(num_classes):
        tp = np.sum((preds == c) & (targets == c))
        fp = np.sum((preds == c) & (targets != c))
        fn = np.sum((preds != c) & (targets == c))
        if tp == 0:
            f1 = 1.0 if (fp == 0 and fn == 0) else 0.0
        else:
            prec = tp / (tp + fp)
            rec = tp / (tp + fn)
            f1 = (2 * prec * rec) / (prec + rec)
        f1s.append(f1)
    return float(np.mean(f1s))


def _run_real_cv(
    real_cases_unique: List[Tuple[DocumentInput, Dict[str, Any]]],
    extractor: LinkCentricFeatureExtractor,
    device: torch.device,
    epochs: int = 35,
    lr: float = 2e-3,
) -> Dict[str, Any]:
    """5-fold Stratified CV on unique real VC.ru articles to measure honest generalization."""
    if len(real_cases_unique) < 15:
        return {"cv_folds": 0, "sample_count": len(real_cases_unique)}

    X_all, y_v_all, y_r_all = build_feature_tensors_from_cases(real_cases_unique, extractor)
    y_np = y_v_all.numpy().astype(int)
    n_splits = min(5, int(np.sum(y_np == 1)), int(np.sum(y_np == 0)))
    if n_splits < 2:
        return {"cv_folds": 0, "sample_count": len(real_cases_unique)}

    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    oof_probs = np.zeros(len(real_cases_unique), dtype=np.float32)
    cascade_preds = np.zeros(len(real_cases_unique), dtype=np.int32)
    explainer = LegalSheafExplainer()

    for fold_idx, (tr_idx, va_idx) in enumerate(skf.split(X_all.numpy(), y_np)):
        set_seed(42 + fold_idx)
        X_tr, y_v_tr, y_r_tr = X_all[tr_idx], y_v_all[tr_idx], y_r_all[tr_idx]
        X_va = X_all[va_idx].to(device)

        mean_tr = X_tr.mean(dim=0)
        std_tr = torch.clamp(X_tr.std(dim=0), min=1e-2)

        model = LinkCentricAdClassifierV3().to(device)
        model.set_normalization(mean_tr, std_tr)
        loss_fn = LinkClassifierLoss(focal_gamma=1.5, focal_alpha=0.60, lambda_regime=0.5, label_smoothing=0.0)
        opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=5e-2)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

        ds = TensorDataset(X_tr, y_v_tr, y_r_tr)
        loader = DataLoader(ds, batch_size=min(24, len(ds)), shuffle=True)
        model.train()
        for _ in range(epochs):
            for bx, bv, br in loader:
                bx, bv, br = bx.to(device), bv.to(device), br.to(device)
                opt.zero_grad()
                preds = model(bx)
                loss = loss_fn(preds, {"is_violation": bv, "regime": br})["loss_total"]
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
            sched.step()

        model.eval()
        with torch.no_grad():
            va_out = model(X_va)
            logits = va_out["violation_logit"].squeeze(-1).cpu().numpy()
            reg_logits = va_out["regime_logits"].cpu().numpy()
            attrs = va_out["feature_attributions"].cpu().numpy()
            oof_probs[va_idx] = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
            for local_i, orig_i in enumerate(va_idx):
                doc_va = real_cases_unique[int(orig_i)][0]
                rep_va = explainer.explain(
                    doc=doc_va,
                    model_output={
                        "violation_logit": logits[local_i],
                        "regime_logits": reg_logits[local_i],
                        "feature_attributions": attrs[local_i],
                    },
                    feature_values=X_all[orig_i].numpy(),
                )
                cascade_preds[int(orig_i)] = int(bool(rep_va.is_violation))

    def _binary_stats(preds_arr: np.ndarray) -> Tuple[float, float, float, float, int, int, int, int]:
        tp_ = int(np.sum((preds_arr == 1) & (y_np == 1)))
        fp_ = int(np.sum((preds_arr == 1) & (y_np == 0)))
        fn_ = int(np.sum((preds_arr == 0) & (y_np == 1)))
        tn_ = int(np.sum((preds_arr == 0) & (y_np == 0)))
        p_ = tp_ / float(tp_ + fp_) if (tp_ + fp_) > 0 else 0.0
        r_ = tp_ / float(tp_ + fn_) if (tp_ + fn_) > 0 else 0.0
        f_ = (2.0 * p_ * r_) / (p_ + r_) if (p_ + r_) > 0 else 0.0
        a_ = (tp_ + tn_) / float(len(y_np))
        return p_, r_, f_, a_, tp_, fp_, tn_, fn_

    oof_preds = (oof_probs >= 0.50).astype(int)
    prec, rec, f1, acc, tp, fp, tn, fn = _binary_stats(oof_preds)
    c_prec, c_rec, c_f1, c_acc, c_tp, c_fp, c_tn, c_fn = _binary_stats(cascade_preds)

    cv_stats = {
        "cv_folds": n_splits,
        "sample_count": int(len(y_np)),
        "oof_precision": round(prec, 4),
        "oof_recall": round(rec, 4),
        "oof_f1": round(f1, 4),
        "oof_accuracy": round(acc, 4),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "cascade_oof_precision": round(c_prec, 4),
        "cascade_oof_recall": round(c_rec, 4),
        "cascade_oof_f1": round(c_f1, 4),
        "cascade_oof_accuracy": round(c_acc, 4),
        "cascade_tp": c_tp,
        "cascade_fp": c_fp,
        "cascade_tn": c_tn,
        "cascade_fn": c_fn,
    }
    print(f"[*] 5-Fold Real VC.ru OOF CV: {json.dumps(cv_stats, ensure_ascii=False)}")
    return cv_stats


def train_link_classifier(
    epochs: int = 35,
    batch_size: int = 32,
    lr: float = 2e-3,
    device_name: str = "cuda",
    artifacts_dir: str = "artifacts",
    real_data_path: Optional[str] = None,
    use_fast_ngram_fallback: bool = False,
) -> Dict[str, Any]:
    set_seed(42)
    device = torch.device(device_name if (device_name == "cuda" and torch.cuda.is_available()) else "cpu")
    print(f"[*] Training LinkCentricAdClassifierV3 on device: {device}")

    art_dir = Path(artifacts_dir)
    art_dir.mkdir(parents=True, exist_ok=True)

    extractor = LinkCentricFeatureExtractor(
        device=str(device),
        use_fast_ngram_fallback=use_fast_ngram_fallback,
    )
    print(f"[*] Semantic Encoder: {extractor.encoder_model_name} on {extractor.device}")

    model = LinkCentricAdClassifierV3().to(device)
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[*] LinkCentricAdClassifierV3 Trainable Parameters: {total_params:,}")

    # 1. Real-First Dataset: Load all safe real VC.ru articles + minimal statutory anchors (2 per archetype)
    safe_real_tr, safe_real_cal, real_stats = _load_safe_real_vcru_cases(real_data_path)

    # Deduplicate real calibration rows (unweighted 1x) to satisfy conformal exchangeability
    seen_cal_u = set()
    cal_real_unique: List[Tuple[DocumentInput, Dict[str, Any]]] = []
    for d, t in safe_real_cal:
        kc = (d.page_url, len(d.clean_text))
        if kc not in seen_cal_u:
            seen_cal_u.add(kc)
            cal_real_unique.append((d, t))

    # Deduplicate real cases just for OOF CV reporting
    seen_cv = set()
    real_unique_for_cv: List[Tuple[DocumentInput, Dict[str, Any]]] = []
    for d, t in safe_real_tr + cal_real_unique:
        k = (d.page_url, len(d.clean_text))
        if k not in seen_cv:
            seen_cv.add(k)
            real_unique_for_cv.append((d, t))
    real_cv_metrics = _run_real_cv(real_unique_for_cv, extractor, device, epochs=epochs, lr=lr)

    sim_tr = ProceduralCaseSimulator(seed=42)
    anchor_train = sim_tr.generate_dataset(num_samples=68)  # 2 per 34 archetypes
    sim_val = ProceduralCaseSimulator(seed=1337)
    anchor_val = sim_val.generate_dataset(num_samples=34)   # 1 per 34 archetypes
    sim_test = ProceduralCaseSimulator(seed=9999)
    test_cases = sim_test.generate_dataset(num_samples=170) # 5 per 34 archetypes

    train_cases: List[Tuple[DocumentInput, Dict[str, Any]]] = list(safe_real_tr) + anchor_train
    val_cases: List[Tuple[DocumentInput, Dict[str, Any]]] = list(cal_real_unique) + anchor_val

    print(
        f"[*] Dataset composition -> Train: {len(safe_real_tr)} real + {len(anchor_train)} anchors = {len(train_cases)} | "
        f"Cal: {len(cal_real_unique)} unique real + {len(anchor_val)} anchors = {len(val_cases)} | "
        f"Test anchors: {len(test_cases)}"
    )

    X_train, y_viol_tr, y_reg_tr = build_feature_tensors_from_cases(train_cases, extractor)
    X_val, y_viol_val, y_reg_val = build_feature_tensors_from_cases(val_cases, extractor)
    X_test, y_viol_te, y_reg_te = build_feature_tensors_from_cases(test_cases, extractor)

    # Store normalization stats inside ONNX model buffers
    feat_mean = X_train.mean(dim=0)
    feat_std = torch.clamp(X_train.std(dim=0), min=1e-2)
    model.set_normalization(feat_mean, feat_std)

    train_ds = TensorDataset(X_train, y_viol_tr, y_reg_tr)
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

    loss_fn = LinkClassifierLoss(focal_gamma=1.5, focal_alpha=0.60, lambda_regime=0.5, label_smoothing=0.0)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=5e-2)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    print("\n--- BEGINNING LINK-CENTRIC CLASSIFIER V3 TRAINING ---")
    model.train()
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        for batch_x, batch_v, batch_r in train_loader:
            batch_x = batch_x.to(device)
            batch_v = batch_v.to(device)
            batch_r = batch_r.to(device)

            optimizer.zero_grad()
            preds = model(batch_x)
            losses = loss_fn(preds, {"is_violation": batch_v, "regime": batch_r})
            loss_total = losses["loss_total"]
            loss_total.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_loss += float(loss_total.item())

        scheduler.step()
        if epoch % 15 == 0 or epoch == epochs:
            print(f"Epoch {epoch:02d}/{epochs:02d} - Loss: {epoch_loss / max(1, len(train_loader)):.4f}")

    # Conformal Calibration
    model.eval()
    with torch.no_grad():
        val_preds = model(X_val.to(device))
        val_v_logits = val_preds["violation_logit"].squeeze(-1).cpu().numpy()
        val_labels = y_viol_val.numpy()

    calibrator = ConformalCalibrator(target_precision=0.96)
    calibrator.calibrate(val_v_logits, val_labels)
    calib_path = art_dir / "conformal_calibration.json"
    calibrator.save(str(calib_path))
    print(
        f"[*] Calibrator saved -> threshold={calibrator.decision_threshold:.3f}, "
        f"band=[{calibrator.abstain_lower:.3f}, {calibrator.abstain_upper:.3f}], "
        f"T={calibrator.temperature_val:.3f}, Val ECE={calibrator.ece_score:.4f}"
    )

    # Evaluate on Statutory Holdout Anchors (N=170)
    with torch.no_grad():
        test_preds = model(X_test.to(device))
        test_v_logits = test_preds["violation_logit"].squeeze(-1).cpu().numpy()
        test_reg_logits = test_preds["regime_logits"].cpu().numpy()

    test_labels = y_viol_te.numpy()
    test_reg_labels = y_reg_te.numpy()
    test_calib_logits = test_v_logits / max(1e-4, calibrator.temperature_val)
    test_probs = 1.0 / (1.0 + np.exp(-np.clip(test_calib_logits, -40.0, 40.0)))
    test_ece = compute_ece(test_probs, test_labels)

    test_pred_viol = (test_probs >= calibrator.decision_threshold).astype(int)
    pos_mask = test_pred_viol == 1
    precision_viol = float(np.mean(test_labels[pos_mask])) if np.sum(pos_mask) > 0 else 0.0
    recall_viol = float(np.sum((test_pred_viol == 1) & (test_labels == 1)) / max(1, np.sum(test_labels == 1)))
    brier_score = float(np.mean((test_probs - test_labels) ** 2))
    accuracy = float(np.mean(test_pred_viol == test_labels))
    macro_f1 = compute_macro_f1(np.argmax(test_reg_logits, axis=-1), test_reg_labels, num_classes=7)

    metrics = {
        "architecture": "LinkCentricAdClassifierV3",
        "feature_dim": 48,
        "trainable_params": int(total_params),
        "precision": round(precision_viol, 4),
        "recall": round(recall_viol, 4),
        "macro_f1": round(macro_f1, 4),
        "ece": round(test_ece, 4),
        "brier_score": round(brier_score, 4),
        "accuracy": round(accuracy, 4),
        "sample_count": int(len(test_labels)),
        "encoder_model": extractor.encoder_model_name,
        "use_fast_ngram_fallback": bool(extractor.use_fast_ngram_fallback),
        "real_human_labeled_training": real_stats,
        "real_cv_metrics": real_cv_metrics,
    }

    print("\n=======================================================")
    print("       LINK-CENTRIC V3 STATUTORY ANCHOR METRICS        ")
    print("=======================================================")
    for k, v in metrics.items():
        print(f"  {k:28s}: {v}")
    print("=======================================================\n")

    model_path = art_dir / "link_classifier_v3.pt"
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "metrics": metrics,
            "calibrator": {
                "decision_threshold": calibrator.decision_threshold,
                "abstain_lower": calibrator.abstain_lower,
                "abstain_upper": calibrator.abstain_upper,
                "temperature_val": calibrator.temperature_val,
            },
        },
        model_path,
    )
    print(f"[*] Checkpoint saved: {model_path}")

    onnx_path = art_dir / "link_classifier_v3.onnx"
    export_link_classifier_to_onnx(model.cpu(), onnx_path, device="cpu")
    print(f"[*] ONNX exported: {onnx_path} ({onnx_path.stat().st_size:,} bytes)")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train LinkCentricAdClassifierV3")
    parser.add_argument("--epochs", type=int, default=35)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=2e-3)
    parser.add_argument("--device", type=str, default="cuda")
    default_artifacts = str(Path(__file__).resolve().parent.parent / "artifacts")
    parser.add_argument("--artifacts-dir", type=str, default=default_artifacts)
    parser.add_argument("--real-data", type=str, default=None)
    parser.add_argument("--ngram-fallback", action="store_true")
    args = parser.parse_args()

    train_link_classifier(
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        device_name=args.device,
        artifacts_dir=args.artifacts_dir,
        real_data_path=args.real_data,
        use_fast_ngram_fallback=args.ngram_fallback,
    )

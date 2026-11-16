"""
Complete Training Pipeline for LegalSheafNet-16x41.
Curriculum pretraining on 20 authentic legal archetypes + fine-tuning on JSONL data.
Trains, evaluates Stiefel orthogonality, fits Conformal Calibration, and saves artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Ensure package src directory is on sys.path
pkg_src = str(Path(__file__).resolve().parent.parent / "src")
pkg_root = str(Path(__file__).resolve().parent.parent)
if pkg_src not in sys.path:
    sys.path.insert(0, pkg_src)
if pkg_root not in sys.path:
    sys.path.insert(0, pkg_root)

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from legal_sheaf.calibration import ConformalCalibrator, compute_ece
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.losses import MultiTaskSheafLoss
from legal_sheaf.schema import DocumentInput, LegalRegime
from legal_sheaf.sheaf_model import LegalSheafNet16x41
from training.procedural_case_sim import ProceduralCaseSimulator


def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def build_tensors_from_cases(
    cases: List[Tuple[DocumentInput, Dict[str, Any]]],
    builder: SlotFeatureBuilder,
    batch_size: int = 64,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Extract features and ground truth labels for a list of simulated or real cases.
    Returns:
        X: [N, 16, 36]
        y_viol: [N]
        y_regime: [N]
        y_slots: [N, 16]
    """
    X_list = []
    y_viol_list = []
    y_regime_list = []
    y_slots_list = []

    for doc, target in cases:
        t = builder.build_document_tensor(doc)  # [16, 36]
        X_list.append(t.cpu())
        y_viol_list.append(float(target["is_violation"]))
        y_regime_list.append(int(target["regime"]))
        y_slots_list.append(target["slot_labels"])

    X = torch.stack(X_list, dim=0)
    y_viol = torch.tensor(y_viol_list, dtype=torch.float32)
    y_regime = torch.tensor(y_regime_list, dtype=torch.long)
    y_slots = torch.tensor(y_slots_list, dtype=torch.float32)

    return X, y_viol, y_regime, y_slots


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


def train_legal_sheaf(
    epochs: int = 25,
    batch_size: int = 64,
    lr: float = 1.5e-3,
    device_name: str = "cuda",
    artifacts_dir: str = "artifacts",
    real_data_path: Optional[str] = None,
) -> Dict[str, Any]:
    set_seed(42)
    device = torch.device(device_name if (device_name == "cuda" and torch.cuda.is_available()) else "cpu")
    print(f"[*] Training LegalSheafNet-16x41 on device: {device}")

    Path(artifacts_dir).mkdir(parents=True, exist_ok=True)

    # 1. Instantiate Feature Builder & Model (with fast fallback for training/testing reproducibility)
    feature_builder = SlotFeatureBuilder(device=str(device), use_fast_ngram_fallback=True)
    model = LegalSheafNet16x41().to(device)

    # Count parameters
    total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    model_size_mb = sum(p.numel() * p.element_size() for p in model.parameters()) / (1024 * 1024)
    print(f"[*] LegalSheafNet-16x41 Parameters: {total_params:,} ({model_size_mb:.2f} MB)")

    # 2. Synthesize or Load Datasets
    sim = ProceduralCaseSimulator(seed=42)
    print("[*] Generating 1200 training cases across 20 legal archetypes...")
    train_cases = sim.generate_dataset(num_samples=1200)

    print("[*] Generating 400 validation cases for Conformal Calibration...")
    sim_val = ProceduralCaseSimulator(seed=1337)
    val_cases = sim_val.generate_dataset(num_samples=400)

    print("[*] Generating 500 test cases for Quality Gate 4 evaluation...")
    sim_test = ProceduralCaseSimulator(seed=9999)
    test_cases = sim_test.generate_dataset(num_samples=500)

    # Optional: Append real data cases if provided
    if real_data_path and Path(real_data_path).exists():
        print(f"[*] Loading real cases from {real_data_path}...")
        with open(real_data_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    doc = DocumentInput(**item["document"])
                    train_cases.append((doc, item["target"]))

    print("[*] Building input feature tensors (16 x 36)...")
    X_train, y_viol_tr, y_reg_tr, y_slots_tr = build_tensors_from_cases(train_cases, feature_builder)
    X_val, y_viol_val, y_reg_val, y_slots_val = build_tensors_from_cases(val_cases, feature_builder)
    X_test, y_viol_te, y_reg_te, y_slots_te = build_tensors_from_cases(test_cases, feature_builder)

    train_ds = TensorDataset(X_train, y_viol_tr, y_reg_tr, y_slots_tr)
    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

    # 3. Setup Optimizer and Loss
    loss_fn = MultiTaskSheafLoss(
        focal_gamma=2.0,
        focal_alpha=0.65,
        lambda_regime=1.0,
        lambda_slot_aux=0.5,
        lambda_energy_contrastive=0.3,
        lambda_stiefel_orth=0.1,
    )

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    # 4. Training Loop
    print("\n--- BEGINNING SHEAF NETWORK TRAINING ---")
    model.train()
    for epoch in range(1, epochs + 1):
        epoch_loss = 0.0
        for batch_x, batch_v, batch_r, batch_s in train_loader:
            batch_x = batch_x.to(device)
            batch_v = batch_v.to(device)
            batch_r = batch_r.to(device)
            batch_s = batch_s.to(device)

            optimizer.zero_grad()
            preds = model(batch_x)
            stiefel_loss = model.compute_stiefel_orthogonality_loss()

            targets = {
                "is_violation": batch_v,
                "regime": batch_r,
                "slot_labels": batch_s,
            }

            losses = loss_fn(preds, targets, stiefel_loss)
            loss_total = losses["loss_total"]
            loss_total.backward()

            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_loss += loss_total.item()

        scheduler.step()
        if epoch % 5 == 0 or epoch == epochs:
            avg_loss = epoch_loss / len(train_loader)
            print(f"Epoch {epoch:02d}/{epochs:02d} - Loss: {avg_loss:.4f} - Stiefel L_orth: {stiefel_loss.item():.4f}")

    # 5. Fit Conformal Calibrator on Validation Set
    print("\n[*] Fitting Temperature Scaling and Conformal Threshold on Validation Set...")
    model.eval()
    with torch.no_grad():
        val_x = X_val.to(device)
        val_preds = model(val_x)
        val_v_logits = val_preds["violation_logit"].squeeze(-1).cpu().numpy()
        val_labels = y_viol_val.numpy()

    calibrator = ConformalCalibrator(target_precision=0.96)
    calibrator.calibrate(val_v_logits, val_labels)
    calib_path = os.path.join(artifacts_dir, "conformal_calibration.json")
    calibrator.save(calib_path)
    print(f"[*] Calibrator saved. Optimal threshold: {calibrator.decision_threshold:.3f}, Temperature: {calibrator.temperature_val:.3f}, Val ECE: {calibrator.ece_score:.4f}")

    # 6. Comprehensive Evaluation on 500 Test Samples (Quality Gate 4)
    print("\n[*] Evaluating on 500 Test Samples...")
    with torch.no_grad():
        test_x = X_test.to(device)
        test_preds = model(test_x)
        test_v_logits = test_preds["violation_logit"].squeeze(-1).cpu().numpy()
        test_reg_logits = test_preds["regime_logits"].cpu().numpy()
        test_energies = test_preds["total_sheaf_energy"].squeeze(-1).cpu().numpy()

    test_labels = y_viol_te.numpy()
    test_reg_labels = y_reg_te.numpy()

    # Temperature-scaled probabilities for test
    test_calib_logits = test_v_logits / calibrator.temperature_val
    test_probs = 1.0 / (1.0 + np.exp(-test_calib_logits))
    test_ece = compute_ece(test_probs, test_labels)

    # Conformal predictions on test set
    test_pred_viol = (test_probs >= calibrator.decision_threshold).astype(int)
    pos_mask = (test_pred_viol == 1)
    if np.sum(pos_mask) > 0:
        precision_viol = float(np.mean(test_labels[pos_mask]))
        recall_viol = float(np.sum((test_pred_viol == 1) & (test_labels == 1)) / max(1, np.sum(test_labels == 1)))
    else:
        precision_viol = 0.0
        recall_viol = 0.0

    # Regime Macro-F1
    pred_regimes = np.argmax(test_reg_logits, axis=-1)
    macro_f1 = compute_macro_f1(pred_regimes, test_reg_labels, num_classes=7)

    # Sheaf Energy Contrast
    viol_mask = (test_labels == 1)
    clean_mask = (test_labels == 0)
    mean_energy_viol = float(np.mean(test_energies[viol_mask]))
    mean_energy_clean = float(np.mean(test_energies[clean_mask]))
    energy_ratio = mean_energy_viol / max(1e-6, mean_energy_clean)

    # Stiefel Non-degeneracy
    stiefel_loss_final = model.compute_stiefel_orthogonality_loss().item()

    metrics = {
        "macro_f1": round(macro_f1, 4),
        "precision_violation": round(precision_viol, 4),
        "recall_violation": round(recall_viol, 4),
        "test_ece": round(test_ece, 4),
        "mean_sheaf_energy_viol": round(mean_energy_viol, 4),
        "mean_sheaf_energy_clean": round(mean_energy_clean, 4),
        "energy_contrast_ratio": round(energy_ratio, 2),
        "stiefel_loss": round(stiefel_loss_final, 5),
    }

    print("\n=======================================================")
    print("           LEGAL SHEAF VERIFICATION METRICS           ")
    print("=======================================================")
    for k, v in metrics.items():
        print(f"  {k:28s}: {v}")
    print("=======================================================\n")

    # 7. Save model checkpoint
    model_path = os.path.join(artifacts_dir, "legal_sheaf_model.pt")
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "metrics": metrics,
            "calibrator": {
                "decision_threshold": calibrator.decision_threshold,
                "temperature_val": calibrator.temperature_val,
            },
        },
        model_path,
    )
    print(f"[*] Checkpoint saved successfully: {model_path}")

    # 8. Export ONNX model for high-performance inference
    from legal_sheaf.onnx_runtime import export_sheaf_to_onnx
    onnx_path = os.path.join(artifacts_dir, "legal_sheaf_model.onnx")
    export_sheaf_to_onnx(model.cpu(), onnx_path)
    print(f"[*] ONNX model exported successfully: {onnx_path}")

    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=25)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=1.5e-3)
    parser.add_argument("--device", type=str, default="cuda")
    default_artifacts = str(Path(__file__).resolve().parent.parent / "artifacts")
    parser.add_argument("--artifacts-dir", type=str, default=default_artifacts)
    parser.add_argument("--real-data", type=str, default=None)
    args = parser.parse_args()

    train_legal_sheaf(
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        device_name=args.device,
        artifacts_dir=args.artifacts_dir,
        real_data_path=args.real_data,
    )

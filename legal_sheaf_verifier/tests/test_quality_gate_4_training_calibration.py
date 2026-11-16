"""
Quality Gate 4 Test: Verification of Trained LegalSheafNet-16x41 and Conformal Calibration.
Evaluates:
- Macro-F1 across 7 regimes >= 0.93
- Precision on violation class after conformal threshold >= 0.96
- Expected Calibration Error (ECE) <= 0.05
- Mean sheaf energy on violations >= 3x mean sheaf energy on clean documents.
"""

import json
import sys
from pathlib import Path

# Add project root and src to sys.path
pkg_root = Path(__file__).resolve().parent.parent
if str(pkg_root) not in sys.path:
    sys.path.insert(0, str(pkg_root))
if str(pkg_root / "src") not in sys.path:
    sys.path.insert(0, str(pkg_root / "src"))

import numpy as np
import pytest
import torch

from legal_sheaf.calibration import ConformalCalibrator, compute_ece
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.schema import DocumentInput
from legal_sheaf.sheaf_model import LegalSheafNet16x41
from training.procedural_case_sim import ProceduralCaseSimulator
from training.train_sheaf import build_tensors_from_cases, compute_macro_f1


@pytest.fixture
def artifacts_dir():
    pkg_root = Path(__file__).resolve().parent.parent
    return pkg_root / "artifacts"


def test_quality_gate_4_metrics(artifacts_dir):
    model_path = artifacts_dir / "legal_sheaf_model.pt"
    calib_path = artifacts_dir / "conformal_calibration.json"

    assert model_path.exists(), f"Model checkpoint not found at {model_path}"
    assert calib_path.exists(), f"Calibration file not found at {calib_path}"

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load model
    model = LegalSheafNet16x41().to(device)
    ckpt = torch.load(model_path, map_location=device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    # Load calibrator
    calibrator = ConformalCalibrator()
    calibrator.load(str(calib_path))

    # Evaluate on 500 test cases
    sim = ProceduralCaseSimulator(seed=9999)
    test_cases = sim.generate_dataset(num_samples=500)
    feature_builder = SlotFeatureBuilder(device=str(device), use_fast_ngram_fallback=True)
    X_test, y_viol_te, y_reg_te, _ = build_tensors_from_cases(test_cases, feature_builder)

    with torch.no_grad():
        x = X_test.to(device)
        preds = model(x)
        v_logits = preds["violation_logit"].squeeze(-1).cpu().numpy()
        reg_logits = preds["regime_logits"].cpu().numpy()
        energies = preds["total_sheaf_energy"].squeeze(-1).cpu().numpy()

    labels = y_viol_te.numpy()
    reg_labels = y_reg_te.numpy()

    # 1. Macro-F1 across 7 regimes >= 0.93
    pred_regimes = np.argmax(reg_logits, axis=-1)
    macro_f1 = compute_macro_f1(pred_regimes, reg_labels, num_classes=7)
    assert macro_f1 >= 0.93, f"Macro-F1 {macro_f1:.4f} is below 0.93!"

    # 2. Precision on violation class >= 0.96
    calib_logits = v_logits / calibrator.temperature_val
    probs = 1.0 / (1.0 + np.exp(-calib_logits))
    pred_viol = (probs >= calibrator.decision_threshold).astype(int)
    pos_mask = (pred_viol == 1)
    assert np.sum(pos_mask) > 0
    precision = float(np.mean(labels[pos_mask]))
    assert precision >= 0.96, f"Precision {precision:.4f} is below 0.96!"

    # 3. Expected Calibration Error (ECE) <= 0.05
    ece = compute_ece(probs, labels)
    assert ece <= 0.05, f"ECE {ece:.4f} exceeds 0.05!"

    # 4. Sheaf energy contrast ratio >= 3.0x
    viol_mask = (labels == 1)
    clean_mask = (labels == 0)
    mean_e_viol = float(np.mean(energies[viol_mask]))
    mean_e_clean = float(np.mean(energies[clean_mask]))
    ratio = mean_e_viol / max(1e-6, mean_e_clean)
    assert ratio >= 3.0, f"Sheaf energy ratio {ratio:.2f} is below 3.0x!"

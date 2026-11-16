"""
End-to-End Determinism and ONNX Parity Tests (Quality Gate 5).
Verifies:
1. PyTorch to ONNX export and exact numerical equivalence (max_abs_diff < 1e-4)
2. 3 consecutive end-to-end runs yield bitwise/numerically identical outputs (diff < 1e-6)
3. Model weight < 10 MB.
"""

import os
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

from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.onnx_runtime import ONNXLegalSheafVerifier, export_sheaf_to_onnx
from legal_sheaf.schema import DocumentInput
from legal_sheaf.sheaf_model import LegalSheafNet16x41


@pytest.fixture
def artifacts_dir():
    return pkg_root / "artifacts"


def test_onnx_export_and_numerical_parity(artifacts_dir):
    model_path = artifacts_dir / "legal_sheaf_model.pt"
    onnx_path = artifacts_dir / "legal_sheaf_model.onnx"

    assert model_path.exists(), "Model checkpoint must exist!"

    device = "cpu"  # Export on CPU for universal ONNX portability
    model = LegalSheafNet16x41().to(device)
    ckpt = torch.load(model_path, map_location=device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    # 1. Export to ONNX
    exported_path = export_sheaf_to_onnx(model, onnx_path, device=device)
    assert os.path.exists(exported_path)

    # Check file size < 10 MB
    file_size_mb = os.path.getsize(exported_path) / (1024 * 1024)
    print(f"\n[ONNX] File size: {file_size_mb:.2f} MB")
    assert file_size_mb < 10.0, f"ONNX model {file_size_mb:.2f} MB exceeds 10 MB limit!"

    # 2. Compare PyTorch vs ONNX on 16 test samples
    verifier = ONNXLegalSheafVerifier(onnx_path, prefer_cuda=False)
    dummy_input = torch.randn(16, 16, 36)

    with torch.no_grad():
        pt_out = model(dummy_input)

    onnx_out = verifier.predict(dummy_input.numpy())

    # Parity on violation_logit
    diff_viol = np.max(np.abs(pt_out["violation_logit"].numpy() - onnx_out["violation_logit"]))
    assert diff_viol < 1e-4, f"Parity violation on violation_logit: max diff {diff_viol}"

    # Parity on regime_logits
    diff_reg = np.max(np.abs(pt_out["regime_logits"].numpy() - onnx_out["regime_logits"]))
    assert diff_reg < 1e-4, f"Parity violation on regime_logits: max diff {diff_reg}"

    # Parity on slot_probabilities
    diff_slots = np.max(np.abs(pt_out["slot_probabilities"].numpy() - onnx_out["slot_probabilities"]))
    assert diff_slots < 1e-4, f"Parity violation on slot_probabilities: max diff {diff_slots}"

    # Parity on edge_conflict_energies
    diff_edges = np.max(np.abs(pt_out["edge_conflict_energies"].numpy() - onnx_out["edge_conflict_energies"]))
    assert diff_edges < 1e-4, f"Parity violation on edge_conflict_energies: max diff {diff_edges}"


def test_end_to_end_determinism_three_runs(artifacts_dir):
    """
    Run 3 consecutive end-to-end inference passes from raw DocumentInput through
    SlotFeatureBuilder -> LegalSheafNet -> Explainer.
    Confirms zero drift: outputs are strictly identical (diff < 1e-6).
    """
    model_path = artifacts_dir / "legal_sheaf_model.pt"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = LegalSheafNet16x41().to(device)
    ckpt = torch.load(model_path, map_location=device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    builder = SlotFeatureBuilder(device=str(device))
    explainer = LegalSheafExplainer()

    control_doc = DocumentInput(
        clean_text=(
            "Ребята, рекомендую лучший авторский курс по Data Science! "
            "Скидка 40% по промокоду PYDATA только сегодня! "
            "Ссылка на регистрацию https://dataschool.ru/?utm_source=tg"
        ),
        raw_html="<div><p>Рекомендую лучший курс!</p></div>",
        page_url="https://dataschool.ru/?utm_source=tg",
        author_domain="t.me",
    )

    reports = []
    tensors = []

    for run_idx in range(3):
        with torch.no_grad():
            tensor = builder.build_document_tensor(control_doc).unsqueeze(0)  # [1, 16, 36]
            out = model(tensor)
            rep = explainer.explain(control_doc, out)
            reports.append(rep)
            tensors.append(out["edge_conflict_energies"].cpu().numpy())

    # Compare 3 runs
    assert reports[0].is_violation == reports[1].is_violation == reports[2].is_violation
    assert reports[0].regime == reports[1].regime == reports[2].regime
    assert abs(reports[0].violation_probability - reports[1].violation_probability) < 1e-6
    assert abs(reports[1].violation_probability - reports[2].violation_probability) < 1e-6

    # Compare edge conflict energies across runs
    diff_01 = np.max(np.abs(tensors[0] - tensors[1]))
    diff_12 = np.max(np.abs(tensors[1] - tensors[2]))
    assert diff_01 < 1e-6, f"Run 0 vs Run 1 drift: {diff_01}"
    assert diff_12 < 1e-6, f"Run 1 vs Run 2 drift: {diff_12}"

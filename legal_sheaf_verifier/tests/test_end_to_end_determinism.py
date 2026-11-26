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
from legal_sheaf.link_classifier import LinkCentricAdClassifierV3
from legal_sheaf.link_features import FEATURE_DIM, LinkCentricFeatureExtractor
from legal_sheaf.onnx_runtime import ONNXLinkClassifierVerifier, export_link_classifier_to_onnx
from legal_sheaf.schema import DocumentInput


@pytest.fixture
def artifacts_dir():
    return pkg_root / "artifacts"


def test_onnx_export_and_numerical_parity(artifacts_dir):
    model_path = artifacts_dir / "link_classifier_v3.pt"
    onnx_path = artifacts_dir / "link_classifier_v3.onnx"

    assert model_path.exists(), "Model checkpoint must exist!"

    device = "cpu"  # Export on CPU for universal ONNX portability
    model = LinkCentricAdClassifierV3().to(device)
    ckpt = torch.load(model_path, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    # 1. Export to ONNX
    exported_path = export_link_classifier_to_onnx(model, onnx_path, device=device)
    assert os.path.exists(exported_path)

    # Check file size < 10 MB
    file_size_mb = os.path.getsize(exported_path) / (1024 * 1024)
    print(f"\n[ONNX] File size: {file_size_mb:.2f} MB")
    assert file_size_mb < 10.0, f"ONNX model {file_size_mb:.2f} MB exceeds 10 MB limit!"

    # 2. Compare PyTorch vs ONNX on 16 test samples
    verifier = ONNXLinkClassifierVerifier(onnx_path, prefer_cuda=False)
    dummy_input = torch.randn(16, FEATURE_DIM)

    with torch.no_grad():
        pt_out = model(dummy_input)

    onnx_out = verifier.predict(dummy_input.numpy())

    # Parity on violation_logit
    diff_viol = np.max(np.abs(pt_out["violation_logit"].numpy() - onnx_out["violation_logit"]))
    assert diff_viol < 1e-4, f"Parity violation on violation_logit: max diff {diff_viol}"

    # Parity on regime_logits
    diff_reg = np.max(np.abs(pt_out["regime_logits"].numpy() - onnx_out["regime_logits"]))
    assert diff_reg < 1e-4, f"Parity violation on regime_logits: max diff {diff_reg}"

    # Parity on feature_attributions
    diff_attr = np.max(np.abs(pt_out["feature_attributions"].numpy() - onnx_out["feature_attributions"]))
    assert diff_attr < 1e-4, f"Parity violation on feature_attributions: max diff {diff_attr}"


def test_end_to_end_determinism_three_runs(artifacts_dir):
    """
    Run 3 consecutive end-to-end inference passes from raw DocumentInput through
    LinkCentricFeatureExtractor -> LinkCentricAdClassifierV3 -> Explainer.
    Confirms zero drift: outputs are strictly identical (diff < 1e-6).
    """
    model_path = artifacts_dir / "link_classifier_v3.pt"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = LinkCentricAdClassifierV3().to(device)
    ckpt = torch.load(model_path, map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    builder = LinkCentricFeatureExtractor(device=str(device))
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
    attributions = []

    for run_idx in range(3):
        with torch.no_grad():
            feat = builder.build_feature_vector(control_doc).unsqueeze(0).to(device)  # [1, 48]
            out = model(feat)
            rep = explainer.explain(control_doc, out)
            reports.append(rep)
            attributions.append(out["feature_attributions"].cpu().numpy())

    # Compare 3 runs
    assert reports[0].is_violation == reports[1].is_violation == reports[2].is_violation
    assert reports[0].regime == reports[1].regime == reports[2].regime
    assert abs(reports[0].violation_probability - reports[1].violation_probability) < 1e-6
    assert abs(reports[1].violation_probability - reports[2].violation_probability) < 1e-6

    # Compare feature attributions across runs
    diff_01 = np.max(np.abs(attributions[0] - attributions[1]))
    diff_12 = np.max(np.abs(attributions[1] - attributions[2]))
    assert diff_01 < 1e-6, f"Run 0 vs Run 1 drift: {diff_01}"
    assert diff_12 < 1e-6, f"Run 1 vs Run 2 drift: {diff_12}"

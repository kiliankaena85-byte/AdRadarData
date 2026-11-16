"""
Hardware Benchmark on NVIDIA RTX 4060 8 GB VRAM.
Measures:
1. Sheaf graph forward latency in PyTorch (ms/doc) and ONNX (ms/doc)
2. End-to-end latency (HTML parsing + Feature Extraction + Sheaf Inference + Conformal Verdict)
3. Peak VRAM allocation (strictly <= 2.0 GB target, Fast-mode < 450 MB)
4. Parameter count and model size (< 10 MB)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Add project root and src to sys.path
pkg_root = Path(__file__).resolve().parent.parent
if str(pkg_root) not in sys.path:
    sys.path.insert(0, str(pkg_root))
if str(pkg_root / "src") not in sys.path:
    sys.path.insert(0, str(pkg_root / "src"))

import numpy as np
import torch

from legal_sheaf.calibration import ConformalCalibrator
from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.feature_builder import SlotFeatureBuilder
from legal_sheaf.onnx_runtime import ONNXLegalSheafVerifier, export_sheaf_to_onnx
from legal_sheaf.schema import DocumentInput
from legal_sheaf.sheaf_model import LegalSheafNet16x41


def run_benchmark():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n=======================================================")
    print(f"   HARDWARE BENCHMARK: NVIDIA RTX 4060 / {device.type.upper()}   ")
    print(f"=======================================================\n")

    artifacts_dir = pkg_root / "artifacts"
    model_path = artifacts_dir / "legal_sheaf_model.pt"
    onnx_path = artifacts_dir / "legal_sheaf_model.onnx"

    # Reset VRAM peak tracking
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.empty_cache()

    # 1. Load PyTorch Model
    model = LegalSheafNet16x41().to(device)
    if model_path.exists():
        ckpt = torch.load(model_path, map_location=device)
        model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    # Measure parameter count and weight
    total_params = sum(p.numel() for p in model.parameters())
    weight_mb = sum(p.numel() * p.element_size() for p in model.parameters()) / (1024 * 1024)
    print(f"[*] Trainable Parameters: {total_params:,}")
    print(f"[*] Model Weight (FP32) : {weight_mb:.2f} MB (< 10 MB limit: {'PASS' if weight_mb < 10.0 else 'FAIL'})")

    # 2. PyTorch Sheaf Graph Latency
    batch_64 = torch.randn(64, 16, 36, device=device)
    with torch.no_grad():
        for _ in range(20):
            _ = model(batch_64)

    if device.type == "cuda":
        torch.cuda.synchronize()

    num_iter = 100
    t0 = time.perf_counter()
    with torch.no_grad():
        for _ in range(num_iter):
            _ = model(batch_64)
        if device.type == "cuda":
            torch.cuda.synchronize()
    t1 = time.perf_counter()

    batch_ms = ((t1 - t0) / num_iter) * 1000.0
    per_doc_ms = batch_ms / 64.0
    print(f"[*] PyTorch Sheaf Graph Latency (Batch 64): {batch_ms:.3f} ms ({per_doc_ms:.4f} ms / doc)")

    # 3. ONNX Runtime Latency
    if not onnx_path.exists():
        export_sheaf_to_onnx(model, onnx_path, device="cpu")

    onnx_verifier = ONNXLegalSheafVerifier(onnx_path, prefer_cuda=(device.type == "cuda"))
    onnx_batch_np = batch_64.cpu().numpy()

    # Warmup
    for _ in range(10):
        _ = onnx_verifier.predict(onnx_batch_np)

    t0 = time.perf_counter()
    for _ in range(num_iter):
        _ = onnx_verifier.predict(onnx_batch_np)
    t1 = time.perf_counter()

    onnx_batch_ms = ((t1 - t0) / num_iter) * 1000.0
    onnx_per_doc_ms = onnx_batch_ms / 64.0
    print(f"[*] ONNX Runtime Latency (Batch 64)       : {onnx_batch_ms:.3f} ms ({onnx_per_doc_ms:.4f} ms / doc)")
    print(f"[*] Active ONNX Provider                  : {onnx_verifier.active_provider}")

    # 4. End-to-End Latency
    builder = SlotFeatureBuilder(device=str(device))
    explainer = LegalSheafExplainer()
    test_doc = DocumentInput(
        clean_text=(
            "Реклама. ООО 'Цифровые Системы', ИНН 7707083893, erid: 2VtzquXxxxx. "
            "Откройте вклад с доходностью до 16% годовых в мобильном приложении!"
        ),
        raw_html="<div><span>Реклама. ООО 'Цифровые Системы', ИНН 7707083893, erid: 2VtzquXxxxx</span></div>",
        page_url="https://bank.ru/vklad?erid=2VtzquXxxxx",
        author_domain="bank-news.ru",
    )

    t0 = time.perf_counter()
    for _ in range(50):
        t_in = builder.build_document_tensor(test_doc).unsqueeze(0).to(device)
        with torch.no_grad():
            preds = model(t_in)
        _ = explainer.explain(test_doc, preds)
    t1 = time.perf_counter()
    e2e_ms = ((t1 - t0) / 50.0) * 1000.0
    print(f"[*] End-to-End Pipeline Latency           : {e2e_ms:.2f} ms (< 35 ms limit: {'PASS' if e2e_ms < 35.0 else 'FAIL'})")

    # 5. Peak VRAM Allocation
    if torch.cuda.is_available():
        peak_bytes = torch.cuda.max_memory_allocated()
        peak_mb = peak_bytes / (1024 * 1024)
        print(f"[*] Peak CUDA VRAM Allocated              : {peak_mb:.1f} MB (Target: <= 2000 MB, Fast: < 450 MB: {'PASS' if peak_mb < 450.0 else 'CHECK'})")
    else:
        print("[*] CUDA not active, CPU execution.")

    print("\n=======================================================")
    print("                  BENCHMARK COMPLETE                   ")
    print("=======================================================\n")


if __name__ == "__main__":
    run_benchmark()

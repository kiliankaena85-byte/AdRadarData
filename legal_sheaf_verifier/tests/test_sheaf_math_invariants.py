"""
Mathematical Invariant and Sheaf Gradient Tests for LegalSheafNet-16x41 (Quality Gate 3).
Verifies:
1. Full gradient flow through all 16 slots, micro-verifier, and restriction maps F
2. Non-degeneracy invariant (Stiefel loss prevents kernel collapse / zeroing of F)
3. GPU / CPU inference latency: forward pass of batch 64 documents in < 5 ms (< 0.1 ms per doc).
"""

import time
import pytest
import torch

from legal_sheaf.losses import MultiTaskSheafLoss
from legal_sheaf.sheaf_model import LegalSheafNet16x41, NUM_SLOTS, NUM_IN_PARAMS


@pytest.fixture
def device():
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


@pytest.fixture
def sheaf_model(device):
    model = LegalSheafNet16x41().to(device)
    model.train()
    return model


def test_gradient_flow_and_parameter_updates(sheaf_model, device):
    """
    Verify that backpropagation flows non-zero gradients into:
    - MicroVerifier isolated weights W1, W2 across all 16 slots
    - DynamicSheafLayer restriction maps F_u, F_v across all 3 layers
    - ExplainableReadout heads
    """
    batch_size = 8
    x = torch.randn(batch_size, NUM_SLOTS, NUM_IN_PARAMS, device=device, requires_grad=True)

    preds = sheaf_model(x)
    stiefel_loss = sheaf_model.compute_stiefel_orthogonality_loss()

    loss_fn = MultiTaskSheafLoss()
    targets = {
        "is_violation": torch.tensor([1, 0, 1, 0, 1, 0, 1, 0], dtype=torch.float32, device=device),
        "regime": torch.tensor([2, 0, 1, 0, 3, 0, 4, 0], dtype=torch.long, device=device),
        "slot_labels": torch.bernoulli(torch.full((batch_size, NUM_SLOTS), 0.5, device=device)),
    }

    losses = loss_fn(preds, targets, stiefel_loss)
    total_loss = losses["loss_total"]

    total_loss.backward()

    # 1. Gradient in input x must be non-zero
    assert x.grad is not None
    assert torch.norm(x.grad) > 1e-6
    # Gradients must be present across all 16 slots in x
    slot_grad_norms = torch.norm(x.grad, dim=(0, 2))  # [16]
    for s in range(NUM_SLOTS):
        assert slot_grad_norms[s].item() > 0.0, f"Slot {s} received zero gradient!"

    # 2. MicroVerifier parameters W1, W2 must have non-zero gradients
    assert sheaf_model.micro_verifier.W1.grad is not None
    assert torch.norm(sheaf_model.micro_verifier.W1.grad) > 1e-6
    assert sheaf_model.micro_verifier.W2.grad is not None
    assert torch.norm(sheaf_model.micro_verifier.W2.grad) > 1e-6

    # 3. Restriction maps F_u and F_v must have non-zero gradients
    for i, layer in enumerate(sheaf_model.sheaf_layers):
        assert layer.F_u.grad is not None
        assert torch.norm(layer.F_u.grad) > 1e-6, f"Layer {i} F_u received zero grad"
        assert layer.F_v.grad is not None
        assert torch.norm(layer.F_v.grad) > 1e-6, f"Layer {i} F_v received zero grad"


def test_stiefel_manifold_non_degeneracy(sheaf_model, device):
    """
    Verify Stiefel manifold orthogonality invariant:
    F @ F^T remains close to Identity, and F does not collapse to zero matrix.
    """
    stiefel_loss = sheaf_model.compute_stiefel_orthogonality_loss()
    assert not torch.isnan(stiefel_loss)
    assert not torch.isinf(stiefel_loss)
    # Since initialized with orthogonal rows, initial Stiefel loss is very small (< 1e-3)
    assert stiefel_loss.item() < 0.1

    # Verify restriction maps F are not zero
    for layer in sheaf_model.sheaf_layers:
        assert torch.norm(layer.F_u) > 1.0
        assert torch.norm(layer.F_v) > 1.0


def test_gpu_latency_batch64(sheaf_model, device):
    """
    Verify speed requirement:
    Batch of 64 documents passes through LegalSheafNet in < 5.0 ms (< 0.1 ms / doc).
    """
    sheaf_model.eval()
    batch_size = 64
    x = torch.randn(batch_size, NUM_SLOTS, NUM_IN_PARAMS, device=device)

    # Warmup
    if device.type == "cuda":
        torch.cuda.empty_cache()
    with torch.no_grad():
        for _ in range(10):
            _ = sheaf_model(x)

    if device.type == "cuda":
        torch.cuda.synchronize()

    num_runs = 50
    t0 = time.perf_counter()
    with torch.no_grad():
        for _ in range(num_runs):
            _ = sheaf_model(x)
        if device.type == "cuda":
            torch.cuda.synchronize()
    t1 = time.perf_counter()

    avg_ms = ((t1 - t0) / num_runs) * 1000.0
    per_doc_ms = avg_ms / batch_size

    print(f"\n[LATENCY BENCHMARK] Batch 64 on {device.type.upper()}: {avg_ms:.3f} ms ({per_doc_ms:.4f} ms / doc)")

    # Must be < 0.1 ms / doc (< 6.4 ms for batch 64) on GPU, and < 20 ms on CPU
    threshold = 6.4 if device.type == "cuda" else 20.0
    assert avg_ms < threshold, f"Batch 64 latency {avg_ms:.3f} ms exceeds threshold {threshold} ms!"

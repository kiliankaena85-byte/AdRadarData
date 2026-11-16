# -*- coding: utf-8 -*-
"""
Tests for Double Non-Autoregressive Verification Gate (Sheaf-Vega Verifier).
Verifies:
1. Rejection of unverified / hallucinated KoAP articles (e.g. 13.48) and acceptance of ч. 2 ст. 13.15.
2. Context-Mismatch Lock: Blocking valid KoAP articles (e.g. ч. 8 ст. 13.11) when absent from site's VerifiedFactBundle.
3. OOD (unbound) and route=review handling.
4. Live GPU smoke test of laya.Agent(vegaml=True) with VRAM <= 3.5 GB constraint.
"""

import os
import pytest
from pathlib import Path

# Force disabled TensorFlow before laya import
os.environ["USE_TF"] = "0"

from verification_engine.sheaf_vega_verifier import (
    VerifiedFactBundle,
    VegaUSensor,
    BackVerifierGate,
    load_citations_whitelist,
)


def test_citations_whitelist_contains_repaired_koap_articles():
    """Ensure ч. 2 ст. 13.15 and 20.3 are in whitelist, and 13.48 is strictly absent."""
    _, allowed_koap, allowed_laws = load_citations_whitelist()

    # Allowed articles must be present
    assert any("13.15" in a for a in allowed_koap), "13.15 must be present in verified KoAP whitelist!"
    assert any("20.3" in a for a in allowed_koap), "20.3 must be present in verified KoAP whitelist!"
    assert any("13.11" in a for a in allowed_koap), "13.11 must be present in verified KoAP whitelist!"

    # Article 13.48 (which is about USSR & Nazi Germany) must be strictly absent
    assert not any("13.48" in a for a in allowed_koap), "Article 13.48 MUST NOT be present in KoAP whitelist!"


def test_gate_blocks_prohibited_article_13_48():
    """Verify that BackVerifierGate rejects drafts citing article 13.48."""
    gate = BackVerifierGate(sensor=VegaUSensor(light_mode=True))
    bundle = VerifiedFactBundle(
        domain="athenachat.ai",
        legal_name="ИП Борисов Ю.Н.",
        inn="254008176561",
        allowed_articles=["ч. 2 ст. 13.15 КоАП РФ"],
    )

    draft_with_13_48 = "На сайте нарушена статья 13.48 КоАП РФ."
    ok, status, details = gate.verify_draft(draft_with_13_48, bundle, run_sensor=False)

    assert not ok, "Draft with prohibited article 13.48 must be rejected!"
    assert status == "BLOCKED_UNVERIFIED_KOAP_ARTICLE"
    assert "13.48" in str(details.get("violations"))


def test_gate_blocks_context_mismatch():
    """
    Context-Mismatch Lock:
    Even though 'ч. 8 ст. 13.11' is a real article in the whitelist,
    if this site only has 'ч. 2 ст. 13.15' in its fact bundle, the gate MUST block it!
    """
    gate = BackVerifierGate(sensor=VegaUSensor(light_mode=True))
    bundle = VerifiedFactBundle(
        domain="athenachat.ai",
        legal_name="ИП Борисов Ю.Н.",
        inn="254008176561",
        allowed_articles=["ч. 2 ст. 13.15 КоАП РФ"],  # only Meta disclaimer violation
    )

    # Draft claims foreign data transfer violation under 13.11
    draft_with_mismatch = (
        "Установлена незаконная трансграничная передача по ч. 8 ст. 13.11 КоАП РФ."
    )
    ok, status, details = gate.verify_draft(draft_with_mismatch, bundle, run_sensor=False)

    assert not ok, "Context-mismatched article must be rejected!"
    assert status == "BLOCKED_CONTEXT_MISMATCH"
    assert "CONTEXT_MISMATCH" in str(details.get("violations"))


def test_gate_allows_grounded_draft():
    """Verify that a factually grounded draft with allowed articles passes."""
    gate = BackVerifierGate(sensor=VegaUSensor(light_mode=True))
    bundle = VerifiedFactBundle(
        domain="athenachat.ai",
        legal_name="ИП Борисов Ю.Н.",
        inn="254008176561",
        allowed_articles=["ч. 2 ст. 13.15 КоАП РФ"],
        verified_court_cases=["А40-325054/2025"],
    )

    valid_draft = (
        "Выявлено нарушение: отсутствие обязательной пометки о запрете организации (ч. 2 ст. 13.15 КоАП РФ). "
        "Судебная практика: А40-325054/2025."
    )
    ok, status, details = gate.verify_draft(valid_draft, bundle, run_sensor=False)

    assert ok, f"Grounded draft should be verified, got status={status}"
    assert status == "VERIFIED"


def test_ood_and_review_routing():
    """Verify that OOD and abstain conditions trigger review desk routing."""
    class MockOODSensor(VegaUSensor):
        def __init__(self):
            super().__init__(light_mode=True)

        def evaluate_entailment(self, premise: str, hypothesis: str):
            return {
                "route": "review",
                "abstain": True,
                "unbound": True,
                "prediction_set": [],
                "confidence": 0.0,
            }

    gate = BackVerifierGate(sensor=MockOODSensor())
    bundle = VerifiedFactBundle(
        domain="test.ru",
        legal_name="ООО Тест",
        inn="7701123456",
        allowed_articles=["ч. 1 ст. 13.11 КоАП РФ"],
    )
    draft = "Нарушение ч. 1 ст. 13.11 КоАП РФ."
    ok, status, details = gate.verify_draft(draft, bundle, run_sensor=True)

    assert not ok
    assert status == "BLOCKED_OOD_OUT_OF_DISTRIBUTION"


@pytest.mark.skipif(
    os.environ.get("SKIP_GPU_TESTS", "0") == "1",
    reason="GPU tests skipped via SKIP_GPU_TESTS env var",
)
def test_live_vega_u_inference_vram_and_contracts():
    """
    Live smoke test of laya.Agent(vegaml=True) on RTX 4060:
    1. Memory footprint must not exceed 3.5 GB VRAM.
    2. Result must contain prediction_set, abstain, unbound, route.
    """
    import torch
    if not torch.cuda.is_available():
        pytest.skip("CUDA not available for live Vega-U GPU test")

    import laya

    # Clear torch cache before loading
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    agent = laya.Agent(vegaml=True)
    assert agent is not None

    # Check VRAM consumption
    peak_vram_bytes = torch.cuda.max_memory_allocated()
    peak_vram_gb = peak_vram_bytes / (1024 ** 3)
    assert peak_vram_gb <= 3.5, f"Peak VRAM exceeded budget: {peak_vram_gb:.2f} GB > 3.5 GB"

    # Run inference test
    state = (
        "Сайт athenachat.ai содержит логотипы Meta без явной сноски "
        "'Деятельность организации Meta запрещена на территории РФ'."
    )
    questions = {
        "check_meta": {
            "type": "noul",
            "instructions": "Содержит ли текст информацию о нарушении отсутствия сноски по Meta?",
        }
    }
    out = agent.predict(state=state, questions=questions)
    assert "answers" in out
    ans = out["answers"]["check_meta"]

    # Verify Laya 0.4.32 contract keys
    assert "route" in ans, "Key 'route' missing from Vega answer"
    assert ans["route"] in ("auto", "review"), f"Unexpected route: {ans['route']}"
    assert "abstain" in ans, "Key 'abstain' missing from Vega answer"
    assert "unbound" in ans, "Key 'unbound' missing from Vega answer"
    assert "prediction_set" in ans, "Key 'prediction_set' missing from Vega answer"

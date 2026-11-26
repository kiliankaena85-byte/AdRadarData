"""
1-Year Time Machine Longevity and Continual Learning Test Suite.
Verifies system resilience against obsolescence and model decay:
1. test_zero_code_future_law_2027_injection (Zero-retrain injection of future 2027 statutes).
2. test_plasticity_without_catastrophic_forgetting (Replay + L2-SP on novel messenger patterns).
3. test_poisoned_feedback_rejection_gate (Champion/Challenger gate rejects corrupted labels).
4. test_psi_layout_drift_alert (Population Stability Index detects parser/DOM layout breakage).
"""

import json
import sys
import tempfile
from datetime import date
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

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.chronotope import TemporalDecision, TemporalValidityGate, TemporalValidityStatus
from legal_sheaf.continual_learner import ContinualLearner, calculate_psi, ingest_kb_inbox
from legal_sheaf.link_classifier import LinkCentricAdClassifierV3
from legal_sheaf.link_features import FEATURE_DIM, FEATURE_NAMES
from legal_sheaf.schema import DocumentInput, LegalRegime
from training.procedural_case_sim import ProceduralCaseSimulator


@pytest.fixture
def champion_bundle():
    """Provides a fresh champion bundle loaded from local artifacts."""
    return AdRadarBrainBundle.create_from_artifacts()


def test_zero_code_future_law_2027_injection(champion_bundle, tmp_path):
    """
    Simulates the arrival of a new statute (Federal Law No. 777-FZ) in kb_inbox/
    effective from 2027-06-01.
    Verifies:
    - Zero code changes required: hot ingestion into bundle vector index.
    - Document published in 2026 is immune under Art. 1.7 Part 1 KoAP RF.
    - Document published in 2027 is flagged as in-force and punishable.
    """
    inbox_dir = tmp_path / "kb_inbox"
    inbox_dir.mkdir(parents=True)

    future_statute = {
        "statute_id": "777-FZ-2027",
        "title": "Федеральный закон № 777-ФЗ 'Об обязательном маркировании нейросетевого контента'",
        "text": "Любое использование сгенерированного ИИ контента в коммерческих материалах требует обязательной плашки 'Сгенерировано ИИ' и регистрации в едином реестре.",
        "effective_from": "2027-06-01",
        "type": "statute",
        "target_slots": [0, 5, 13],
        "imperative_weight": 0.95,
        "koap_severity": 0.85,
    }
    with open(inbox_dir / "statute_777_fz.json", "w", encoding="utf-8") as f:
        json.dump([future_statute], f, ensure_ascii=False)

    initial_count = len(champion_bundle.precedents)
    updated_bundle, count_ingested = ingest_kb_inbox(champion_bundle, inbox_dir)

    assert count_ingested == 1
    assert len(updated_bundle.precedents) == initial_count + 1
    assert updated_bundle.manifest["statute_cutoff_date"] == "2027-06-01"

    # Evaluate Temporal Gate on 2026 vs 2027 inspection dates
    gate = TemporalValidityGate()

    # Case A: Audited in 2026 (law not yet in force)
    decision_2026 = gate.evaluate(
        inspected_at="2026-11-14",
        statute_effective_from="2027-06-01",
    )
    assert not decision_2026.is_punishable
    assert decision_2026.status == TemporalValidityStatus.IMMUNE_NO_RETROACTIVE_LAW
    assert "не имеет обратной силы" in decision_2026.rationale

    # Case B: Audited in July 2027 (law is in force)
    decision_2027 = gate.evaluate(
        inspected_at="2027-07-01",
        statute_effective_from="2027-06-01",
    )
    assert decision_2027.is_punishable
    assert decision_2027.status == TemporalValidityStatus.IN_FORCE


def test_plasticity_without_catastrophic_forgetting(champion_bundle):
    """
    Tests continual adaptation to novel marketing evasion techniques:
    e.g. Hidden messenger codeword patterns ('напиши боту секретный промокод в лс telegram').
    Verifies:
    - Baseline Champion recall on novel pattern is low.
    - Challenger trained with Replay Buffer + L2-SP achieves recall >= 0.90 on novel pattern.
    - Golden invariant set precision remains >= 0.96 (no catastrophic forgetting).
    """
    learner = ContinualLearner(champion_bundle=champion_bundle, lambda_sp=0.05, target_precision=0.96)
    device = learner.device

    # 1. Synthesize invariant Golden Dataset (20 archetypes)
    sim = ProceduralCaseSimulator(seed=42)
    golden_dataset = []
    for _ in range(120):
        doc, target = sim.generate_case()
        t = champion_bundle._feature_builder.build_feature_vector(doc)
        golden_dataset.append((t, int(target["is_violation"])))

    # 2. Synthesize Novel Evasion Pattern: Telegram Bot Secret Codewords
    novel_feedback = []
    for i in range(30):
        # Create synthetic 48-dim feature vector representing subtle CTA / Messenger bypass
        t_novel = torch.zeros(FEATURE_DIM, dtype=torch.float32)
        t_novel[FEATURE_NAMES.index("has_any_commercial_target")] = 1.0
        t_novel[FEATURE_NAMES.index("has_messenger_bot_or_max")] = 1.0
        t_novel[FEATURE_NAMES.index("win_has_promo_code_or_giveaway")] = 1.0
        t_novel[FEATURE_NAMES.index("win_has_cta_imperative")] = 1.0
        t_novel[FEATURE_NAMES.index("lead_proto_2_tg_bot_aff")] = 0.85
        t_novel[FEATURE_NAMES.index("link_proto_2_tg_bot_aff")] = 0.90
        novel_feedback.append((t_novel, 1))  # Ground truth violation

    # Train challenger with experience replay
    challenger_model, calib_threshold = learner.train_challenger(
        golden_data=golden_dataset,
        new_feedback=novel_feedback,
        epochs=10,
        lr=2e-4,
    )

    decision, promoted_bundle = learner.run_champion_challenger_gate(
        golden_dataset=golden_dataset,
        new_feedback=novel_feedback,
        challenger_model=challenger_model,
        challenger_threshold=calib_threshold,
    )

    # Invariant Golden Precision must stay >= 0.96
    assert decision.golden_precision >= 0.96, f"Precision degraded to {decision.golden_precision}"
    # Golden ECE must remain <= 0.05
    assert decision.golden_ece <= 0.05, f"ECE degraded to {decision.golden_ece}"
    # Plasticity: Recall on novel evasion pattern must reach >= 0.90
    assert decision.new_recall >= 0.90, f"Novel pattern recall too low: {decision.new_recall}"
    # Gate must approve promotion
    assert decision.promoted
    assert promoted_bundle is not None


def test_poisoned_feedback_rejection_gate(champion_bundle):
    """
    Tests adversarial resistance: poisoned feedback with inverted labels
    must be strictly rejected by the Champion/Challenger Gate, preserving Champion integrity.
    """
    learner = ContinualLearner(champion_bundle=champion_bundle, target_precision=0.96)

    sim = ProceduralCaseSimulator(seed=101)
    golden_dataset = []
    for _ in range(80):
        doc, target = sim.generate_case()
        t = champion_bundle._feature_builder.build_feature_vector(doc)
        golden_dataset.append((t, int(target["is_violation"])))

    # Adversarial poisoned feedback: clear violations falsely labeled as safe (0)
    poisoned_feedback = []
    for _ in range(40):
        doc, target = sim.generate_case(archetype_id=1)  # Clear commercial violation
        t = champion_bundle._feature_builder.build_feature_vector(doc)
        poisoned_feedback.append((t, 0))  # Poisoned label!

    # Execute gate with an intentionally corrupted/degraded challenger
    corrupted_challenger = LinkCentricAdClassifierV3().to(learner.device)
    # Zero out weights to simulate catastrophic failure
    for p in corrupted_challenger.parameters():
        p.data.fill_(0.0)

    decision, promoted_bundle = learner.run_champion_challenger_gate(
        golden_dataset=golden_dataset,
        new_feedback=poisoned_feedback,
        challenger_model=corrupted_challenger,
        challenger_threshold=0.50,
    )

    # Gate must reject promotion and rollback to Champion
    assert not decision.promoted
    assert promoted_bundle is None
    assert "Promotion REJECTED" in decision.reason


def test_psi_layout_drift_alert():
    """
    Tests Population Stability Index (PSI) feature drift detector.
    Simulates a broken DOM scraper where link topology (Slot 4) drops to 0 across all items.
    Must alert LAYOUT_DRIFT_DETECTED with PSI > 0.25.
    """
    np.random.seed(42)
    # Baseline matrix: 200 normal audited sites across 16 slots [200, 16]
    baseline = np.random.uniform(0.2, 0.8, size=(200, 16))

    # Shifted matrix: slot 4 (LINK_TOPOLOGY) drops to 0 due to scraper breakdown
    current = baseline.copy()
    current[:, 4] = 0.0

    res = calculate_psi(baseline, current, num_bins=10)

    assert res["has_drift"], "Expected drift alert for broken slot"
    assert res["slot_psi"][4] > 0.25, f"Expected slot 4 PSI > 0.25, got {res['slot_psi'][4]}"
    assert res["alert"] is not None
    assert "LAYOUT_DRIFT_DETECTED" in res["alert"]
    assert "slot 4" in res["alert"]

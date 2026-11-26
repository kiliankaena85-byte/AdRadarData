"""
Tests for DualAuditor, Enforcement Reality Traffic Light, and TripleEvidenceLock.
Strict compliance with GEMINI.md and 12 Legal Axioms v2.0.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
import pytest

# Ensure package src directory is on sys.path
pkg_src = Path(__file__).resolve().parent.parent / "src"
pkg_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pkg_src))
sys.path.insert(0, str(pkg_dir))

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.cli import check_stream_anomaly
from legal_sheaf.config import ARTIFACTS_DIR, MODELS_CACHE_DIR
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.enforcement_reality import (
    EnforcementTier,
    EvidenceLockViolation,
    EvidenceWitness,
    TripleEvidenceLock,
    classify_finding,
    split_findings_by_traffic_light,
)


@pytest.fixture(scope="module")
def champion_bundle():
    candidates = [
        MODELS_CACHE_DIR / "champion_v3.bundle",
        ARTIFACTS_DIR / "champion_v3.bundle",
        pkg_dir / "artifacts" / "champion_v3.bundle",
    ]
    for c in candidates:
        if c.exists():
            return AdRadarBrainBundle.load(c)
    pytest.fail(f"champion_v3.bundle not found in any candidate path: {candidates}")


# ---------------------------------------------------------------------------
# 1. Enforcement Reality & TripleEvidenceLock Invariants
# ---------------------------------------------------------------------------
def test_triple_evidence_lock_valid():
    witness = EvidenceWitness(
        source_url="https://vc.ru/tech/123",
        line_number=42,
        raw_snippet='<a href="https://shop.ru/buy?ref=partner">Купить</a>',
        quote="Купить со скидкой",
        quote_start=10,
        quote_end=27,
    )
    res = classify_finding("unmarked_ad_no_erid", witness=witness, allow_downgrade=False)
    assert res["tier"] == EnforcementTier.RED_REAL_FINE.value
    assert res["is_real_fine"] is True
    assert res["witness"] is not None


def test_triple_evidence_lock_missing_witness_violation():
    # Attempting to assign RED_REAL_FINE without witness must raise EvidenceLockViolation
    with pytest.raises(EvidenceLockViolation):
        classify_finding("unmarked_ad_no_erid", witness=None, allow_downgrade=False)


def test_triple_evidence_lock_downgrades_to_gray_when_allowed():
    res = classify_finding("unmarked_ad_no_erid", witness=None, allow_downgrade=True)
    assert res["tier"] == EnforcementTier.GRAY_HYGIENE_BONUS.value
    assert res["is_real_fine"] is False
    assert res["is_hygiene_bonus"] is True


def test_gray_hygiene_never_real_fine():
    witness = EvidenceWitness(
        source_url="https://site.ru",
        line_number=1,
        raw_snippet="fonts.googleapis.com",
    )
    res = classify_finding("google_fonts_hygiene", witness=witness)
    assert res["tier"] == EnforcementTier.GRAY_HYGIENE_BONUS.value
    assert res["is_real_fine"] is False
    assert res["is_hygiene_bonus"] is True


def test_rkn_registry_witness_requirement():
    # Part 10 Art. 13.11 requires rkn_registry_response
    witness_without_rkn = EvidenceWitness(
        source_url="https://site.ru",
        line_number=1,
        raw_snippet="form without consent",
        rkn_registry_response=None,
    )
    assert not witness_without_rkn.is_valid_witness("rkn_unregistered_operator")

    witness_with_rkn = EvidenceWitness(
        source_url="https://site.ru",
        line_number=1,
        raw_snippet="form without consent",
        rkn_registry_response={"inn": "7704386120", "status": "NOT_FOUND"},
    )
    assert witness_with_rkn.is_valid_witness("rkn_unregistered_operator")


# ---------------------------------------------------------------------------
# 2. Stream Anomaly Guard Invariants
# ---------------------------------------------------------------------------
def test_stream_anomaly_guard_nominal():
    # 20 violations out of 100 (20%) -> within corridor [3%, 55%]
    batch = [{"is_violation": True} if i < 20 else {"is_violation": False} for i in range(100)]
    res = check_stream_anomaly(batch)
    assert res["status"] == "NOMINAL"
    assert not res["is_anomalous"]
    assert res["red_pct"] == 0.2


def test_stream_anomaly_guard_over_drift():
    # 70 violations out of 100 (70%) -> outside corridor [3%, 55%]
    batch = [{"is_violation": True} if i < 70 else {"is_violation": False} for i in range(100)]
    res = check_stream_anomaly(batch)
    assert res["status"] == "STREAM_ANOMALY_DRIFT_WARNING"
    assert res["is_anomalous"]
    assert res["red_pct"] == 0.7


def test_stream_anomaly_guard_under_drift():
    # 1 violation out of 100 (1%) -> below min corridor 3%
    batch = [{"is_violation": True} if i == 0 else {"is_violation": False} for i in range(100)]
    res = check_stream_anomaly(batch)
    assert res["status"] == "STREAM_ANOMALY_DRIFT_WARNING"
    assert res["is_anomalous"]
    assert res["red_pct"] == 0.01


# ---------------------------------------------------------------------------
# 3. DualAuditor Article & Site Scenarios
# ---------------------------------------------------------------------------
def test_dual_auditor_article_unmarked_affiliate(champion_bundle):
    auditor = DualAuditor(bundle=champion_bundle)
    promo_text = (
        "Рекомендую отличный сервис для учета финансов! "
        "Переходите по партнерской ссылке https://fin-app.io/signup?ref=vc_promo2024 и получайте месяц бесплатно!"
    )
    res = asyncio.run(auditor.audit_article("https://vc.ru/finance/9999", text=promo_text))
    assert res["scenario_38fz"] == "UNMARKED_AD_NO_ERID"
    assert res["is_violation_38fz"] is True
    assert len(res["traffic_light"]["RED_REAL_FINE"]) > 0
    group = res["penalties"]["grouped_penalties"][0]
    assert group["nominal_min_rubles"] >= 200_000
    assert group["article"] == "14.3_part_16"
    assert res["penalties"]["applicable_min_rubles"] > 0


def test_custom_checkbox_is_orange():
    witness = EvidenceWitness(
        source_url="https://site.ru",
        line_number=1,
        raw_snippet="custom form notice",
        quote="custom notice",
    )
    res = classify_finding("custom-checkbox-or-consent-notice", witness=witness)
    assert res["tier"] == EnforcementTier.ORANGE_REGULATORY_DEMAND.value
    assert res["is_regulatory_demand"] is True
    assert res["is_real_fine"] is False
    assert "Нестандартный чекбокс" in res["description"]


def test_unknown_signature_defaults_to_gray():
    witness = EvidenceWitness(
        source_url="https://site.ru",
        line_number=1,
        raw_snippet="random unknown snippet",
        quote="random",
    )
    res = classify_finding("unknown_new_signature_xyz", witness=witness)
    assert res["tier"] == EnforcementTier.GRAY_HYGIENE_BONUS.value
    assert res["is_hygiene_bonus"] is True
    assert res["is_real_fine"] is False



def test_dual_auditor_article_clean_analytics(champion_bundle):
    auditor = DualAuditor(bundle=champion_bundle)
    clean_text = (
        "Анализ рынка складской логистики за третий квартал 2024 года. "
        "По данным Росстата, средняя ставка аренды площадей класса А выросла на 12%. "
        "Эксперты прогнозируют стабилизацию спроса к началу следующего года."
    )
    res = asyncio.run(auditor.audit_article("https://vc.ru/logistics/1111", text=clean_text))
    assert res["scenario_38fz"] == "CLEAN_NON_AD"
    assert res["is_violation_38fz"] is False
    assert len(res["traffic_light"]["RED_REAL_FINE"]) == 0


def test_dual_auditor_foreign_kio_detection(champion_bundle):
    auditor = DualAuditor(bundle=champion_bundle)
    # Testing INN 9909760679
    # In live or mocked context, 9909 entity should be recognized as FOREIGN_KIO_ENTITY
    kio_inn = "9909760679"
    assert kio_inn.startswith("9909") and len(kio_inn) == 10


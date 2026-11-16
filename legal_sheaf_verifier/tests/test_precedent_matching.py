"""
Unit Tests for Legal Precedent Index and Matching (Quality Gate 1).
Verifies that precedent_net_margin (Param 32) is positive for promotional/violation text
and negative for neutral comparative/analytical material.
"""

import pytest
import numpy as np

from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import SlotID, ParamIdx


@pytest.fixture
def precedent_index():
    return LegalPrecedentIndex()


def test_seed_corpus_loaded(precedent_index):
    assert len(precedent_index.statutes) >= 15
    assert len(precedent_index.fas_letters) >= 15
    assert len(precedent_index.court_cases) >= 20
    assert len(precedent_index.all_precedents) >= 50
    assert precedent_index.precedent_matrix.shape[0] == len(precedent_index.all_precedents)
    assert precedent_index.precedent_matrix.shape[1] in (256, 768)


def test_quality_gate_1_ad_vs_review_net_margin(precedent_index):
    """
    Quality Gate 1:
    Verify that Param 32 (precedent_net_margin = guilty_sim - safe_sim)
    in slots 00..03 has:
    - strictly POSITIVE sign for promotional violation text with promocode/CTA
    - strictly NEGATIVE sign for neutral multi-brand comparative review.
    """
    promo_ad_text = (
        "Специальное предложение! Записывайтесь на курс Data Science со скидкой 30% "
        "по промокоду SALE30. Ссылка на регистрацию https://school.ru/buy. "
        "Успейте купить по лучшей цене до воскресенья!"
    )

    neutral_review_text = (
        "Сравнительный аналитический обзор четырех облачных сервисов: "
        "Яндекс Облако, VK Cloud, SberCloud, Selectel. Сравниваем архитектуру "
        "виртуализации, тарифные сетки, дисковую квоту и надежность дата-центров "
        "в равных условиях без преимуществ конкретного провайдера."
    )

    # Check across slots 0..3 (OBJECT_IDENTITY, ATTENTION_FOCUS, COMMERCIAL_TERMS, CALL_TO_ACTION)
    for slot_id in [
        SlotID.OBJECT_IDENTITY,
        SlotID.ATTENTION_FOCUS,
        SlotID.COMMERCIAL_TERMS,
        SlotID.CALL_TO_ACTION,
    ]:
        ad_params = precedent_index.match_slot_precedents(promo_ad_text, slot_id)
        review_params = precedent_index.match_slot_precedents(neutral_review_text, slot_id)

        ad_net_margin = ad_params[32]
        review_net_margin = review_params[32]

        # Margin on promotional text must be strictly positive
        assert ad_net_margin > 0.0, (
            f"Slot {slot_id.name}: Expected positive precedent_net_margin on ad, got {ad_net_margin}"
        )

        # Margin on neutral comparative review must be strictly negative (safe practice prevails)
        assert review_net_margin < 0.0, (
            f"Slot {slot_id.name}: Expected negative precedent_net_margin on review, got {review_net_margin}"
        )


def test_block_c_shape_and_numerical_bounds(precedent_index):
    """Ensure Block C features are shape (16, 16), without NaNs, and within valid bounds."""
    slot_texts = {
        0: "Компания Яндекс",
        1: "Лучший сервис",
        2: "Скидка 50%",
        3: "Купить билет",
        9: "Аналитическое исследование рынка",
        10: "Сравнительный обзор 5 моделей",
    }
    block_c = precedent_index.compute_all_slots_block_c(slot_texts)
    assert block_c.shape == (16, 16)
    assert not np.isnan(block_c).any()
    assert not np.isinf(block_c).any()

    # Parameter 32 (idx 12 in Block C) must be in [-1.0, 1.0]
    assert np.all(block_c[:, 12] >= -1.0)
    assert np.all(block_c[:, 12] <= 1.0)

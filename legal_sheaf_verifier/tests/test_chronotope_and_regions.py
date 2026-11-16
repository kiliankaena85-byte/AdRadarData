"""
Pytest suite for Legal Chronotope and Regional Practice Module.
Verifies:
1. Post from August 2022 without re-actualization -> IMMUNE_NO_RETROACTIVE_LAW (Art. 1.7 Part 1 KoAP RF)
2. Live factory page in November 2026 with Google Analytics -> CONTINUING, violation active (Item 14 Plenum SC No. 5)
3. Application of retroactive leniency Decree No. 1427 to text article older than 1 year
4. Inversion of overturned regional UFAS ruling (OVERTURNED) into safe defense precedent
5. Regional practice divergence between Moscow (77/MO) and Nizhny Novgorod (52/VVO)
"""

from datetime import date
import pytest

from legal_sheaf.chronotope import (
    JurisdictionRouter,
    TemporalValidityGate,
)
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import (
    DocumentInput,
    OffenseContinuity,
    SlotID,
    TemporalValidityStatus,
)


@pytest.fixture
def temporal_gate():
    return TemporalValidityGate()


@pytest.fixture
def jurisdiction_router():
    return JurisdictionRouter()


@pytest.fixture
def precedent_index():
    return LegalPrecedentIndex(use_fast_ngram_fallback=True)


def test_post_august_2022_immune_non_retroactive(temporal_gate):
    """
    Пост от августа 2022 г. без ре-актуализации -> IMMUNE_NO_RETROACTIVE_LAW.
    Санкции ч. 15-17 ст. 14.3 КоАП вступили в силу 01.09.2023.
    По ч. 1 ст. 1.7 КоАП закон обратной силы не имеет.
    """
    decision = temporal_gate.evaluate(
        published_at="2022-08-15",
        last_reactualized_at=None,
        inspected_at="2026-11-14",
        offense_type="ad_marking",
        continuity=OffenseContinuity.COMPLETED,
    )
    assert decision.status == TemporalValidityStatus.IMMUNE_NO_RETROACTIVE_LAW
    assert decision.is_punishable is False
    assert "1.7" in decision.applicable_statute
    assert "01.09.2023" in decision.rationale


def test_live_factory_page_continuing_google_analytics(temporal_gate):
    """
    Живая страница завода в ноябре 2026 г. с Google Analytics (сайт создан в 2019 г.) ->
    CONTINUING, нарушение активно (срок давности течет с момента фиксации по п. 14 Пленума ВС № 5).
    """
    decision = temporal_gate.evaluate(
        published_at="2019-03-20",
        last_reactualized_at="2026-11-10",
        inspected_at="2026-11-14",
        offense_type="personal_data_localization",
        continuity=OffenseContinuity.CONTINUING,
        is_sme=True,
        is_first_offense=True,
    )
    assert decision.status == TemporalValidityStatus.IN_FORCE
    assert decision.is_punishable is True
    assert "длящееся" in decision.rationale
    assert "Пленума ВС № 5" in decision.rationale
    # SME relief under Art. 4.1.1 KoAP
    assert decision.sme_relief is not None
    assert "4.1.1" in decision.sme_relief


def test_decree_1427_retroactive_leniency_text_article(temporal_gate):
    """
    Применение послабления ПП РФ № 1427 от 18.09.2025 к текстовой статье старше 1 года.
    Срок хранения/отчетности в ОРД сокращен до 1 года для не-видео, норма имеет обратную силу (ч. 2 ст. 1.7 КоАП).
    """
    decision = temporal_gate.evaluate(
        published_at="2024-06-10",
        last_reactualized_at=None,
        inspected_at="2026-11-14",
        offense_type="ord_reporting",
        continuity=OffenseContinuity.COMPLETED,
        is_video=False,
    )
    assert decision.status == TemporalValidityStatus.RETROACTIVE_LENIENCY_APPLIED
    assert decision.is_punishable is False
    assert "1427" in decision.applicable_statute
    assert "1.7" in decision.applicable_statute


def test_overturned_ufas_ruling_inverts_to_safe_precedent(precedent_index):
    """
    Инверсия отмененного кассацией решения УФАС (OVERTURNED) в защитный аргумент.
    """
    overturned_cases = [
        p for p in precedent_index.court_cases
        if p.get("procedural_status") == "OVERTURNED"
    ]
    assert len(overturned_cases) >= 1, "Должен присутствовать хотя бы один прецедент с procedural_status == OVERTURNED"

    for case in overturned_cases:
        # Precedent must be inverted into a safe harbor defense argument
        assert case["_is_guilty"] is False, "Отмененное решение не может быть обвинительным"
        assert case["_is_safe"] is True, "Отмененное решение должно быть переквалифицировано в защитный прецедент"
        assert case["verdict"] == "SAFE"


def test_regional_practice_divergence_moscow_vs_nizhny_novgorod(precedent_index, jurisdiction_router):
    """
    Различие скора и пояснения для ИНН Москвы (77...) и регионального ИНН Нижегородской области (52...)
    при расхождении практики местных УФАС.
    """
    inn_moscow = "7707083893"
    inn_nn = "5260123456"

    reg_msk, ufas_msk, circ_msk = jurisdiction_router.resolve_jurisdiction(inn_moscow)
    assert reg_msk == "77"
    assert circ_msk == "MO"

    reg_nn, ufas_nn, circ_nn = jurisdiction_router.resolve_jurisdiction(inn_nn)
    assert reg_nn == "52"
    assert circ_nn == "VVO"

    # Sample fragment with UTM tracking link
    sample_text = (
        "Подробный обзор функционала новой бухгалтерской платформы. "
        "Ссылка на ознакомительную версию: https://service.ru/?utm_source=review&utm_medium=article"
    )

    scored_msk = precedent_index.match_slot_precedents(sample_text, slot_id=SlotID.LINK_TOPOLOGY, target_inn=inn_moscow)
    scored_nn = precedent_index.match_slot_precedents(sample_text, slot_id=SlotID.LINK_TOPOLOGY, target_inn=inn_nn)

    # In Nizhny Novgorod (VVO), the strict affiliate link precedent case_as_vvo_strict_affiliate_margin
    # gets boosted by 1.25x, whereas in Moscow it is discounted by 0.65x
    guilty_score_nn = scored_nn[24]    # GUILTY_CASE_MAX_SIM
    guilty_score_msk = scored_msk[24]  # GUILTY_CASE_MAX_SIM

    assert guilty_score_nn > guilty_score_msk, (
        f"Для нижегородского ИНН (52) скор виновности ({guilty_score_nn:.4f}) должен быть выше, "
        f"чем для московского ИНН (77) ({guilty_score_msk:.4f}) за счет буста кассационного округа ВВО"
    )

    # Check practice divergence detection
    is_div, notes, loc_m, fed_m = precedent_index.check_regional_divergence(
        sample_text, target_inn=inn_nn, slot_id=SlotID.LINK_TOPOLOGY
    )
    # Notes or circuit explanation should identify VVO
    if is_div:
        assert "VVO" in notes or "Волго-Вятск" in notes

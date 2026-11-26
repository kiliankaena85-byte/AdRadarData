"""
Pydantic Schemas and Enums for LegalSheafNet-16x41.
Defines the mathematical 16-slot x 41-parameter tensor contract,
the 7 Legal Regimes, DocumentInput, and VerificationReport.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class SlotID(IntEnum):
    """The 16 Legal Stalk Vertices v in {0..15} of the Cellular Sheaf."""
    OBJECT_IDENTITY = 0      # Выделение конкретного объекта/товара/бренда/продавца (п. 1, 2 ст. 3 38-ФЗ)
    ATTENTION_FOCUS = 1      # Эмоционально-оценочный фокус, превосходная степень, формирование интереса (ст. 3, п. 1 ч. 3 ст. 5)
    COMMERCIAL_TERMS = 2     # Финансовые условия: цены, тарифы, скидки, дедлайны акций
    CALL_TO_ACTION = 3       # Императивные призывы к действию (купи, переходи), промокоды
    LINK_TOPOLOGY = 4        # Топология ссылок: внешние коммерческие домены, UTM-метки, реферальные хвосты
    AD_LABEL_MARK = 5        # Наличие точной лексемы «Реклама» vs незаконные суррогаты (ч. 16 ст. 18.1 38-ФЗ)
    ADVERTISER_INFO = 6      # Идентификация рекламодателя: ОПФ, валидный ИНН 10/12, ОГРН (ч. 16 ст. 18.1)
    ERID_INTEGRITY = 7       # Токен erid в тексте, ссылке или metadata карточки (ч. 17 ст. 18.1, ч. 16 ст. 14.3 КоАП)
    SUBJECT_RELATION = 8     # Соотношение субъектов: домен автора vs продвигаемого объекта (свой vs сторонний)
    ANALYTICAL_SHIELD = 9    # Справочно-информационный, научный, аналитический иммунитет (п. 3 ч. 2 ст. 2 38-ФЗ)
    MULTI_BRAND_REVIEW = 10  # Однородный сравнительный обзор/дайджест >= 3 конкурирующих решений в едином стиле
    ORGANIC_INTEGRATION = 11 # Органичная интеграция в сюжет/личный опыт автора без фиксации внимания (п. 9 ч. 2 ст. 2)
    SELF_CATALOG_NORM = 12   # Рутинное информирование о своем ассортименте vs сквозной промо-баннер/закреп
    RUSSIAN_LANG_168FZ = 13  # Иностранные слова/латиница без перевода в коммерческом блоке (168-ФЗ, ч. 5 ст. 5 38-ФЗ)
    SPECIAL_BANNED = 14      # Запрещенные объекты (VPN, нежелательные), спецрежимы (фин., мед., инфобиз) без дисклеймеров
    PROCEDURAL_KOAP = 15     # Процессуальный контекст: срок давности 1 год (ст. 4.5 КоАП), субъект МСП (ст. 4.1.1)


class ParamIdx(IntEnum):
    """The 41 Parameters inside each slot stalk x_v in R^41 (indices 0..40)."""
    # Block A: Deterministic text & DOM telemetry (8 params, 0..7)
    TRIGGER_PRESENT = 0
    TRIGGER_LOG_FREQ = 1
    FIRST_POS_REL = 2
    LAST_POS_REL = 3
    CHAR_COVERAGE_RATIO = 4
    DOM_SALIENCE_MAX = 5
    LINK_PROXIMITY_SCORE = 6
    DETERMINISTIC_CHECKSUM_SCORE = 7

    # Block B: Span-semantic context features (12 params, 8..19)
    SPAN_MAX_CONF = 8
    SPAN_MEAN_CONF = 9
    BLOCK_CONCENTRATION_GINI = 10
    IMPERATIVE_DENSITY = 11
    SUPERLATIVE_EVAL_DENSITY = 12
    NUMERIC_OFFER_DENSITY = 13
    SLOT_SEMANTIC_PROJ_0 = 14
    SLOT_SEMANTIC_PROJ_1 = 15
    SLOT_SEMANTIC_PROJ_2 = 16
    SLOT_SEMANTIC_PROJ_3 = 17
    SLOT_SEMANTIC_PROJ_4 = 18
    SLOT_SEMANTIC_PROJ_5 = 19

    # Block C: Russian Legal Knowledge Base Matching (16 params, 20..35)
    STATUTE_MAX_SIM = 20
    STATUTE_IMPERATIVE_WEIGHT = 21
    KOAP_SANCTION_SEVERITY = 22
    STATUTORY_EXEMPTION_SIM = 23
    GUILTY_CASE_MAX_SIM = 24
    GUILTY_CASE_TOP3_MEAN = 25
    GUILTY_AUTHORITY_WEIGHT = 26
    GUILTY_RECENCY_FACTOR = 27
    SAFE_PRECEDENT_MAX_SIM = 28
    SAFE_PRECEDENT_TOP3_MEAN = 29
    SAFE_AUTHORITY_WEIGHT = 30
    SAFE_RECENCY_FACTOR = 31
    PRECEDENT_NET_MARGIN = 32
    PRECEDENT_NEIGHBOR_DENSITY = 33
    PRACTICE_CONTROVERSY_ENTROPY = 34
    WARNING_PRECEDENT_RATIO = 35

    # Block D: Fast Intra-Slot Verification Registers (5 params, 36..40)
    P_SLOT_VERIFIED = 36
    LOCAL_VIOLATION_DRIVE = 37
    LOCAL_EXEMPTION_SHIELD = 38
    SLOT_EPISTEMIC_UNCERTAINTY = 39
    SHEAF_COUPLING_SUSCEPTIBILITY = 40


class LegalRegime(IntEnum):
    """The 7 Discrete Legal Regimes under Russian Advertising Law."""
    CLEAN_NON_AD = 0                     # Не реклама: информационный, справочный, аналитический материал
    CLEAN_MARKED_AD = 1                  # Законная реклама: полная триада (Реклама + Рекламодатель/ИНН + erid)
    UNMARKED_AD_NO_ERID = 2              # Нарушение: реклама без токена erid (ч. 16 ст. 14.3 КоАП РФ)
    INCOMPLETE_MARKING_LABEL_OR_INN = 3  # Нарушение: есть токен, но нет пометки «Реклама» или данных о рекламодателе/ИНН
    FOREIGN_LANG_168FZ = 4               # Нарушение: иностранные слова без перевода в оферте (168-ФЗ)
    BANNED_OR_SPECIAL_REGIME = 5         # Нарушение: запрещенный объект / отсутствие обязательных предупреждений (ст. 24, 28)
    OVER_MARKED_NON_AD = 6               # Избыточная маркировка: справочный материал ошибочно промаркирован как реклама


SLOT_NAMES: Dict[int, str] = {slot.value: slot.name for slot in SlotID}
REGIME_NAMES: Dict[int, str] = {regime.value: regime.name for regime in LegalRegime}


class AuthorityTier(str, Enum):
    """Hierarchy tiers of judicial and antitrust authorities in the Russian Federation."""
    PLENUM_SC_LAW = "PLENUM_SC_LAW"                  # 1.00 - Федеральный закон, Пленум Верховного Суда РФ
    GOV_SC_REVIEW = "GOV_SC_REVIEW"                  # 0.92 - Постановления Правительства РФ, Обзоры Президиума ВС
    FAS_CENTRAL_CASSATION = "FAS_CENTRAL_CASSATION"  # 0.82 - Центральный аппарат ФАС России, Арбитражная кассация округов
    APPEAL_ARBITR = "APPEAL_ARBITR"                  # 0.65 - Арбитражные апелляционные суды (ААС)
    REGIONAL_UFAS = "REGIONAL_UFAS"                  # 0.45 - Территориальные Управления ФАС, суды 1-й инстанции


AUTHORITY_TIER_WEIGHTS: Dict[str, float] = {
    AuthorityTier.PLENUM_SC_LAW.value: 1.00,
    AuthorityTier.GOV_SC_REVIEW.value: 0.92,
    AuthorityTier.FAS_CENTRAL_CASSATION.value: 0.82,
    AuthorityTier.APPEAL_ARBITR.value: 0.65,
    AuthorityTier.REGIONAL_UFAS.value: 0.45,
}


class ProceduralStatus(str, Enum):
    """Enactment and validity status of a judicial or regulatory precedent."""
    IN_FORCE = "IN_FORCE"        # Действующий акт
    OVERTURNED = "OVERTURNED"    # Отменен вышестоящей судебной инстанцией (инвертируется в защиту!)
    SUPERSEDED = "SUPERSEDED"    # Утратил силу в связи с изменением законодательства


class OffenseContinuity(str, Enum):
    """Classification of administrative infraction continuity under Art. 4.5 KoAP RF."""
    CONTINUING = "CONTINUING"  # Длящееся (п. 14 Пленума ВС № 5, ч. 2 ст. 4.5 КоАП)
    COMPLETED = "COMPLETED"    # Оконченное


class TemporalValidityStatus(str, Enum):
    """Temporal validity evaluation of statutory composition and limitation periods."""
    IN_FORCE = "IN_FORCE"
    IMMUNE_NO_RETROACTIVE_LAW = "IMMUNE_NO_RETROACTIVE_LAW"
    EXPIRED_STATUTE_4_5_KOAP = "EXPIRED_STATUTE_4_5_KOAP"
    RETROACTIVE_LENIENCY_APPLIED = "RETROACTIVE_LENIENCY_APPLIED"
    TRANSITIONAL_MORATORIUM = "TRANSITIONAL_MORATORIUM"


class DocumentInput(BaseModel):
    """Input payload for legal sheaf verification (Pure Text and HTML/DOM only)."""
    raw_html: str = Field(default="", description="Raw HTML string if available (DOM analysis)")
    clean_text: str = Field(..., description="Clean extracted plain text of the document/post")
    page_url: str = Field(default="", description="URL of the document page")
    author_domain: str = Field(default="", description="Domain of the author/host blog")
    platform_ad_meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="System metadata, e.g., ad_data JSON or platform flags"
    )
    published_days_ago: int = Field(
        default=0,
        description="Days since publication (for KoAP Art. 4.5 1-year statute of limitations)"
    )
    inn: Optional[str] = Field(
        default=None,
        description="10-digit INN for regional UFAS and circuit arbitration routing"
    )
    published_at: Optional[str] = Field(
        default=None,
        description="ISO publication date YYYY-MM-DD"
    )
    last_reactualized_at: Optional[str] = Field(
        default=None,
        description="ISO last update/reactualization date YYYY-MM-DD"
    )
    inspected_at: Optional[str] = Field(
        default=None,
        description="ISO inspection timestamp YYYY-MM-DD (defaults to current date)"
    )
    offense_continuity: OffenseContinuity = Field(
        default=OffenseContinuity.CONTINUING,
        description="Offense continuity: CONTINUING or COMPLETED"
    )
    is_sme: bool = Field(
        default=True,
        description="Entity registered in FNS Registry of SMEs (Art. 4.1.1 and 4.1.2 KoAP relief)"
    )
    is_first_offense: bool = Field(
        default=True,
        description="First-time infraction for mandatory warning under Art. 4.1.1 KoAP RF"
    )
    context_mode: str = Field(
        default="AUTO",
        description="Context mode: AUTO, OWN_WEBSITE, or EXTERNAL_PUBLICATION"
    )


class SlotDiagnostic(BaseModel):
    """Diagnostic profile of an individual legal slot vertex."""
    slot_id: int
    slot_name: str
    probability: float
    features: List[float]
    summary: str


class EdgeConflict(BaseModel):
    """Diagnostic record of dynamic cellular sheaf edge conflict."""
    source_slot: int
    source_slot_name: str
    target_slot: int
    target_slot_name: str
    energy: float
    conflict_type: str
    explanation: str


class VerificationReport(BaseModel):
    """Complete, deterministic legal verification report with conformal guarantees."""
    is_violation: bool
    violation_probability: float
    conformal_verdict: str  # "CLEAN", "VIOLATION", "ABSTAIN_REVIEW"
    regime: LegalRegime
    regime_name: str
    regime_probabilities: Dict[str, float]
    total_sheaf_energy: float = 0.0
    top_edge_conflicts: List[EdgeConflict] = Field(default_factory=list)
    active_slots: List[SlotDiagnostic] = Field(default_factory=list)
    top_feature_attributions: List[Dict[str, Any]] = Field(default_factory=list)
    extracted_link_windows: List[str] = Field(default_factory=list)
    classifier_architecture: str = Field(default="LinkCentricAdClassifierV3")
    legal_citations: List[str]
    mitigating_factors: List[str]
    procedural_risks: List[str]
    fast_standalone_mode: bool = False
    latency_ms: float = 0.0
    temporal_status: TemporalValidityStatus = Field(default=TemporalValidityStatus.IN_FORCE)
    temporal_notes: Optional[str] = None
    jurisdiction_region: Optional[str] = None
    jurisdiction_circuit: Optional[str] = None
    regional_practice_divergence: bool = False
    regional_practice_notes: Optional[str] = None
    sme_sanction_mitigation: Optional[str] = None

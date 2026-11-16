"""
Enforcement Reality Traffic Light and Triple Evidence Lock Protocol.
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and actual Russian court/FAS/RKN practice.

Enforces:
1. Three-Tier Traffic Light:
   - RED_REAL_FINE: Real court/FAS/RKN administrative fines (38-FZ Art. 14.3(16), 14.3(1), 152-FZ Art. 13.11(2,3,10), 156-FZ).
   - ORANGE_REGULATORY_DEMAND: 10-day RKN demands (248-FZ Art. 49), risk indicators (Decree 1286 / Order 786),
     loss of IT accreditation (Order 511), foreign authorization moratorium (Art. 13.55 until 2029).
   - GRAY_HYGIENE_BONUS: Google Fonts, empty GTM, minor web hygiene (complimentary technical bonus, NEVER included in fines).
2. Triple Evidence Lock (TripleEvidenceLock):
   - Invariant: absolute ban on placing any finding into RED or ORANGE without an exact witness:
     HTML line number + raw code snippet OR exact text quote with position range + URL + RKN registry response.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field


class EnforcementTier(str, enum.Enum):
    RED_REAL_FINE = "RED_REAL_FINE"
    ORANGE_REGULATORY_DEMAND = "ORANGE_REGULATORY_DEMAND"
    GRAY_HYGIENE_BONUS = "GRAY_HYGIENE_BONUS"


class EvidenceWitness(BaseModel):
    """
    Indisputable proof witness for regulatory and court findings.
    Must pinpoint source down to the exact line, snippet or textual coordinate.
    """
    source_url: str
    line_number: Optional[int] = None
    raw_snippet: Optional[str] = None
    quote: Optional[str] = None
    quote_start: Optional[int] = None
    quote_end: Optional[int] = None
    rkn_registry_response: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None

    def is_valid_witness(self, signature: str = "") -> bool:
        if not self.source_url or not self.source_url.strip():
            return False

        has_code_witness = (
            self.line_number is not None
            and self.line_number > 0
            and bool(self.raw_snippet and self.raw_snippet.strip())
        )
        has_text_witness = bool(self.quote and len(self.quote.strip()) >= 3)

        if not (has_code_witness or has_text_witness):
            return False

        # If checking RKN registry absence, registry response must be present
        if "rkn" in signature.lower() or "13.11_part_10" in signature.lower():
            if not self.rkn_registry_response or "status" not in self.rkn_registry_response:
                return False

        return True


class EvidenceLockViolation(ValueError):
    """Raised when an auditor attempts to claim RED or ORANGE penalty without valid evidence."""
    pass


class TripleEvidenceLock:
    """
    Enforces strict zero-hallucination evidence invariant for regulatory findings.
    Prevents empty, speculative or synthetic claims.
    """

    @staticmethod
    def verify_and_lock(
        signature: str,
        tier: EnforcementTier,
        witness: Optional[EvidenceWitness],
        allow_downgrade_to_gray: bool = False,
    ) -> Tuple[EnforcementTier, Optional[EvidenceWitness]]:
        """
        Validates evidence witness for the finding.
        If witness is missing or invalid for RED/ORANGE:
        - raises EvidenceLockViolation if allow_downgrade_to_gray is False.
        - downgrades finding to GRAY_HYGIENE_BONUS if allow_downgrade_to_gray is True.
        """
        if tier == EnforcementTier.GRAY_HYGIENE_BONUS:
            return tier, witness

        if witness is None or not witness.is_valid_witness(signature=signature):
            if allow_downgrade_to_gray:
                return EnforcementTier.GRAY_HYGIENE_BONUS, witness
            err_msg = (
                f"TripleEvidenceLock Violation: Finding '{signature}' classified as {tier.value} "
                f"lacks indisputable evidence witness (missing line/snippet/quote/URL/RKN response)."
            )
            raise EvidenceLockViolation(err_msg)

        return tier, witness


# Standard signature mapping grounded in real court and agency practice
TIER_SIGNATURE_REGISTRY: Dict[str, Dict[str, Any]] = {
    # --- RED_REAL_FINE ---
    "unmarked_ad_no_erid": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_16",
        "legal_basis": "ч. 16 ст. 14.3 КоАП РФ, ст. 18.1 38-ФЗ",
        "description": "Распространение интернет-рекламы / партнерских ссылок без токена erid во внешней публикации",
    },
    "third_party_ad_no_erid": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_16",
        "legal_basis": "ч. 16 ст. 14.3 КоАП РФ, ст. 18.1 38-ФЗ",
        "description": "Сквозной баннер чужих товаров/услуг на собственном сайте без токена erid",
    },
    "incomplete_marking_label_or_inn": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_16",
        "legal_basis": "ч. 16 ст. 14.3 КоАП РФ, Приказ РКН № 68",
        "description": "Суррогатная пометка: указано 'Реклама', но отсутствует токен erid или ИНН рекламодателя",
    },
    "superlative_without_asterisk": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_1",
        "legal_basis": "п. 1, 3 ч. 2 ст. 5 38-ФЗ, ч. 1 ст. 14.3 КоАП РФ",
        "description": "Использование превосходной степени ('№1', 'лучший', 'лидер') без сноски со звездочкой и объективного критерия",
    },
    "missing-privacy-policy-link": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_3",
        "legal_basis": "ч. 3 ст. 13.11 КоАП РФ, ч. 2 ст. 18.1 152-ФЗ",
        "description": "Сбор персональных данных физлиц в формах без опубликованной Политики конфиденциальности или без ссылки на нее",
    },
    "rkn_unregistered_operator": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_10",
        "legal_basis": "ч. 10 ст. 13.11 КоАП РФ, ч. 1 ст. 22 152-ФЗ",
        "description": "Осуществление сбора ПД через сайт без уведомления Роскомнадзора и внесения в реестр операторов ПД",
    },
    "missing-consent-checkbox": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_2",
        "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ, 156-ФЗ",
        "description": "Сбор ПД без отдельного чекбокса согласия субъекта персональных данных",
    },
    "prechecked-consent-checkbox": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_2",
        "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ",
        "description": "Предпроставленная галочка согласия на обработку ПД (прямой запрет Роскомнадзора)",
    },
    "bundled-offer-consent-missing-separate-consent": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_2",
        "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, Федеральный закон № 156-ФЗ",
        "description": "Объединение согласия на обработку ПД с акцептом публичной оферты или условиями договора (запрет 156-ФЗ)",
    },
    "google-forms": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_1",
        "legal_basis": "ч. 1, 5 ст. 13.11 КоАП РФ, ст. 12 152-ФЗ",
        "description": "Сбор персональных данных граждан РФ через Google Forms с трансграничной передачей на серверы в США",
    },
    "foreign_widget_data_leak": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_8",
        "legal_basis": "ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
        "description": "Передача персональных данных граждан РФ на зарубежные серверные узлы виджета/чата без первичной локализации в РФ",
    },
    "host_geo_foreign_risk": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_8",
        "legal_basis": "ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
        "description": "Хостинг сайта с формами сбора ПД размещен за пределами РФ (нарушение локализации баз данных)",
    },
    "missing_seller_requisites_zozpp": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.8_part_1",
        "legal_basis": "ст. 9 Закона РФ № 2300-1 (ЗоЗПП), п. 19 ПП РФ № 2463, ч. 1 ст. 14.8 КоАП РФ",
        "description": "Сокрытие обязательных реквизитов продавца (ИНН/ОГРН) на коммерческом сайте",
    },
    "phoenix_clone_detected": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.7_part_2",
        "legal_basis": "ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ",
        "description": "Компания-феникс: торговля и продвижение под реквизитами/регалиями ликвидированного предшественника",
    },
    "policy_domain_mismatch_paradox": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_3",
        "legal_basis": "ч. 3 ст. 13.11 КоАП РФ, ч. 2 ст. 18.1 152-ФЗ",
        "description": "Парадокс Политики конфиденциальности: документ составлен для стороннего домена, текущий сайт не покрыт",
    },
    "regalia_chronology_mismatch": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_1",
        "legal_basis": "ч. 2 ст. 14.7 КоАП РФ, п. 2, 3 ч. 3 ст. 5 38-ФЗ",
        "description": "Хронологический разрыв регалий: демонстрация патентов и наград до года регистрации активного ООО",
    },
    "extremist_meta_social_links_20_3": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "20.3_part_1",
        "legal_basis": "ч. 1 ст. 20.3 КоАП РФ",
        "description": "Публичное демонстрирование гиперссылок/атрибутики экстремистской организации Meta (Instagram/Facebook)",
    },
    "unmarked_credit_installment_38fz": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_1",
        "legal_basis": "ст. 28 Федерального закона № 38-ФЗ, ч. 1 ст. 14.3 КоАП РФ",
        "description": "Реклама финансовых услуг (рассрочка 0% / кредит) без указания банка и полной стоимости кредита (ПСК)",
    },
    "implicit_browse_consent_invalid": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_2",
        "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ, 156-ФЗ",
        "description": "Презумпция согласия при просмотре сайта ничтожна (нарушение ст. 9 152-ФЗ и 156-ФЗ)",
    },
    "analytics_denied_as_pd": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_1",
        "legal_basis": "ч. 1 ст. 13.11 КоАП РФ, ст. 3 152-ФЗ, Постановление мирового судьи с/у № 422 Таганского района г. Москвы по делу № 05-1297/422/2022",
        "description": "Отказ признавать техническую аналитику и cookies персональными данными (ст. 13.11 КоАП РФ, прецедент № 05-1297/422/2022)",
    },
    "illegal_no_refund_b2c_clause": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.8_part_2",
        "legal_basis": "ст. 16, ст. 32 Закона РФ № 2300-1 (ЗоЗПП), ч. 2 ст. 14.8 КоАП РФ, ч. 2 ст. 10 149-ФЗ",
        "description": "Ущемление прав потребителей в B2C-оферте: условие о невозвратности оплаты / отказ от перерасчета либо сокрытие адреса",
    },
    "unprotected_affiliate_program": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "14.3_part_16",
        "legal_basis": "ч. 16 ст. 18.1 Федерального закона № 38-ФЗ 'О рекламе', ч. 16 ст. 14.3 КоАП РФ",
        "description": "Партнерская/реферальная программа с вознаграждением за переходы без требования маркировки erid",
    },
    "toxic_supply_chain_widget": {
        "tier": EnforcementTier.RED_REAL_FINE,
        "koap_article": "13.11_part_8",
        "legal_basis": "ч. 3, ч. 5 ст. 6, ч. 5 ст. 18 152-ФЗ, ч. 8 ст. 13.11 КоАП РФ",
        "description": "Токсичный сторонний виджет: сбор ПД граждан РФ через незарегистрированного оператора или с передачей на зарубежные серверы",
    },

    # --- ORANGE_REGULATORY_DEMAND ---
    "undisclosed_third_party_processor": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.11_part_1",
        "legal_basis": "ч. 3 ст. 6, ст. 18.1 152-ФЗ",
        "description": "Нераскрытие поручения обработки ПДн стороннему сервису/виджету в тексте Политики конфиденциальности",
    },
    "hidden_subprocessors_list": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.11_part_1",
        "legal_basis": "ч. 1 ст. 13.11 КоАП РФ, ч. 3 ст. 6, ст. 18.1 152-ФЗ",
        "description": "Сокрытие перечня субобработчиков: отсылка к предоставлению списка третьих лиц 'по запросу'",
    },
    "foreign_mx_email_transfer_risk": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "248_fz_art_49",
        "legal_basis": "ст. 12 152-ФЗ, ст. 49 248-ФЗ",
        "description": "Трансграничная передача входящих заявок через иностранный почтовый сервер без уведомления РКН",
    },
    "rkn_precaution_10day_demand": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "248_fz_art_49",
        "legal_basis": "ст. 49 Федерального закона № 248-ФЗ",
        "description": "Предостережение / требование Роскомнадзора об устранении нарушений верстки/форм в 10-дневный срок",
    },
    "rkn_risk_indicator_threshold": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "decree_1286",
        "legal_basis": "ПП РФ № 1286, Приказ Минцифры № 786",
        "description": "Индикатор риска: наличие от 3 нарушений влечет согласование внеплановой проверки с прокуратурой",
    },
    "mintsifry_it_accreditation_risk": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "mintsifry_511",
        "legal_basis": "Приказ Минцифры России № 511",
        "description": "Риск аннулирования государственной аккредитации IT-компании (не раскрыт ИНН, профильный ОКВЭД или стек)",
    },
    "foreign-auth-google-apple": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.55_part_2",
        "legal_basis": "ч. 2 ст. 13.55 КоАП РФ, Федеральный закон № 199-ФЗ",
        "description": "Авторизация через Google/Apple (мораторий на штрафы до 01.01.2029: требование об устранении без штрафа)",
    },
    "telegram-auth-widget": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.55_part_2",
        "legal_basis": "ч. 2 ст. 13.55 КоАП РФ, Федеральный закон № 199-ФЗ",
        "description": "Виджет авторизации Telegram (мораторий на штрафы до 01.01.2029: требование об устранении без штрафа)",
    },
    "yandex-metrika-undisclosed-in-policy": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.11_part_1",
        "legal_basis": "ч. 1 ст. 18.1 152-ФЗ, ст. 49 248-ФЗ",
        "description": "Счетчик Яндекс.Метрики установлен, но не раскрыт в Политике конфиденциальности (требование 248-ФЗ)",
    },
    "commercial_b2c_foreign_words_168fz": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "168_fz",
        "legal_basis": "Федеральный закон № 168-ФЗ (О государственном языке)",
        "description": "Публичная B2C информация для потребителей на иностранном языке без равнозначного русского перевода",
    },
    "custom-checkbox-or-consent-notice": {
        "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "koap_article": "13.11_part_2",
        "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ, Федеральный закон № 156-ФЗ",
        "description": "Нестандартный чекбокс / уведомление о согласии в тексте формы (требуется проверка отдельного согласия по 156-ФЗ)",
    },

    # --- GRAY_HYGIENE_BONUS ---
    "google_fonts_hygiene": {
        "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
        "koap_article": "hygiene",
        "legal_basis": "Техническая веб-гигиена",
        "description": "Шрифты Google Fonts (fonts.googleapis.com) рекомендуются к локализации для ускорения загрузки DOM",
    },
    "benign_gtm_container": {
        "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
        "koap_article": "hygiene",
        "legal_basis": "Техническая веб-гигиена",
        "description": "Контейнер Google Tag Manager проверен: пуст или содержит только российские счетчики (состав нарушения отсутствует)",
    },
    "empty_gtm_container_404": {
        "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
        "koap_article": "hygiene",
        "legal_basis": "Техническая веб-гигиена",
        "description": "Контейнер GTM деактивирован на стороне Google (404 Not Found), передача данных отсутствует",
    },
    "over_marked_non_ad": {
        "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
        "koap_article": "business_hygiene",
        "legal_basis": "Маркетинговая оптимизация охватов",
        "description": "Перемаркировка: токен erid размещен на аналитическом материале без объекта рекламирования",
    },
}


def classify_finding(
    signature: str,
    context_mode: str = "OWN_WEBSITE",
    witness: Optional[EvidenceWitness] = None,
    allow_downgrade: bool = True,
) -> Dict[str, Any]:
    """
    Classifies an audit finding into EnforcementTier and applies TripleEvidenceLock.
    """
    sig_clean = signature.strip().lower()
    meta = TIER_SIGNATURE_REGISTRY.get(sig_clean)

    if not meta:
        # Fallback classification based on signature heuristics
        if any(k in sig_clean for k in ["font", "hygiene", "over_mark"]):
            meta = {
                "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
                "koap_article": "hygiene",
                "legal_basis": "Техническая веб-гигиена",
                "description": f"Рекомендация по веб-гигиене ({signature})",
            }
        elif any(k in sig_clean for k in ["moratorium", "auth", "mintsifry", "indicator", "13.55", "168", "custom", "checkbox", "notice"]):
            meta = {
                "tier": EnforcementTier.ORANGE_REGULATORY_DEMAND,
                "koap_article": "regulatory_demand",
                "legal_basis": "Федеральный контроль и предписания",
                "description": f"Регуляторное требование к форме / согласию ({signature})",
            }
        else:
            # STRICT INVARIANT: Unknown signatures by default go to GRAY, NEVER to RED!
            meta = {
                "tier": EnforcementTier.GRAY_HYGIENE_BONUS,
                "koap_article": "hygiene",
                "legal_basis": "Информационное замечание",
                "description": f"Техническое замечание ({signature})",
            }

    raw_tier = meta["tier"]
    final_tier, locked_witness = TripleEvidenceLock.verify_and_lock(
        signature=sig_clean,
        tier=raw_tier,
        witness=witness,
        allow_downgrade_to_gray=allow_downgrade,
    )

    return {
        "signature": sig_clean,
        "tier": final_tier.value,
        "is_real_fine": final_tier == EnforcementTier.RED_REAL_FINE,
        "is_regulatory_demand": final_tier == EnforcementTier.ORANGE_REGULATORY_DEMAND,
        "is_hygiene_bonus": final_tier == EnforcementTier.GRAY_HYGIENE_BONUS,
        "koap_article": meta.get("koap_article", ""),
        "legal_basis": meta.get("legal_basis", ""),
        "description": meta.get("description", ""),
        "witness": locked_witness.model_dump() if locked_witness else None,
    }


def split_findings_by_traffic_light(
    findings: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Separates list of findings into RED_REAL_FINE, ORANGE_REGULATORY_DEMAND, and GRAY_HYGIENE_BONUS.
    Ensures strict segregation for reporting and calculations.
    """
    red_items: List[Dict[str, Any]] = []
    orange_items: List[Dict[str, Any]] = []
    gray_items: List[Dict[str, Any]] = []

    for f in findings:
        t = f.get("tier")
        if t == EnforcementTier.RED_REAL_FINE.value or t == "RED":
            red_items.append(f)
        elif t == EnforcementTier.ORANGE_REGULATORY_DEMAND.value or t == "ORANGE":
            orange_items.append(f)
        else:
            gray_items.append(f)

    return {
        "RED_REAL_FINE": red_items,
        "ORANGE_REGULATORY_DEMAND": orange_items,
        "GRAY_HYGIENE_BONUS": gray_items,
    }

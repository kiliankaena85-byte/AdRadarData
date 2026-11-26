"""
KoAP Sanctions and Regulatory Risk Calculator.
Implements:
1. Non-multiplication principle of administrative fines (Part 5 Art. 4.4 KoAP RF).
2. 248-FZ Procedural Barrier: Precaution / 10-day demand vs Unscheduled Inspection Risk (Decree 1286 / Order 786).
3. SME reliefs: First offense warning (Art. 4.1.1 KoAP RF) and 50% discount (Art. 4.1.2 KoAP RF).
4. Foreign authorization fine moratorium until 01.01.2029 (Part 2 Art. 13.55 KoAP RF).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml


DEFAULT_CONFIG = {
    "rkn_risk_indicator_threshold": 3,
    "rkn_outdated_registry_year_cutoff": 2022,
    "foreign_auth_fine_moratorium_until": "2029-01-01",
    "koap_sanctions_table": {
        "13.11_part_1": {
            "description": "Обработка ПД в случаях, не предусмотренных законом, либо несовместимая с целями",
            "min_legal": 60000,
            "max_legal": 100000,
            "min_official": 10000,
            "max_official": 20000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "13.11_part_1.1": {
            "description": "Обработка ПД без согласия в письменной форме либо с нарушением требований к составу",
            "min_legal": 30000,
            "max_legal": 150000,
            "min_official": 20000,
            "max_official": 40000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "13.11_part_2": {
            "description": "Обработка ПД без согласия субъекта (отсутствие чекбокса/согласия в форме)",
            "min_legal": 30000,
            "max_legal": 150000,
            "min_official": 20000,
            "max_official": 40000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "13.11_part_3": {
            "description": "Невыполнение обязанности по опубликованию политики обработки ПД",
            "min_legal": 30000,
            "max_legal": 60000,
            "min_official": 6000,
            "max_official": 12000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "14.3_part_1": {
            "description": "Нарушение законодательства о рекламе (в т.ч. превосходная степень без сноски)",
            "min_legal": 100000,
            "max_legal": 500000,
            "min_official": 4000,
            "max_official": 20000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "14.3_part_16": {
            "description": "Распространение интернет-рекламы без erid либо непредставление в ЕРИР",
            "min_legal": 200000,
            "max_legal": 500000,
            "min_official": 100000,
            "max_official": 200000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "14.8_part_2": {
            "description": "Включение в договор условий, ущемляющих права потребителя (156-ФЗ)",
            "min_legal": 10000,
            "max_legal": 20000,
            "min_official": 1000,
            "max_official": 2000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "13.11_part_8": {
            "description": "Невыполнение обязанности по локализации баз данных ПД граждан РФ (ч. 5 ст. 18 152-ФЗ)",
            "min_legal": 1000000,
            "max_legal": 6000000,
            "min_official": 100000,
            "max_official": 200000,
            "sme_warning_eligible": False,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "14.8_part_1": {
            "description": "Нарушение права потребителя на информацию об изготовителе/продавце (ст. 9 ЗоЗПП)",
            "min_legal": 5000,
            "max_legal": 10000,
            "min_official": 500,
            "max_official": 1000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "14.7_part_2": {
            "description": "Введение потребителей в заблуждение относительно производителя/свойств товара (ст. 14.7 КоАП РФ)",
            "min_legal": 100000,
            "max_legal": 500000,
            "min_official": 12000,
            "max_official": 20000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "20.3_part_1": {
            "description": "Публичное демонстрирование символики экстремистских организаций (Meta/Instagram/Facebook)",
            "min_legal": 10000,
            "max_legal": 50000,
            "min_official": 1000,
            "max_official": 4000,
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
        "13.55_part_2": {
            "description": "Нарушение требований к авторизации (Google, Apple, Telegram widget)",
            "min_legal": 0,
            "max_legal": 0,
            "nominal_min_legal": 500000,
            "nominal_max_legal": 700000,
            "min_official": 0,
            "max_official": 0,
            "nominal_min_official": 30000,
            "nominal_max_official": 50000,
            "moratorium_active": True,
            "moratorium_until": "2029-01-01",
            "sme_warning_eligible": True,
            "sme_discount_eligible": True,
            "grouping_rule": "single_fine_per_check",
        },
    },
}

VIOLATION_TO_ARTICLE_MAP = {
    "missing-consent-checkbox": "13.11_part_2",
    "prechecked-consent-checkbox": "13.11_part_2",
    "missing-privacy-policy-link": "13.11_part_3",
    "bundled-offer-consent-missing-separate-consent": "13.11_part_2",
    "bundled-pd-and-advertising-consent": "13.11_part_2",
    "yandex-metrika-undisclosed-in-policy": "13.11_part_1",
    "google-tag-manager": "13.11_part_1",
    "google-analytics": "13.11_part_1",
    "google-recaptcha": "13.11_part_1",
    "google-forms": "13.11_part_1",
    "foreign-tracker-meta-hotjar": "13.11_part_1",
    "foreign-auth-google-apple": "13.55_part_2",
    "telegram-auth-widget": "13.55_part_2",
    "erid_missing": "14.3_part_16",
    "unmarked_ad_no_erid": "14.3_part_16",
    "incomplete_marking_label_or_inn": "14.3_part_16",
    "partial_erid_missing_on_link": "14.3_part_16",
    "banned_or_special_regime": "14.3_part_1",
    "rkn_unregistered_operator": "13.11_part_1",
    "third_party_ad_no_erid": "14.3_part_16",
    "superlative_without_asterisk": "14.3_part_1",
    "custom-checkbox-or-consent-notice": "13.11_part_2",
    "foreign_widget_data_leak": "13.11_part_8",
    "host_geo_foreign_risk": "13.11_part_8",
    "missing_seller_requisites_zozpp": "14.8_part_1",
    "phoenix_clone_detected": "14.7_part_2",
    "policy_domain_mismatch_paradox": "13.11_part_3",
    "regalia_chronology_mismatch": "14.3_part_1",
    "extremist_meta_social_links_20_3": "20.3_part_1",
    "unmarked_credit_installment_38fz": "14.3_part_1",
    "implicit_browse_consent_invalid": "13.11_part_2",
    "analytics_denied_as_pd": "13.11_part_1",
    "hidden_subprocessors_list": "13.11_part_1",
    "illegal_no_refund_b2c_clause": "14.8_part_2",
    "unprotected_affiliate_program": "14.3_part_16",
    "toxic_supply_chain_widget": "13.11_part_8",
    "toxic-supply-chain-widget": "13.11_part_8",
    "undisclosed_third_party_processor": "13.11_part_1",
    "undisclosed-third-party-processor": "13.11_part_1",
    "commercial_b2c_foreign_words_168fz": "14.8_part_1",
    "168_fz": "14.8_part_1",
    "rkn_undeclared_cross_border_transfer": "13.11_part_1",
    "cloudflare-turnstile": "13.11_part_8",
    "foreign-chat-crm-widget": "13.11_part_8",
    "foreign-ad-pixel-tiktok-linkedin": "13.11_part_8",
}


class KoAPSanctionsCalculator:
    """
    Calculator for administrative liabilities under KoAP RF.
    Ensures legally grounded calculation honoring procedural safeguards.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config = dict(DEFAULT_CONFIG)
        resolved_path = None
        if config_path and Path(config_path).exists():
            resolved_path = Path(config_path)
        else:
            candidates = [
                Path("config/compliance_nuances_v2.yaml"),
                Path("legal_sheaf_verifier/config/compliance_nuances_v2.yaml"),
                Path(__file__).resolve().parent.parent.parent / "config" / "compliance_nuances_v2.yaml",
            ]
            for c in candidates:
                if c.exists():
                    resolved_path = c
                    break

        if resolved_path and resolved_path.exists():
            try:
                with open(resolved_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                    if isinstance(loaded, dict):
                        self.config.update(loaded)
            except Exception:
                pass

        self.rkn_risk_indicator_threshold: int = self.config.get("rkn_risk_indicator_threshold", 3)
        self.sanctions_table: Dict[str, Any] = self.config.get("koap_sanctions_table", DEFAULT_CONFIG["koap_sanctions_table"])

    def map_violation_to_article(self, violation: str) -> str:
        """Resolves signature or infraction string to KoAP RF article key."""
        v_clean = violation.strip().lower()
        if v_clean in VIOLATION_TO_ARTICLE_MAP:
            return VIOLATION_TO_ARTICLE_MAP[v_clean]
        for k, art in VIOLATION_TO_ARTICLE_MAP.items():
            if k in v_clean:
                return art
        if "13.11" in v_clean:
            if "ч. 2" in v_clean or "part_2" in v_clean:
                return "13.11_part_2"
            if "ч. 3" in v_clean or "part_3" in v_clean:
                return "13.11_part_3"
            return "13.11_part_1"
        if "14.3" in v_clean:
            if "16" in v_clean or "erid" in v_clean:
                return "14.3_part_16"
            return "14.3_part_1"
        if "13.55" in v_clean:
            return "13.55_part_2"
        if "14.8" in v_clean or "168" in v_clean or "zozpp" in v_clean:
            if "part_2" in v_clean or "ч. 2" in v_clean or "refund" in v_clean:
                return "14.8_part_2"
            return "14.8_part_1"
        if "14.7" in v_clean:
            return "14.7_part_2"
        if "20.3" in v_clean:
            return "20.3_part_1"
        return "13.11_part_1"

    def calculate_penalties(
        self,
        violations: List[str],
        is_sme: bool = True,
        first_offense: bool = True,
        is_ip: bool = False,
    ) -> Dict[str, Any]:
        """
        Calculates sanctions under KoAP RF with grouping, 248-FZ barrier, and SME relief.
        """
        if not violations:
            return {
                "raw_violations_count": 0,
                "unique_articles_count": 0,
                "rkn_risk_indicators_count": 0,
                "regulatory_outcome": "COMPLIANT",
                "outcome_description": "Нарушений не выявлено. Риски привлечения к ответственности отсутствуют.",
                "is_sme": is_sme,
                "is_ip": is_ip,
                "first_offense": first_offense,
                "warning_substituted": False,
                "sme_warning_note": "",
                "grouped_penalties": [],
                "total_min_rubles": 0,
                "total_max_rubles": 0,
                "applicable_min_rubles": 0,
                "applicable_max_rubles": 0,
                "applied_legal_principles": [],
            }

        # 1. Group violations by KoAP Article (Part 5 Art. 4.4 KoAP RF) - Canonical Sort
        article_groups: Dict[str, List[str]] = {}
        unique_signatures = set()
        for v in sorted(violations):
            sig = v.strip().lower()
            unique_signatures.add(sig)
            art = self.map_violation_to_article(sig)
            article_groups.setdefault(art, []).append(v)

        rkn_risk_indicators_count = len(unique_signatures)

        # 2. 248-FZ Regulatory Outcome
        if rkn_risk_indicators_count < self.rkn_risk_indicator_threshold:
            regulatory_outcome = "PRECAUTION_OR_DEMAND_10_DAYS"
            outcome_description = (
                f"По ст. 49 248-ФЗ число расхождений ({rkn_risk_indicators_count}) ниже порога риска ({self.rkn_risk_indicator_threshold}). "
                "Автоматический штраф не назначается; оператору направляется предостережение или требование об устранении за 10 дней."
            )
        else:
            regulatory_outcome = "UNSCHEDULED_INSPECTION_RISK"
            outcome_description = (
                f"Число индикаторов риска ({rkn_risk_indicators_count}) достигло порогового значения ({self.rkn_risk_indicator_threshold}) "
                "согласно ПП РФ № 1286 и Приказу Минцифры РФ № 786. Высокая вероятность внеплановой проверки РКН и составления протокола."
            )

        # 3. Calculate grouped fines
        grouped_penalties = []
        total_min = 0
        total_max = 0
        applicable_min = 0
        applicable_max = 0
        warning_substituted_any = False
        applied_principles = [
            "ч. 5 ст. 4.4 КоАП РФ: Однородные нарушения сгруппированы в один состав (запрет мультипликации штрафов)",
            "248-ФЗ: Процессуальный фильтр (предостережение и 10 дней на устранение)",
        ]

        for art, matched_viols in sorted(article_groups.items()):
            sanction_info = self.sanctions_table.get(art, {})
            desc = sanction_info.get("description", f"Статья {art} КоАП РФ")
            min_legal = sanction_info.get("min_legal", 0)
            max_legal = sanction_info.get("max_legal", 0)
            min_official = sanction_info.get("min_official", 0)
            max_official = sanction_info.get("max_official", 0)
            sme_warn_eligible = sanction_info.get("sme_warning_eligible", True)
            sme_disc_eligible = sanction_info.get("sme_discount_eligible", True)
            moratorium_active = sanction_info.get("moratorium_active", False)

            if art == "13.55_part_2" or moratorium_active:
                status = "MORATORIUM_WARNING"
                art_min = 0
                art_max = 0
                art_app_min = 0
                art_app_max = 0
                note = "Действует мораторий на штрафы до 01.01.2029 по ч. 2 ст. 13.55 КоАП РФ. Применяется предупреждение."
                if "Мораторий до 01.01.2029 по ст. 13.55 КоАП РФ" not in applied_principles:
                    applied_principles.append("Мораторий до 01.01.2029 по ст. 13.55 КоАП РФ (ч. 2 ст. 13.55)")
            elif is_ip:
                status = "IP_SANCTION"
                if art == "14.3_part_16":
                    art_app_min = 30000
                    art_app_max = 100000
                    note = "Санкция для индивидуальных предпринимателей по ч. 16 ст. 14.3 КоАП РФ (30 000 – 100 000 руб.)."
                else:
                    art_app_min = min_official or int(min_legal * 0.5)
                    art_app_max = max_official or int(max_legal * 0.5)
                    note = f"Санкция для индивидуальных предпринимателей по ст. {art} КоАП РФ."

                if is_sme and first_offense and sme_warn_eligible:
                    status = "SME_WARNING_SUBSTITUTED"
                    art_min = 0
                    art_max = 0
                    warning_substituted_any = True
                    note = f"Применена ст. 4.1.1 КоАП РФ: замена первого штрафа предупреждением для ИП. Вилка при неисполнении: {art_app_min:,} – {art_app_max:,} руб."
                else:
                    art_min = art_app_min
                    art_max = art_app_max
            elif is_sme and sme_disc_eligible:
                art_app_min = int(min_legal * 0.5)
                art_app_max = int(max_legal * 0.5)
                if first_offense and sme_warn_eligible:
                    status = "SME_WARNING_SUBSTITUTED"
                    art_min = 0
                    art_max = 0
                    warning_substituted_any = True
                    note = f"Применена ст. 4.1.1 КоАП РФ: замена первого штрафа предупреждением для МСП. Вилка при неисполнении предписания: {art_app_min:,} – {art_app_max:,} руб."
                    if "ст. 4.1.1 КоАП РФ: Замена первого штрафа предупреждением для МСП" not in applied_principles:
                        applied_principles.append("ст. 4.1.1 КоАП РФ: Замена первого штрафа предупреждением для МСП")
                else:
                    status = "SME_DISCOUNT_APPLIED"
                    art_min = art_app_min
                    art_max = art_app_max
                    note = "Применена ст. 4.1.2 КоАП РФ: 50% скидка на административный штраф для юридических лиц субъектов МСП."
                    if "ст. 4.1.2 КоАП РФ: 50% скидка на штрафы юрлиц для МСП" not in applied_principles:
                        applied_principles.append("ст. 4.1.2 КоАП РФ: 50% скидка на штрафы юрлиц для МСП")
            else:
                status = "STANDARD_FINE"
                art_app_min = min_legal
                art_app_max = max_legal
                art_min = min_legal
                art_max = max_legal
                note = "Стандартная санкция КоАП РФ для юридических лиц."

            total_min += art_min
            total_max += art_max
            applicable_min += art_app_min
            applicable_max += art_app_max

            grouped_penalties.append(
                {
                    "article": art,
                    "description": desc,
                    "matched_count": len(matched_viols),
                    "matched_violations": matched_viols,
                    "status": status,
                    "min_rubles": art_min,
                    "max_rubles": art_max,
                    "applicable_min_rubles": art_app_min,
                    "applicable_max_rubles": art_app_max,
                    "nominal_min_rubles": min_legal,
                    "nominal_max_rubles": max_legal,
                    "note": note,
                }
            )

        return {
            "raw_violations_count": len(violations),
            "unique_articles_count": len(article_groups),
            "rkn_risk_indicators_count": rkn_risk_indicators_count,
            "regulatory_outcome": regulatory_outcome,
            "outcome_description": outcome_description,
            "is_sme": is_sme,
            "is_ip": is_ip,
            "first_offense": first_offense,
            "warning_substituted": warning_substituted_any,
            "sme_warning_note": "(1-й контакт МСП: предупреждение по ст. 4.1.1 КоАП; штраф при жалобе / неисполнении предписания)" if warning_substituted_any else "",
            "grouped_penalties": grouped_penalties,
            "total_min_rubles": total_min,
            "total_max_rubles": total_max,
            "applicable_min_rubles": applicable_min,
            "applicable_max_rubles": applicable_max,
            "applied_legal_principles": applied_principles,
        }

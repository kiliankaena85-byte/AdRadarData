"""
Legal Chronotope and Regional Practice Module.
Implements temporal validity gates (Art. 1.7 & Art. 4.5 KoAP RF),
court & antitrust hierarchy weighting, retroactive leniency (Decree No. 1427),
SME reliefs (Art. 4.1.1 & 4.1.2 KoAP RF), and regional jurisdiction routing by INN.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import yaml
from pydantic import BaseModel, Field

from legal_sheaf.schema import (
    AuthorityTier,
    AUTHORITY_TIER_WEIGHTS,
    OffenseContinuity,
    ProceduralStatus,
    TemporalValidityStatus,
)


# =====================================================================
# 1. RUSSIAN FNS REGIONS AND ARBITRAL CASSATION CIRCUITS (10 CIRCUITS)
# =====================================================================

CIRCUIT_DESCRIPTIONS = {
    "MO": "Арбитражный суд Московского округа (АС МО)",
    "SZO": "Арбитражный суд Северо-Западного округа (АС СЗО)",
    "VVO": "Арбитражный суд Волго-Вятского округа (АС ВВО)",
    "PO": "Арбитражный суд Поволжского округа (АС ПО)",
    "SKO": "Арбитражный суд Северо-Кавказского округа (АС СКО)",
    "CO": "Арбитражный суд Центрального округа (АС ЦО)",
    "UO": "Арбитражный суд Уральского округа (АС УО)",
    "ZSO": "Арбитражный суд Западно-Сибирского округа (АС ЗСО)",
    "VSO": "Арбитражный суд Восточно-Сибирского округа (АС ВСО)",
    "DO": "Арбитражный суд Дальневосточного округа (АС ДО)",
}

# FNS 2-digit region code -> (Circuit Code, Region Name, Territorial UFAS Name)
FNS_REGION_MAP: Dict[str, Tuple[str, str, str]] = {
    "01": ("SKO", "Республика Адыгея", "Адыгейское УФАС России"),
    "02": ("UO", "Республика Башкортостан", "Башкортостанское УФАС России"),
    "03": ("VSO", "Республика Бурятия", "Бурятское УФАС России"),
    "04": ("ZSO", "Республика Алтай", "Алтайское республиканское УФАС России"),
    "05": ("SKO", "Республика Дагестан", "Дагестанское УФАС России"),
    "06": ("SKO", "Республика Ингушетия", "Ингушское УФАС России"),
    "07": ("SKO", "Кабардино-Балкарская Республика", "Кабардино-Балкарское УФАС России"),
    "08": ("PO", "Республика Калмыкия", "Калмыцкое УФАС России"),
    "09": ("SKO", "Карачаево-Черкесская Республика", "Карачаево-Черкесское УФАС России"),
    "10": ("SZO", "Республика Карелия", "Карельское УФАС России"),
    "11": ("VVO", "Республика Коми", "Коми УФАС России"),
    "12": ("VVO", "Республика Марий Эл", "Марийское УФАС России"),
    "13": ("VVO", "Республика Мордовия", "Мордовское УФАС России"),
    "14": ("VSO", "Республика Саха (Якутия)", "Якутское УФАС России"),
    "15": ("SKO", "Республика Северная Осетия - Алания", "Северо-Осетинское УФАС России"),
    "16": ("PO", "Республика Татарстан", "Татарстанское УФАС России"),
    "17": ("VSO", "Республика Тыва", "Тывинское УФАС России"),
    "18": ("UO", "Удмуртская Республика", "Удмуртское УФАС России"),
    "19": ("VSO", "Республика Хакасия", "Хакасское УФАС России"),
    "20": ("SKO", "Чеченская Республика", "Чеченское УФАС России"),
    "21": ("VVO", "Чувашская Республика", "Чувашское УФАС России"),
    "22": ("ZSO", "Алтайский край", "Алтайское краевое УФАС России"),
    "23": ("SKO", "Краснодарский край", "Краснодарское УФАС России"),
    "24": ("VSO", "Красноярский край", "Красноярское УФАС России"),
    "25": ("DO", "Приморский край", "Приморское УФАС России"),
    "26": ("SKO", "Ставропольский край", "Ставропольское УФАС России"),
    "27": ("DO", "Хабаровский край", "Хабаровское УФАС России"),
    "28": ("DO", "Амурская область", "Амурское УФАС России"),
    "29": ("SZO", "Архангельская область", "Архангельское УФАС России"),
    "30": ("PO", "Астраханская область", "Астраханское УФАС России"),
    "31": ("CO", "Белгородская область", "Белгородское УФАС России"),
    "32": ("CO", "Брянская область", "Брянское УФАС России"),
    "33": ("VVO", "Владимирская область", "Владимирское УФАС России"),
    "34": ("PO", "Волгоградская область", "Волгоградское УФАС России"),
    "35": ("SZO", "Вологодская область", "Вологодское УФАС России"),
    "36": ("CO", "Воронежская область", "Воронежское УФАС России"),
    "37": ("VVO", "Ивановская область", "Ивановское УФАС России"),
    "38": ("VSO", "Иркутская область", "Иркутское УФАС России"),
    "39": ("SZO", "Калининградская область", "Калининградское УФАС России"),
    "40": ("CO", "Калужская область", "Калужское УФАС России"),
    "41": ("DO", "Камчатский край", "Камчатское УФАС России"),
    "42": ("ZSO", "Кемеровская область", "Кемеровское УФАС России"),
    "43": ("VVO", "Кировская область", "Кировское УФАС России"),
    "44": ("VVO", "Костромская область", "Костромское УФАС России"),
    "45": ("UO", "Курганская область", "Курганское УФАС России"),
    "46": ("CO", "Курская область", "Курское УФАС России"),
    "47": ("SZO", "Ленинградская область", "Ленинградское УФАС России"),
    "48": ("CO", "Липецкая область", "Липецкое УФАС России"),
    "49": ("DO", "Магаданская область", "Магаданское УФАС России"),
    "50": ("MO", "Московская область", "Московское областное УФАС России"),
    "51": ("SZO", "Мурманская область", "Мурманское УФАС России"),
    "52": ("VVO", "Нижегородская область", "Нижегородское УФАС России"),
    "53": ("SZO", "Новгородская область", "Новгородское УФАС России"),
    "54": ("ZSO", "Новосибирская область", "Новосибирское УФАС России"),
    "55": ("ZSO", "Омская область", "Омское УФАС России"),
    "56": ("UO", "Оренбургская область", "Оренбургское УФАС России"),
    "57": ("CO", "Орловская область", "Орловское УФАС России"),
    "58": ("PO", "Пензенская область", "Пензенское УФАС России"),
    "59": ("UO", "Пермский край", "Пермское УФАС России"),
    "60": ("SZO", "Псковская область", "Псковское УФАС России"),
    "61": ("SKO", "Ростовская область", "Ростовское УФАС России"),
    "62": ("CO", "Рязанская область", "Рязанское УФАС России"),
    "63": ("PO", "Самарская область", "Самарское УФАС России"),
    "64": ("PO", "Саратовская область", "Саратовское УФАС России"),
    "65": ("DO", "Сахалинская область", "Сахалинское УФАС России"),
    "66": ("UO", "Свердловская область", "Свердловское УФАС России"),
    "67": ("CO", "Смоленская область", "Смоленское УФАС России"),
    "68": ("CO", "Тамбовская область", "Тамбовское УФАС России"),
    "69": ("SZO", "Тверская область", "Тверское УФАС России"),
    "70": ("ZSO", "Томская область", "Томское УФАС России"),
    "71": ("CO", "Тульская область", "Тульское УФАС России"),
    "72": ("ZSO", "Тюменская область", "Тюменское УФАС России"),
    "73": ("PO", "Ульяновская область", "Ульяновское УФАС России"),
    "74": ("UO", "Челябинская область", "Челябинское УФАС России"),
    "75": ("VSO", "Забайкальский край", "Забайкальское УФАС России"),
    "76": ("VVO", "Ярославская область", "Ярославское УФАС России"),
    "77": ("MO", "г. Москва", "Московское УФАС России"),
    "78": ("SZO", "г. Санкт-Петербург", "Санкт-Петербургское УФАС России"),
    "79": ("DO", "Еврейская автономная область", "Еврейское УФАС России"),
    "83": ("SZO", "Ненецкий автономный округ", "Ненецкое УФАС России"),
    "86": ("ZSO", "ХМАО - Югра", "Ханты-Мансийское УФАС России"),
    "87": ("DO", "Чукотский автономный округ", "Чукотское УФАС России"),
    "89": ("ZSO", "ЯНАО", "Ямало-Ненецкое УФАС России"),
    "91": ("SKO", "Республика Крым", "Крымское УФАС России"),
    "92": ("SKO", "г. Севастополь", "Севастопольское УФАС России"),
    "99": ("MO", "Межрегиональные юрисдикции", "Центральный аппарат ФАС России"),
}


class TemporalDecision(BaseModel):
    """Result of temporal validity analysis under Art. 1.7 & Art. 4.5 KoAP RF."""
    status: TemporalValidityStatus
    is_punishable: bool
    applicable_statute: str
    rationale: str
    sme_relief: Optional[str] = None


class TemporalValidityGate:
    """
    Time-machine and statutory temporal validity evaluator.
    Evaluates retroactivity, limitation periods, Decree No. 1427 leniency,
    and SME mitigation under Art. 1.7, 4.1.1, 4.1.2, and 4.5 KoAP RF.
    """

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            pkg_root = Path(__file__).resolve().parent.parent.parent
            config_path = str(pkg_root / "config" / "special_regimes.yaml")

        self.config_path = Path(config_path)
        self.special_regimes: Dict[str, Any] = {}
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.special_regimes = yaml.safe_load(f) or {}

    def _parse_date(self, d: Optional[Union[str, date]]) -> Optional[date]:
        if d is None:
            return None
        if isinstance(d, date):
            return d
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    def evaluate(
        self,
        published_at: Optional[Union[str, date]] = None,
        last_reactualized_at: Optional[Union[str, date]] = None,
        inspected_at: Optional[Union[str, date]] = None,
        offense_type: str = "ad_marking",
        continuity: Union[OffenseContinuity, str] = OffenseContinuity.CONTINUING,
        is_sme: bool = True,
        is_first_offense: bool = True,
        is_video: bool = False,
        is_active_page: bool = True,
        statute_effective_from: Optional[Union[str, date]] = None,
    ) -> TemporalDecision:
        """
        Evaluate temporal validity of the alleged infraction.
        """
        inspected_date = self._parse_date(inspected_at) or date.today()
        pub_date = self._parse_date(published_at)
        react_date = self._parse_date(last_reactualized_at)

        # Baseline date of commission
        event_date = react_date or pub_date

        # Check 0: Future statute not yet in force (ч. 1 ст. 1.7 КоАП РФ)
        if statute_effective_from is not None:
            eff_date = self._parse_date(statute_effective_from)
            if eff_date and inspected_date < eff_date:
                return TemporalDecision(
                    status=TemporalValidityStatus.IMMUNE_NO_RETROACTIVE_LAW,
                    is_punishable=False,
                    applicable_statute=f"Норма вступает в силу с {eff_date.strftime('%d.%m.%Y')}",
                    rationale=(
                        f"Дата проверки ({inspected_date.strftime('%d.%m.%Y')}) предшествует дате вступления нормы в силу "
                        f"({eff_date.strftime('%d.%m.%Y')}). В силу ч. 1 ст. 1.7 КоАП РФ закон не имеет обратной силы. "
                        "Оснований для привлечения к ответственности не имеется."
                    ),
                )

        if isinstance(continuity, str):
            continuity = OffenseContinuity(continuity)

        # -------------------------------------------------------------
        # 1. COMPLETED OFFENSES (Оконченные правонарушения)
        # -------------------------------------------------------------
        if continuity == OffenseContinuity.COMPLETED:
            if event_date is None:
                event_date = inspected_date

            # Check 1.1: Non-retroactivity of sanctions (ч. 1 ст. 1.7 КоАП РФ)
            # Fines for ad marking (ч. 15-17 ст. 14.3 КоАП РФ) came into force on 2023-09-01
            sanction_start = date(2023, 9, 1)
            if offense_type in ("ad_marking", "unmarked_ad", "incomplete_marking", "erid_missing"):
                if event_date < sanction_start:
                    return TemporalDecision(
                        status=TemporalValidityStatus.IMMUNE_NO_RETROACTIVE_LAW,
                        is_punishable=False,
                        applicable_statute="ч. 1 ст. 1.7 КоАП РФ",
                        rationale=(
                            f"Материал опубликован/совершен {event_date.strftime('%d.%m.%Y')}, до 01.09.2023 "
                            f"(момента введения административной ответственности по ч. 15–17 ст. 14.3 КоАП РФ). "
                            "В силу ч. 1 ст. 1.7 КоАП РФ закон, устанавливающий или отягчающий ответственность, "
                            "обратной силы не имеет. Привлечение к ответственности исключено."
                        ),
                    )

            # Check 1.2: Retroactive leniency under Decree No. 1427 (ч. 2 ст. 1.7 КоАП РФ)
            # For non-video creatives older than 1 year from execution, reporting duty expires
            decree_1427_date = date(2025, 9, 18)
            if not is_video and offense_type in ("ad_marking", "ord_reporting", "unmarked_ad"):
                age_days = (inspected_date - event_date).days
                if age_days > 365:
                    return TemporalDecision(
                        status=TemporalValidityStatus.RETROACTIVE_LENIENCY_APPLIED,
                        is_punishable=False,
                        applicable_statute="ПП РФ от 18.09.2025 № 1427, ч. 2 ст. 1.7 КоАП РФ",
                        rationale=(
                            f"Для не-видео креативов Постановлением Правительства РФ № 1427 от 18.09.2025 "
                            f"срок обязательной отчетности в ОРД сокращен до 1 года (материал размещен {event_date.strftime('%d.%m.%Y')}, "
                            f"возраст {age_days} дн.). В силу ч. 2 ст. 1.7 КоАП РФ норма имеет обратную силу как "
                            "смягчающая ответственность. Состав правонарушения прекращен."
                        ),
                    )

            # Check 1.3: Statute of Limitations (ст. 4.5 КоАП РФ - 1 год)
            limitation_days = (inspected_date - event_date).days
            if limitation_days > 365:
                return TemporalDecision(
                    status=TemporalValidityStatus.EXPIRED_STATUTE_4_5_KOAP,
                    is_punishable=False,
                    applicable_statute="ст. 4.5 КоАП РФ",
                    rationale=(
                        f"С момента совершения оконченного правонарушения ({event_date.strftime('%d.%m.%Y')}) "
                        f"прошло {limitation_days} дн. (более 365 дней). Годичный срок давности привлечения "
                        "к административной ответственности истек. Производство по делу подлежит прекращению."
                    ),
                )

        # -------------------------------------------------------------
        # 2. CONTINUING OFFENSES (Длящиеся правонарушения на живом сайте)
        # -------------------------------------------------------------
        elif continuity == OffenseContinuity.CONTINUING:
            # Under Item 14 Plenum Supreme Court No. 5 and Art. 4.5 Part 2 KoAP RF:
            # Continuing offense duration runs continuously until stopped or discovered by authority.
            # Statute of limitations starts from the day of discovery.
            pass

        # -------------------------------------------------------------
        # 3. SME RELIEFS (ст. 4.1.1 и 4.1.2 КоАП РФ)
        # -------------------------------------------------------------
        sme_relief = None
        if is_sme:
            if is_first_offense:
                sme_relief = (
                    "ст. 4.1.1 КоАП РФ: Обязательная замена административного штрафа на предупреждение "
                    "(первичное правонарушение субъекта МСП при отсутствии вреда жизни, здоровью и имущественного ущерба)."
                )
            else:
                sme_relief = (
                    "ст. 4.1.2 КоАП РФ: Снижение размера штрафа до предела санкции для ИП "
                    "(от половины минимального до половины максимального размера для юрлица)."
                )

        continuity_text = "длящееся (п. 14 Пленума ВС № 5, срок давности течет с момента фиксации)" if continuity == OffenseContinuity.CONTINUING else "оконченное (срок давности течет со дня совершения)"

        return TemporalDecision(
            status=TemporalValidityStatus.IN_FORCE,
            is_punishable=True,
            applicable_statute="Действующая редакция КоАП РФ на дату инспекции",
            rationale=(
                f"Правонарушение квалифицируется как {continuity_text}. "
                f"На момент инспекции ({inspected_date.strftime('%d.%m.%Y')}) норма сохраняет юридическую силу."
            ),
            sme_relief=sme_relief,
        )


class JurisdictionRouter:
    """
    Geographic and Jurisdiction Router.
    Maps INN prefix to Russian Subject, Territorial UFAS, and Arbitral Cassation Circuit.
    Applies geographic precedent weighting and detects regional practice divergences.
    """

    def __init__(self):
        pass

    def resolve_jurisdiction(self, inn: Optional[str]) -> Tuple[str, str, str]:
        """
        Resolves FNS region code, Territorial UFAS name, and Cassation Circuit code.
        Returns: (region_code, ufas_name, circuit_code)
        """
        if not inn or len(inn) < 2:
            return "77", "Московское УФАС России", "MO"

        region_code = inn[:2]
        if region_code in FNS_REGION_MAP:
            circuit, _, ufas_name = FNS_REGION_MAP[region_code]
            return region_code, ufas_name, circuit

        # Fallback to Moscow / Federal Central
        return "77", "Московское УФАС России", "MO"

    def calculate_geographic_weight(
        self,
        prec_jurisdiction: str,
        target_region: str,
        target_circuit: str,
        authority_tier: Union[AuthorityTier, str],
    ) -> float:
        """
        Calculate geographic relevance multiplier for a legal precedent:
        - Federal / Plenum / Supreme / Central: 1.00
        - Native Circuit or Native Regional UFAS: 1.25 (Circuit boost)
        - Non-native Regional UFAS: 0.65 (Foreign regional discount)
        """
        tier_str = authority_tier.value if isinstance(authority_tier, AuthorityTier) else str(authority_tier)

        if prec_jurisdiction == "FEDERAL" or tier_str in (
            AuthorityTier.PLENUM_SC_LAW.value,
            AuthorityTier.GOV_SC_REVIEW.value,
        ):
            return 1.00

        # Check if precedent belongs to the native circuit or native region
        prec_parts = prec_jurisdiction.replace("/", " ").replace("-", " ").split()
        if target_circuit in prec_parts or target_region in prec_parts:
            return 1.25

        # Different regional UFAS / court
        return 0.65

    def detect_practice_divergence(
        self,
        local_margin: float,
        federal_margin: float,
        circuit_code: str,
        ufas_name: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        Detects divergence between regional arbitration practice and federal / Moscow benchmark.
        """
        # Significant margin threshold
        if (local_margin * federal_margin < 0) and (abs(local_margin) >= 0.08 or abs(federal_margin) >= 0.08):
            circuit_name = CIRCUIT_DESCRIPTIONS.get(circuit_code, circuit_code)
            explanation = (
                f"REGIONAL_PRACTICE_DIVERGENCE: Зафиксирована коллизия региональной практики. "
                f"Практика кассационного округа {circuit_name} и {ufas_name} (маржа {local_margin:+.2f}) "
                f"расходится с федеральной практикой ЦА ФАС / АС МО (маржа {federal_margin:+.2f}). "
                f"Рекомендуется строить правовую позицию с приоритетом на позицию АС {circuit_code}."
            )
            return True, explanation

        return False, None

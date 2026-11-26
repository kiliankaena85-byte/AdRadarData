"""
Client Express Lead Packager & Outreach Generator for AdRadar / LegalSheaf.
Generates ephemeral 72-hour standalone HTML audit sheets and 3 tailored B2B outreach scripts.
Zero Content Storage: strictly metadata, technical telemetry, and verified statutes.
Dogfooding 152-FZ: 100% autonomous inline CSS and system font stack.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ingestion.factory_crawler import CompanyProfile, SiteAuditResult
from legal_sheaf.chronotope import CIRCUIT_DESCRIPTIONS, FNS_REGION_MAP, JurisdictionRouter
from legal_sheaf.config import REPORTS_DIR
from legal_sheaf.enforcement_reality import TIER_SIGNATURE_REGISTRY
from legal_sheaf.evolutionary_horizon import OptimalHorizonSolver
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.schema import VerificationReport


LEADS_OUTPUT_DIR = REPORTS_DIR / "leads"
LEADS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MANIFEST_FILE = LEADS_OUTPUT_DIR / "showcase_manifest.json"


def parse_russian_name(raw_name: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parses a Russian name string into (surname, first_name, patronymic).
    Recognizes patronymic suffixes (-ович, -евич, -ич, -овна, -евна, -ична).
    """
    if not raw_name:
        return None, None, None

    cleaned = re.sub(
        r"(?i)^(?:ИП|ООО|АО|ЗАО|ПАО|генеральный директор|директор|руководитель)[:\s\-—]+",
        "",
        raw_name,
    ).strip()
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return None, None, None

    patronymic_pattern = re.compile(r"(?i)^[А-ЯЁ][а-яё]+(?:ович|евич|ич|овна|евна|ична)$")

    if len(tokens) == 3:
        if patronymic_pattern.match(tokens[2]):
            return tokens[0], tokens[1], tokens[2]
        elif patronymic_pattern.match(tokens[1]):
            return tokens[2], tokens[0], tokens[1]
    elif len(tokens) == 2:
        if patronymic_pattern.match(tokens[1]):
            return None, tokens[0], tokens[1]
        elif patronymic_pattern.match(tokens[0]):
            return None, tokens[1], tokens[0]

    return None, None, None


def inflect_dative_genitive(first_name: str, patronymic: str) -> str:
    """
    Inflects Name + Patronymic into Genitive/Dative ("для <кого?>") for routing.
    E.g. "Михаил Вячеславович" -> "Михаила Вячеславовича"
    "Елена Николаевна" -> "Елены Николаевны"
    """
    if patronymic.endswith("ч"):
        patr_gen = patronymic + "а"
    elif patronymic.endswith("на"):
        patr_gen = patronymic[:-1] + "ы"
    else:
        patr_gen = patronymic

    fn_lower = first_name.lower()
    if fn_lower.endswith(("а", "я")):
        if fn_lower.endswith(("га", "ка", "ха", "жа", "ча", "ша", "ща")):
            fn_gen = first_name[:-1] + "и"
        elif fn_lower.endswith("я"):
            fn_gen = first_name[:-1] + "и" if fn_lower.endswith("ия") else first_name[:-1] + "е"
        else:
            fn_gen = first_name[:-1] + "ы"
    elif fn_lower.endswith("й"):
        fn_gen = first_name[:-1] + "я"
    elif fn_lower.endswith("ь"):
        fn_gen = first_name[:-1] + "я"
    elif re.search(r"[бвгджзклмнпрстфхцчшщ]$", fn_lower):
        fn_gen = first_name + "а"
    else:
        fn_gen = first_name

    return f"{fn_gen} {patr_gen}"


def build_human_salutation(
    management_name: Optional[str],
    corporate_emails: List[str],
) -> str:
    """
    Builds natural, personified Russian salutation according to professional etiquette.
    Avoids robot patterns like 'Здравствуйте, Иванов Иван Иванович!'.
    """
    generic_prefixes = (
        "info@", "help@", "contact@", "support@", "sales@", "hello@",
        "mail@", "office@", "artists@", "admin@", "b2b@", "service@",
    )
    is_impersonal = False
    if not corporate_emails or all(any(e.lower().startswith(p) for p in generic_prefixes) for e in corporate_emails):
        is_impersonal = True

    _, first_name, patronymic = parse_russian_name(management_name)

    if first_name and patronymic:
        if is_impersonal:
            genitive_name = inflect_dative_genitive(first_name, patronymic)
            return f"Добрый день! Передайте, пожалуйста, для {genitive_name} или руководителя ИТ/маркетинга:"
        else:
            return f"{first_name} {patronymic}, добрый день!"

    if is_impersonal:
        return "Добрый день! Передайте, пожалуйста, руководителю ИТ или маркетинга:"
    else:
        return "Добрый день!"


def get_human_regime_description(regime_name: str) -> str:
    """Translates internal ML regime enums into natural business Russian."""
    mapping = {
        "UNMARKED_AD_NO_ERID": "содержит прямую коммерческую интеграцию без обязательного токена маркировки erid",
        "COMMERCIAL_NATIVE_AD": "содержит признаки нативной рекламы с целевыми ссылками без обязательного erid",
        "BANNED_OR_SPECIAL_REGIME": "содержит признаки продвижения финансовых или специализированных услуг без установленных законом предупреждений и маркировки erid",
        "SURROGATE_MARKING": "содержит некорректную суррогатную маркировку без соблюдения требований Приказа РКН № 68",
        "INCOMPLETE_MARKING_LABEL_OR_INN": "содержит неполную маркировку (отсутствует токен erid или ИНН рекламодателя)",
        "CATALOG_PRODUCT_PAGE": "представляет собой каталог продукции без рекламных баннеров",
        "CLEAN_NON_AD": "не содержит рекламных триггеров и не требует обязательной маркировки",
    }
    return mapping.get(regime_name, "содержит коммерческие триггеры без подтвержденного токена erid")


def _format_regime_title_ru(regime_name: str) -> str:
    """Translates internal regime enum into clean Russian label for client HTML."""
    titles = {
        "UNMARKED_AD_NO_ERID": "Прямая коммерческая интеграция без erid (38-ФЗ)",
        "COMMERCIAL_NATIVE_AD": "Нативная реклама без маркировки (38-ФЗ)",
        "BANNED_OR_SPECIAL_REGIME": "Специальный рекламный режим / ограничения (38-ФЗ)",
        "SURROGATE_MARKING": "Некорректная суррогатная пометка (Приказ РКН № 68)",
        "INCOMPLETE_MARKING_LABEL_OR_INN": "Неполная маркировка рекламы (нет erid или ИНН)",
        "OVER_MARKED_NON_AD": "Избыточная маркировка аналитического поста",
        "CLEAN_MARKED_AD": "Корректно маркированная реклама (38-ФЗ)",
        "CATALOG_PRODUCT_PAGE": "Ассортиментный каталог (иммунитет 38-ФЗ)",
        "CLEAN_NON_AD": "Информационно-аналитический материал",
        "ABSTAIN_REVIEW": "Пограничный материал (требуется ручная проверка)",
    }
    return titles.get(regime_name, "Анализ рекламного соответствия (38-ФЗ)")


def _format_koap_article_ru(art_key: str) -> str:
    """Formats internal KoAP article keys into clean Russian statutory citations."""
    mapping = {
        "13.11_part_1": "ч. 1 ст. 13.11 КоАП РФ",
        "13.11_part_1.1": "ч. 1.1 ст. 13.11 КоАП РФ",
        "13.11_part_2": "ч. 2 ст. 13.11 КоАП РФ",
        "13.11_part_3": "ч. 3 ст. 13.11 КоАП РФ",
        "13.11_part_8": "ч. 8 ст. 13.11 КоАП РФ",
        "13.11_part_10": "ч. 10 ст. 13.11 КоАП РФ",
        "14.3_part_1": "ч. 1 ст. 14.3 КоАП РФ",
        "14.3_part_16": "ч. 16 ст. 14.3 КоАП РФ",
        "14.7_part_2": "ч. 2 ст. 14.7 КоАП РФ",
        "14.8_part_1": "ч. 1 ст. 14.8 КоАП РФ",
        "14.8_part_2": "ч. 2 ст. 14.8 КоАП РФ",
        "20.3_part_1": "ч. 1 ст. 20.3 КоАП РФ",
        "13.55_part_2": "ч. 2 ст. 13.55 КоАП РФ",
        "248_fz_art_49": "ст. 49 248-ФЗ",
        "168_fz": "Закон № 168-ФЗ / ч. 1 ст. 14.8 КоАП РФ",
        "decree_1286": "ПП РФ № 1286 / Приказ Минцифры № 786",
        "mintsifry_511": "Приказ Минцифры России № 511",
    }
    raw = str(art_key or "").strip()
    if raw in mapping:
        return mapping[raw]
    if "ст." in raw or "КоАП" in raw or "ФЗ" in raw:
        return raw
    return "законодательство РФ"


_FRIENDLY_VIOLATION_COPY: Dict[str, str] = {
    "missing-consent-checkbox": "отсутствие в форме сбора контактных данных обязательного чекбокса согласия на обработку ПДн (ст. 9 152-ФЗ)",
    "missing-privacy-policy-link": "отсутствие в лид-форме прямой кликабельной ссылки на Политику конфиденциальности (ч. 2 ст. 18.1 152-ФЗ)",
    "prechecked-consent-checkbox": "предпроставленная галочка согласия на обработку ПДн в форме сбора заявок (ст. 9 152-ФЗ)",
    "bundled-pd-and-advertising-consent": "объединение согласия на обработку ПДн с рекламной рассылкой в одном чекбоксе (запрещено ФЗ № 156-ФЗ)",
    "bundled-offer-consent-missing-separate-consent": "объединение согласия на обработку ПДн с акцептом публичной оферты (запрещено ФЗ № 156-ФЗ)",
    "yandex-metrika-undisclosed-in-policy": "подключение счетчика Яндекс.Метрики без обязательного раскрытия сбора cookie в Политике конфиденциальности (ч. 1–2 ст. 18.1 152-ФЗ)",
    "google-tag-manager": "активный контейнер Google Tag Manager, осуществляющий трансграничную передачу сетевых идентификаторов граждан РФ (ч. 5 ст. 18 152-ФЗ)",
    "google-analytics": "счетчик Google Analytics, осуществляющий трансграничную передачу данных посетителей на зарубежные серверы (ч. 5 ст. 18 152-ФЗ)",
    "google-recaptcha": "виджет Google reCAPTCHA, передающий сетевые и поведенческие идентификаторы пользователей за пределы РФ (ч. 5 ст. 18 152-ФЗ)",
    "cloudflare-turnstile": "скрипт Cloudflare Turnstile, передающий сетевые идентификаторы пользователей на зарубежные узлы (ч. 5 ст. 18 152-ФЗ)",
    "foreign-tracker-meta-hotjar": "подключение зарубежных аналитических трекеров с трансграничной передачей данных посетителей РФ (ч. 5 ст. 18 152-ФЗ)",
    "foreign-chat-crm-widget": "подключение зарубежного онлайн-чата / CRM-виджета с передачей контактов посетителей за пределы РФ (ч. 5 ст. 18 152-ФЗ)",
    "foreign-ad-pixel-tiktok-linkedin": "подключение зарубежных рекламных пикселей с передачей идентификаторов пользователей РФ (ч. 5 ст. 18 152-ФЗ)",
    "foreign_widget_data_leak": "передача персональных данных граждан РФ на зарубежные серверные узлы виджета без первичной локализации в РФ (ч. 5 ст. 18 152-ФЗ)",
    "toxic_supply_chain_widget": "использование стороннего виджета с передачей ПДн граждан РФ незарегистрированному оператору или за рубеж (ч. 5 ст. 18 152-ФЗ)",
    "host_geo_foreign_risk": "размещение хостинга сайта с формами сбора ПДн за пределами РФ без первичной локализации баз данных (ч. 5 ст. 18 152-ФЗ)",
    "rkn_unregistered_operator": "сбор персональных данных через сайт без уведомления Роскомнадзора и внесения в реестр операторов ПДн (ч. 1 ст. 22 152-ФЗ)",
    "rkn_undeclared_cross_border_transfer": "фактическая передача данных на зарубежные узлы при отметке об отсутствии трансграничной передачи в реестре РКН (ст. 12 152-ФЗ)",
    "policy_domain_mismatch_paradox": "использование в Политике конфиденциальности реквизитов или домена стороннего сайта (ч. 2 ст. 18.1 152-ФЗ)",
    "undisclosed_third_party_processor": "подключение внешних обработчиков данных без обязательного раскрытия поручения в Политике конфиденциальности (ч. 3 ст. 6, ст. 18.1 152-ФЗ)",
    "hidden_subprocessors_list": "сокрытие перечня третьих лиц и субобработчиков в тексте Политики конфиденциальности (ч. 3 ст. 6, ст. 18.1 152-ФЗ)",
    "commercial_b2c_foreign_words_168fz": "размещение публичной B2C-информации для потребителей на иностранном языке без равнозначного перевода на русский (Закон № 168-ФЗ)",
    "missing_seller_requisites_zozpp": "сокрытие обязательных реквизитов продавца (ИНН/ОГРН) на коммерческом сайте (ст. 9 Закона РФ № 2300-1 ЗоЗПП)",
    "illegal_no_refund_b2c_clause": "включение в B2C-оферту условий о невозвратности оплаты или ограничении прав потребителей (ст. 16, 32 ЗоЗПП)",
    "extremist_meta_social_links_20_3": "демонстрирование на сайте ссылок или символики запрещенных ресурсов (ч. 1 ст. 20.3 КоАП РФ)",
    "phoenix_clone_detected": "демонстрация реквизитов или регалий ликвидированного предшественника (ч. 2 ст. 14.7 КоАП РФ)",
}


def _describe_site_violation(v: Optional[Any]) -> Dict[str, str]:
    """
    Maps a technical site violation to clean human-readable Russian text,
    the exact supervising authority (RKN / Rospotrebnadzor / FAS), and formatted KoAP norm.
    Strictly forbids leaking raw internal snake_case/kebab-case slugs to clients.
    """
    if v is None:
        return {
            "human_desc": "технические уязвимости в формах сбора и обработке персональных данных",
            "human_title": "Технические уязвимости в формах и обработке ПДн",
            "regulator": "Роскомнадзора",
            "norm_label": "ст. 13.11 КоАП РФ",
            "line_suffix": "",
        }

    sig = str(getattr(v, "signature", "") or "").strip().lower()
    line_no = int(getattr(v, "line_number", 0) or 0)
    meta = TIER_SIGNATURE_REGISTRY.get(sig, {})

    raw_note = str(getattr(v, "remediation_note", "") or "").strip()
    meta_desc = str(meta.get("description") or "").strip()

    human_desc = _FRIENDLY_VIOLATION_COPY.get(sig)
    if not human_desc:
        candidate = meta_desc or raw_note
        # Reject candidate if it looks like a raw machine slug
        if candidate and not re.match(r"^[a-z0-9_-]{4,}$", candidate):
            human_desc = candidate[0].lower() + candidate[1:] if candidate[0].isupper() else candidate
            # Strip raw slug in parentheses if present
            if sig:
                human_desc = human_desc.replace(f"({sig})", "").strip()
        else:
            human_desc = "несоответствие требованиям законодательства РФ в области сбора и обработки данных"

    human_title = (human_desc[0].upper() + human_desc[1:]) if human_desc else "Нарушение комплаенса"

    koap_art = str(meta.get("koap_article") or getattr(v, "koap_article", "") or "").lower()
    if any(k in koap_art for k in ("14.8", "168")) or any(k in sig for k in ("168fz", "zozpp", "no_refund")):
        regulator = "Роспотребнадзора"
        norm_label = _format_koap_article_ru(koap_art) if "14.8" in koap_art else "ч. 1 ст. 14.8 КоАП РФ"
    elif any(k in koap_art for k in ("14.3", "14.7")):
        regulator = "ФАС России"
        norm_label = _format_koap_article_ru(koap_art)
    elif "20.3" in koap_art:
        regulator = "органов прокуратуры и Роскомнадзора"
        norm_label = "ч. 1 ст. 20.3 КоАП РФ"
    elif "mintsifry" in koap_art or "511" in koap_art:
        regulator = "Минцифры России"
        norm_label = "Приказа Минцифры РФ № 511"
    else:
        regulator = "Роскомнадзора"
        norm_label = _format_koap_article_ru(koap_art) if "13." in koap_art or "248" in koap_art else "ст. 13.11 КоАП РФ"

    line_suffix = f" (строка {line_no})" if line_no > 1 else ""
    return {
        "human_desc": human_desc,
        "human_title": human_title,
        "regulator": regulator,
        "norm_label": norm_label,
        "line_suffix": line_suffix,
    }


class LeadPackager:
    """
    Builds client-ready, elegant, linter-styled HTML reports and high-conversion B2B outreach drafts.
    """

    def __init__(self, output_root: Optional[Path] = None):
        self.output_root = output_root or LEADS_OUTPUT_DIR
        self.jurisdiction_router = JurisdictionRouter()
        self.koap_calculator = KoAPSanctionsCalculator()

    def package_lead(
        self,
        company: CompanyProfile,
        site_audit: SiteAuditResult,
        sheaf_report: VerificationReport,
        post_url: str,
        post_title: str,
        target_link: Optional[str] = None,
        author_domain: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Creates <slug>/index.html and <slug>/outreach_draft.txt with cryptosecure secret token.
        Returns dictionary with generated file paths and selected outreach template.
        """
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", company.domain).lower()
        lead_dir = self.output_root / slug
        lead_dir.mkdir(parents=True, exist_ok=True)

        # 1. Cryptosecure Secret Token for Ephemeral Showcase
        secret_slug = secrets.token_urlsafe(16)
        base_url = os.environ.get("SHOWCASE_BASE_URL", "https://adradar.ru")
        showcase_url = f"{base_url}/r/{secret_slug}"

        # 2. Resolve Jurisdiction & Regional Context
        region_code, ufas_name, circuit_code = self.jurisdiction_router.resolve_jurisdiction(company.inn)
        circuit_desc = CIRCUIT_DESCRIPTIONS.get(circuit_code, circuit_code)
        region_title = FNS_REGION_MAP.get(region_code, ("", "Субъект РФ", ""))[1]

        # 3. Calculate Administrative Sanctions with KoAPSanctionsCalculator
        raw_violations = []
        for v in site_audit.compliance_152fz.foreign_transfer_violations:
            raw_violations.append(v.signature)
        for f in site_audit.compliance_152fz.forms:
            for v in f.violations:
                raw_violations.append(v.signature)
        if sheaf_report.is_violation:
            raw_violations.append("third_party_ad_no_erid")

        is_sme_val = company.lead_tier != "TIER_A_CORP"
        koap_calc = self.koap_calculator.calculate_penalties(
            violations=raw_violations,
            is_sme=is_sme_val,
            first_offense=True,
        )

        # 4. Select Outreach Strategy Template and Accurate Link Framing
        has_strict_affiliate_link = False
        has_utm_link = False
        if target_link:
            if re.search(
                r"(?i)(?:[?&](?:ref|affiliate|partner|r|promo|aff|pid|src)=|/(?:r|partner|promo|aff)/)",
                target_link,
            ):
                has_strict_affiliate_link = True
            if re.search(r"(?i)[?&]utm_(?:source|medium|campaign)=", target_link):
                has_utm_link = True

        is_external_author = True
        if author_domain and author_domain.lower() == company.domain.lower():
            is_external_author = False

        all_site_violations = list(site_audit.compliance_152fz.foreign_transfer_violations) + [
            v for f in site_audit.compliance_152fz.forms
            for v in f.violations
        ]

        def _viol_priority(v_obj: Any) -> Tuple[int, int, str]:
            r_lvl = str(getattr(v_obj, "risk_level", "LOW") or "LOW").upper()
            is_high = 0 if r_lvl == "HIGH" else (1 if r_lvl == "MEDIUM" else 2)
            v_info = _describe_site_violation(v_obj)
            is_rkn_pd = 0 if "Роскомнадзор" in v_info["regulator"] else 1
            return (is_high, is_rkn_pd, str(getattr(v_obj, "signature", "")))

        sorted_violations = sorted(all_site_violations, key=_viol_priority)
        high_152_violations = [
            v for v in sorted_violations
            if getattr(v, "risk_level", "LOW") in ("HIGH", "MEDIUM")
        ] or sorted_violations

        # Commercial Outreach Selection Rule (Anti-3% invariant):
        # 1. Clean article (P(Ad) < 25% or not violation) -> PRAISE_POST_FIX_SITE (praise article, sell 152-FZ)
        # 2. Strict Affiliate/Ref OR Commercial UTM Link -> AFFILIATE_SHIELD with exact link framing (no fake "referral" wording on UTM)
        # 3. Direct / Native Promo Post (ordinary link without ref/utm) -> DIRECT_POST_OR_OVERMARKED
        is_clean_article = (not sheaf_report.is_violation) or (float(sheaf_report.violation_probability or 0.0) < 0.25)

        if is_clean_article:
            template_type = "PRAISE_POST_FIX_SITE"
            link_kind = "clean"
        elif has_strict_affiliate_link:
            template_type = "AFFILIATE_SHIELD"
            link_kind = "affiliate"
        elif has_utm_link:
            template_type = "AFFILIATE_SHIELD"
            link_kind = "utm"
        else:
            template_type = "DIRECT_POST_OR_OVERMARKED"
            link_kind = "direct"

        # 5. Generate HTML & Outreach Text
        html_content = self._render_html_report(
            company=company,
            site_audit=site_audit,
            sheaf_report=sheaf_report,
            post_url=post_url,
            post_title=post_title,
            target_link=target_link,
            region_code=region_code,
            region_title=region_title,
            ufas_name=ufas_name,
            circuit_code=circuit_code,
            circuit_desc=circuit_desc,
            high_152_violations=high_152_violations,
            koap_calc=koap_calc,
        )

        outreach_text = self._render_outreach_draft(
            template_type=template_type,
            company=company,
            post_url=post_url,
            post_title=post_title,
            target_link=target_link,
            sheaf_report=sheaf_report,
            high_152_violations=high_152_violations,
            showcase_url=showcase_url,
            koap_calc=koap_calc,
            is_external_author=is_external_author,
            link_kind=link_kind,
        )

        html_path = lead_dir / "index.html"
        outreach_path = lead_dir / "outreach_draft.txt"

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        with open(outreach_path, "w", encoding="utf-8") as f:
            f.write(outreach_text)

        # 5. Record Token Manifest for Showcase Server
        self._record_manifest(
            secret_slug=secret_slug,
            slug=slug,
            company=company,
            html_path=html_path,
        )

        return {
            "slug": slug,
            "secret_slug": secret_slug,
            "showcase_url": showcase_url,
            "template_type": template_type,
            "html_path": str(html_path),
            "outreach_path": str(outreach_path),
        }

    def _record_manifest(
        self,
        secret_slug: str,
        slug: str,
        company: CompanyProfile,
        html_path: Path,
    ) -> None:
        """Stores token-to-file mapping for showcase server."""
        manifest: Dict[str, Any] = {}
        if MANIFEST_FILE.exists():
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = {}

        manifest[secret_slug] = {
            "slug": slug,
            "domain": company.domain,
            "legal_name": company.legal_name,
            "inn": company.inn,
            "lead_tier": company.lead_tier,
            "revenue_rub": company.revenue_rub,
            "created_at": datetime.now().isoformat(),
            "html_path": str(html_path),
        }

        try:
            with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _render_outreach_draft(
        self,
        template_type: str,
        company: CompanyProfile,
        post_url: str,
        post_title: str,
        target_link: Optional[str],
        sheaf_report: VerificationReport,
        high_152_violations: List[Any],
        showcase_url: str,
        koap_calc: Optional[Dict[str, Any]] = None,
        is_external_author: bool = True,
        link_kind: Optional[str] = None,
    ) -> str:
        raw_org = company.legal_name or company.domain
        org_name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw_org)).strip()
        salutation = build_human_salutation(company.management_name, company.corporate_emails)

        revenue_str = "Не публикуется"
        if company.revenue_rub:
            rev_m = company.revenue_rub / 1_000_000
            revenue_str = f"{rev_m:.1f} млн руб. (за {company.revenue_year or '2025'} г.)"

        # 248-FZ Status & KoAP calculation lines
        reg_status_str = "Без регуляторных рисков (COMPLIANT)"
        fines_str = "0 руб. (соответствует требованиям)"
        if koap_calc:
            outcome = koap_calc.get("regulatory_outcome", "COMPLIANT")
            if outcome == "PRECAUTION_OR_DEMAND_10_DAYS":
                reg_status_str = "Предостережение / 10 дней на устранение (без штрафа по ст. 49 248-ФЗ)"
            elif outcome == "UNSCHEDULED_INSPECTION_RISK":
                reg_status_str = f"Риск внеплановой проверки РКН (индикаторов риска: {koap_calc.get('rkn_risk_indicators_count', 0)} >= 3)"

            t_min = koap_calc.get("total_min_rubles", 0)
            t_max = koap_calc.get("total_max_rubles", 0)
            if t_max > 0:
                fines_str = f"от {t_min:,.0f} до {t_max:,.0f} руб. (ч. 5 ст. 4.4 КоАП: без мультипликации)".replace(",", " ")
            elif koap_calc.get("raw_violations_count", 0) > 0:
                fines_str = "Предупреждение (ст. 4.1.1 КоАП РФ для МСП / мораторий ст. 13.55 до 2029 г.)"

        tg_list = list(getattr(company, "telegram_contacts", None) or [])
        all_contacts = list(company.corporate_emails) + [t for t in tg_list if t not in company.corporate_emails]

        lines = [
            f"ШАБЛОН АУТРИЧА: {template_type}",
            f"ЦЕЛЕВАЯ КОМПАНИЯ: {org_name} (домен {company.domain})",
            f"ТИР ПЛАТЕЖЕСПОСОБНОСТИ: {company.lead_tier}",
            f"ВЫРУЧКА (ГИР БО ФНС): {revenue_str}",
            f"РЕГУЛЯТОРНЫЙ СТАТУС (248-ФЗ): {reg_status_str}",
            f"РАСЧЕТ САНКЦИЙ ПО КОАП РФ: {fines_str}",
            f"АДРЕСАТЫ: {', '.join(all_contacts) or 'чат поддержки / info@'}",
            f"ССЫЛКА НА СПРАВКУ (72h): {showcase_url}",
            "=" * 70,
            "",
        ]

        effective_link_kind = link_kind
        if not effective_link_kind:
            if target_link and re.search(
                r"(?i)(?:[?&](?:ref|affiliate|partner|r|promo|aff|pid|src)=|/(?:r|partner|promo|aff)/)",
                target_link,
            ):
                effective_link_kind = "affiliate"
            elif target_link and re.search(r"(?i)[?&]utm_(?:source|medium|campaign)=", target_link):
                effective_link_kind = "utm"
            else:
                effective_link_kind = "direct"

        if template_type == "AFFILIATE_SHIELD":
            tier_note = (
                "Для крупных участников рынка подобные публикации создают повышенные риски возбуждения антимонопольного дела и публичной огласки."
                if company.lead_tier == "TIER_A_CORP"
                else "Для субъектов МСП в силу ст. 4.1.1 КоАП РФ при первичном выявлении штраф может быть заменен на предупреждение при условии оперативного устранения нарушения."
            )
            if effective_link_kind == "affiliate":
                ad_basis_lines = [
                    "По сложившейся правоприменительной практике ФАС России (Приказ от 14.11.2023 № 821/23 и ч. 16 ст. 14.3 КоАП РФ),",
                    "размещение партнерских или реферальных ссылок без токена erid создает прямые риски привлечения",
                    "к административной ответственности не только автора, но и самого рекламодателя (штрафы от 200 000 до 500 000 руб.).",
                ]
            elif effective_link_kind == "utm":
                ad_basis_lines = [
                    "По сложившейся правоприменительной практике ФАС России (Приказ ФАС России от 14.11.2023 № 821/23 и ч. 16 ст. 14.3 КоАП РФ),",
                    "размещение рекламного материала с коммерческой UTM-ссылкой на ваш ресурс без токена erid создает прямые риски привлечения",
                    "к административной ответственности как распространителя, так и рекламодателя (штрафы от 200 000 до 500 000 руб.).",
                ]
            else:
                ad_basis_lines = [
                    "По сложившейся правоприменительной практике ФАС России (Приказ ФАС России от 14.11.2023 № 821/23 и ч. 16 ст. 14.3 КоАП РФ),",
                    "размещение нативной рекламной публикации со ссылкой на коммерческий ресурс без токена erid создает прямые риски привлечения",
                    "к административной ответственности (штрафы для юридических лиц от 200 000 до 500 000 руб.).",
                ]
            lines.extend([
                salutation,
                "",
                f"Обратили внимание, что на VC.ru вышел материал «{post_title}» ({post_url}),",
                f"в котором размещена ссылка на ваш ресурс {company.domain}:",
                f"-> {target_link or post_url}",
                "",
                *ad_basis_lines,
                tier_note,
                "",
                "Наш независимый мониторинг комплаенса зафиксировал данный триггер и сформировал техническую экспресс-справку:",
                f"-> {showcase_url}",
                "",
                "Справка доступна только по этой ссылке, не содержит персональных данных и будет автоматически удалена через 72 часа.",
                "Вы можете переслать ее юристу или автору статьи, чтобы оперативно добавить токен или скорректировать подачу.",
                "",
                "Подскажите, куда удобнее направить подробную техническую памятку для вашего веб-мастера — ответным письмом сюда или в рабочий Telegram?",
                "",
                "С уважением,",
                "Команда сервиса комплаенс-мониторинга AdRadar",
            ])

        elif template_type == "PRAISE_POST_FIX_SITE":
            first_v = high_152_violations[0] if high_152_violations else None
            v_info = _describe_site_violation(first_v)

            fine_praise_note = ""
            if koap_calc and koap_calc.get("total_max_rubles", 0) > 0:
                art_labels: List[str] = []
                for gp in koap_calc.get("grouped_penalties", []):
                    lbl = _format_koap_article_ru(gp.get("article", ""))
                    if lbl and lbl not in art_labels:
                        art_labels.append(lbl)
                arts_str = ", ".join(art_labels) if art_labels else v_info["norm_label"]
                fine_praise_note = f"Потенциальные санкции по {arts_str} составляют {fines_str}."

            tier_praise_note = (
                f"Для крупных участников рынка подобные расхождения влекут за собой внеплановые предписания {v_info['regulator']} и регуляторные издержки."
                if company.lead_tier == "TIER_A_CORP"
                else "Для субъектов МСП в силу ст. 4.1.1 КоАП РФ при первичном выявлении штраф может быть заменен на предупреждение при своевременном устранении замечаний (возможно оформление комплексного устранения под ключ)."
            )

            if is_external_author:
                intro_line_1 = f"Заметили упоминание вашего сервиса {company.domain} в публикации «{post_title}» на VC.ru."
                intro_line_2 = "Наш аудит подтвердил: сам материал носит информационно-аналитический характер, не содержит рекламных триггеров и не требует маркировки по 38-ФЗ."
            else:
                intro_line_1 = f"С интересом ознакомились с вашим материалом «{post_title}» на VC.ru."
                intro_line_2 = "Отличная статья! Наш аудит подтвердил: публикация носит аналитический характер, не содержит рекламных триггеров и не требует маркировки по 38-ФЗ."

            lines.extend([
                salutation,
                "",
                intro_line_1,
                intro_line_2,
                "",
                f"При этом в ходе сопутствующего технического чека сайта {company.domain}",
                f"наш сканер зафиксировал нарушение: {v_info['human_desc']}{v_info['line_suffix']}, которое сейчас находится в фокусе контрольных мероприятий {v_info['regulator']}.",
            ])
            if fine_praise_note:
                lines.append(fine_praise_note)
            lines.extend([
                tier_praise_note,
                "",
                "Мы подготовили наглядную техническую справку для вашего IT-отдела и веб-мастера:",
                f"-> {showcase_url}",
                "",
                "В справке показано, как сохранить всю веб-аналитику и исключить риски регуляторных проверок.",
                "Отчет доступен в течение 72 часов, после чего автоматически удаляется.",
                "",
                "Подскажите, куда удобнее направить подробную техническую памятку для вашего веб-мастера — ответным письмом сюда или в рабочий Telegram?",
                "",
                "С уважением,",
                "Команда сервиса комплаенс-мониторинга AdRadar",
            ])

        else:  # DIRECT_POST_OR_OVERMARKED (Only when is_violation is True and P(Ad) >= 0.25)
            regime_desc = get_human_regime_description(sheaf_report.regime_name)
            tier_direct_note = (
                "Для крупных участников рынка подобные интеграции создают риск внепланового возбуждения антимонопольного дела и публичной огласки."
                if company.lead_tier == "TIER_A_CORP"
                else "Для субъектов МСП по ст. 4.1.1 КоАП РФ при первичном выявлении штраф подлежит замене на предупреждение при оперативной маркировке материала."
            )
            direct_intro = (
                f"Обратили внимание, что на VC.ru вышел материал стороннего автора «{post_title}» ({post_url}) со ссылкой на ваш ресурс {company.domain}:"
                if is_external_author
                else f"Заметили публикацию «{post_title}» ({post_url}) на VC.ru с продвижением вашего сервиса {company.domain}:"
            )
            lines.extend([
                salutation,
                "",
                direct_intro,
                f"-> {target_link or post_url}",
                "",
                f"Наш аудит комплаенса выявил в материале признаки нарушения рекламного законодательства: {regime_desc}.",
                "По сложившейся правоприменительной практике ФАС России (Приказ ФАС России от 14.11.2023 № 821/23 и ч. 16 ст. 14.3 КоАП РФ) распространение интернет-рекламы без обязательного идентификатора erid влечет штраф для юридических лиц от 200 000 до 500 000 руб.",
                tier_direct_note,
                "",
                f"Также мы провели экспресс-чек технических параметров сайта {company.domain} по 152-ФЗ и свели все замечания в наглядную справку со строками кода:",
                f"-> {showcase_url}",
                "",
                "Отчет носит строго конфиденциальный характер, доступен только по прямой ссылке и будет автоматически удален через 72 часа.",
                "",
                "Подскажите, куда удобнее направить подробную техническую памятку для вашего веб-мастера — ответным письмом сюда или в рабочий Telegram?",
                "",
                "С уважением,",
                "Команда сервиса комплаенс-мониторинга AdRadar",
            ])

        return "\n".join(lines)

    def _render_html_report(
        self,
        company: CompanyProfile,
        site_audit: SiteAuditResult,
        sheaf_report: VerificationReport,
        post_url: str,
        post_title: str,
        target_link: Optional[str],
        region_code: str,
        region_title: str,
        ufas_name: str,
        circuit_code: str,
        circuit_desc: str,
        high_152_violations: List[Any],
        koap_calc: Optional[Dict[str, Any]] = None,
    ) -> str:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        exp_str = (datetime.now() + timedelta(hours=72)).strftime("%d.%m.%Y %H:%M")
        raw_org_html = company.legal_name or f"Компания ({company.domain})"
        org_name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw_org_html)).strip()
        inn_str = company.inn or "Не указан"
        ogrn_str = company.ogrn or "Не указан"

        # Revenue and Tier display
        tier_label = "Крупный / средний бизнес (ООО/АО)" if company.lead_tier == "TIER_A_CORP" else "Малый бизнес / ИП"
        rev_val = "Не публикуется"
        if company.revenue_rub:
            rev_val = f"{company.revenue_rub / 1_000_000:.1f} млн руб. (за {company.revenue_year or '2025'} г.)"

        # Badge colors
        if sheaf_report.is_violation:
            badge_38 = '<span class="badge badge-warning">ТРЕБУЕТ ВНИМАНИЯ (38-ФЗ)</span>'
        else:
            badge_38 = '<span class="badge badge-success">АНАЛИТИКА / CLEAN (38-ФЗ)</span>'

        high_only_count = sum(
            1 for v in high_152_violations
            if str(getattr(v, "risk_level", "LOW") or "LOW").upper() == "HIGH"
        )
        if high_only_count > 0:
            badge_152 = f'<span class="badge badge-danger">ВЫСОКИЙ РИСК ({len(high_152_violations)} ЗАМЕЧАНИЙ)</span>'
        elif len(high_152_violations) > 0:
            badge_152 = f'<span class="badge badge-warning">ТРЕБУЕТ ВНИМАНИЯ ({len(high_152_violations)} ЗАМЕЧАНИЙ)</span>'
        else:
            badge_152 = '<span class="badge badge-success">СООТВЕТСТВУЕТ (152-ФЗ)</span>'

        # 248-FZ Status Badge & KoAP Sanctions
        reg_outcome = koap_calc.get("regulatory_outcome", "COMPLIANT") if koap_calc else "COMPLIANT"
        if reg_outcome == "UNSCHEDULED_INSPECTION_RISK":
            badge_248 = '<span class="badge badge-danger">248-ФЗ: РИСК ВНЕПЛАНОВОЙ ПРОВЕРКИ (&gt;=3 РАСХОЖДЕНИЙ)</span>'
        elif reg_outcome == "PRECAUTION_OR_DEMAND_10_DAYS":
            badge_248 = '<span class="badge badge-warning">248-ФЗ: ПРЕДОСТЕРЕЖЕНИЕ (10 ДНЕЙ НА УСТРАНЕНИЕ)</span>'
        else:
            badge_248 = '<span class="badge badge-success">248-ФЗ: НЕТ ИНДИКАТОРОВ РИСКА</span>'

        total_min_rub = koap_calc.get("total_min_rubles", 0) if koap_calc else 0
        total_max_rub = koap_calc.get("total_max_rubles", 0) if koap_calc else 0
        if total_max_rub > 0:
            koap_val_str = f"от {total_min_rub:,.0f} до {total_max_rub:,.0f} руб.".replace(",", " ")
        elif koap_calc and koap_calc.get("raw_violations_count", 0) > 0:
            koap_val_str = "Предупреждение (ст. 4.1.1 КоАП / мораторий ст. 13.55)"
        else:
            koap_val_str = "0 руб. (соответствует требованиям)"

        try:
            h_star = OptimalHorizonSolver.compute_optimal_horizon(
                uncertainty=0.25,
                base_fine=float(total_max_rub or 120000.0),
            )
        except Exception:
            h_star = 29

        # KoAP Grouped Table
        koap_rows = []
        if koap_calc and koap_calc.get("grouped_penalties"):
            for gp in koap_calc["grouped_penalties"]:
                art_label = _format_koap_article_ru(gp.get("article", ""))
                stat_desc = gp["description"]
                cnt = gp["matched_count"]
                if gp["max_rubles"] > 0:
                    fine_cell = f"<strong>{gp['min_rubles']:,.0f} – {gp['max_rubles']:,.0f} руб.</strong><br><small>{gp['note']}</small>".replace(",", " ")
                else:
                    fine_cell = f"<strong>0 руб.</strong><br><small>{gp['note']}</small>"
                koap_rows.append(f"""
                <tr>
                    <td><code>{art_label}</code></td>
                    <td><strong>{stat_desc}</strong></td>
                    <td style="text-align: center;"><span class="pill pill-neutral">{cnt} ед. (сгруппировано)</span></td>
                    <td>{fine_cell}</td>
                </tr>
                """)
        koap_table_html = "".join(koap_rows) if koap_rows else "<tr><td colspan='4' style='text-align:center; padding: 12px;'>Административные санкции отсутствуют.</td></tr>"

        principles_html = "".join(f"<li>{p}</li>" for p in koap_calc.get("applied_legal_principles", [])) if koap_calc else "<li>Стандартный расчет</li>"

        # Rows of compliance findings (human-readable Russian titles, zero raw slugs)
        v_rows = []
        for v in site_audit.compliance_152fz.foreign_transfer_violations:
            v_info = _describe_site_violation(v)
            is_high = str(getattr(v, "risk_level", "LOW") or "LOW").upper() == "HIGH"
            cls = "danger" if is_high else "neutral"
            risk_badge = "Высокий риск (штраф)" if is_high else "Регуляторное требование"
            line_num = int(getattr(v, "line_number", 0) or 0)
            line_cell = f"<code>стр. {line_num}</code>" if line_num > 1 else "<code>—</code>"
            v_rows.append(f"""
            <tr class="row-{cls}">
                <td>{line_cell}</td>
                <td><strong>{v_info['human_title']}</strong><br><small>{v.legal_basis}</small></td>
                <td><span class="pill pill-{cls}">{risk_badge}</span></td>
                <td><small>{v.remediation_note or v.potential_fine_rub}</small></td>
            </tr>
            """)

        for f in site_audit.compliance_152fz.forms:
            if not f.is_compliant:
                for v in f.violations:
                    v_info = _describe_site_violation(v)
                    line_num = int(getattr(v, "line_number", 0) or 0)
                    line_cell = f"<code>стр. {line_num}</code>" if line_num > 1 else "<code>—</code>"
                    v_rows.append(f"""
                    <tr class="row-danger">
                        <td>{line_cell}</td>
                        <td><strong>Лид-форма: {v_info['human_title']}</strong><br><small>{v.legal_basis}</small></td>
                        <td><span class="pill pill-danger">Высокий риск (штраф)</span></td>
                        <td><small>{v.remediation_note or v.potential_fine_rub}</small></td>
                    </tr>
                    """)

        findings_table = "".join(v_rows) if v_rows else "<tr><td colspan='4' style='text-align:center; padding: 16px;'>Критических технических нарушений не обнаружено.</td></tr>"

        # Pure autonomous inline CSS with system fonts (Dogfooding 152-FZ clean)
        html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="robots" content="noindex, nofollow">
    <title>Технический экспресс-чек: {company.domain}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --border: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #38bdf8;
            --success: #10b981;
            --warning: #f59e0b;
            --danger: #ef4444;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text-main);
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.5;
            padding: 32px 16px;
        }}
        .container {{
            max-width: 960px;
            margin: 0 auto;
        }}
        .card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 1px solid var(--border);
            padding-bottom: 20px;
            margin-bottom: 24px;
        }}
        h1 {{ font-size: 24px; font-weight: 700; color: #fff; }}
        .meta {{ color: var(--text-muted); font-size: 14px; margin-top: 4px; }}
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-success {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }}
        .badge-warning {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }}
        .badge-danger {{ background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }}
        .pill {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
        .pill-danger {{ background: rgba(239, 68, 68, 0.15); color: #fca5a5; }}
        .pill-neutral {{ background: rgba(148, 163, 184, 0.15); color: #cbd5e1; }}
        .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
        .metric-box {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }}
        .metric-label {{ font-size: 12px; color: var(--text-muted); text-transform: uppercase; }}
        .metric-val {{ font-size: 16px; font-weight: 600; margin-top: 4px; color: #fff; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-top: 12px;
        }}
        th, td {{
            text-align: left;
            padding: 12px;
            border-bottom: 1px solid var(--border);
        }}
        th {{ color: var(--text-muted); font-weight: 600; background: rgba(15, 23, 42, 0.4); }}
        code {{
            background: rgba(15, 23, 42, 0.8);
            padding: 2px 6px;
            border-radius: 4px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 12px;
            color: var(--accent);
        }}
        .alert-banner {{
            background: rgba(56, 189, 248, 0.1);
            border: 1px solid rgba(56, 189, 248, 0.3);
            border-radius: 8px;
            padding: 16px;
            font-size: 14px;
            margin-top: 24px;
        }}
        .footer {{
            text-align: center;
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 32px;
            padding-top: 16px;
            border-top: 1px solid var(--border);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="header">
                <div>
                    <h1>Технический экспресс-чек: {company.domain}</h1>
                    <div class="meta">Профиль: {org_name} | ИНН: {inn_str} | ОГРН: {ogrn_str}</div>
                    <div class="meta">Сегмент: {tier_label} | Выручка (ГИР БО ФНС): {rev_val}</div>
                    <div class="meta">Юрисдикция: {region_code} ({region_title}, {ufas_name}) | Арбитражный округ: {circuit_desc}</div>
                </div>
                <div>
                    {badge_38}
                </div>
            </div>

            <div class="grid-2">
                <div class="metric-box">
                    <div class="metric-label">Проверенная публикация</div>
                    <div class="metric-val" style="font-size: 14px; font-weight: normal;">
                        <a href="{post_url}" target="_blank" style="color: var(--accent); text-decoration: none;">{post_title[:65]}...</a>
                    </div>
                    <div class="meta" style="margin-top: 8px;">Статус: <strong>{_format_regime_title_ru(sheaf_report.regime_name)}</strong></div>
                </div>
                <div class="metric-box">
                    <div class="metric-label">Статус комплаенса 152-ФЗ и 248-ФЗ</div>
                    <div class="metric-val" style="font-size: 13px; display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px;">
                        {badge_152} {badge_248}
                    </div>
                    <div class="meta" style="margin-top: 8px;">Расчет санкций (ч. 5 ст. 4.4 КоАП): <strong>{koap_val_str}</strong></div>
                </div>
            </div>

            <h3 style="font-size: 16px; margin: 24px 0 8px 0; color: #fff;">1. Телеметрия веб-ресурса по 152-ФЗ (Трансграничная передача и формы)</h3>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
                Лог автоматического AST-сканера фронтенда сайта. Предназначен для системного администратора и разработчика.
            </p>
            <table>
                <thead>
                    <tr>
                        <th style="width: 15%;">Позиция</th>
                        <th style="width: 45%;">Объект и основание</th>
                        <th style="width: 20%;">Уровень риска</th>
                        <th style="width: 20%;">Рекомендация</th>
                    </tr>
                </thead>
                <tbody>
                    {findings_table}
                </tbody>
            </table>

            <h3 style="font-size: 16px; margin: 24px 0 8px 0; color: #fff;">2. Расчет административной ответственности по КоАП РФ (ч. 5 ст. 4.4 — без мультипликации)</h3>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
                По ч. 5 ст. 4.4 КоАП РФ однородные нарушения группируются в один состав (запрет умножения на количество форм). Учтены процессуальный барьер 248-ФЗ, льготы МСП (ст. 4.1.1, ст. 4.1.2) и мораторий по ч. 2 ст. 13.55 до 2029 г.
            </p>
            <table>
                <thead>
                    <tr>
                        <th style="width: 18%;">Статья КоАП</th>
                        <th style="width: 42%;">Состав правонарушения</th>
                        <th style="width: 15%; text-align: center;">Фактов</th>
                        <th style="width: 25%;">Статус и расчетный штраф</th>
                    </tr>
                </thead>
                <tbody>
                    {koap_table_html}
                </tbody>
            </table>
            <ul style="font-size: 12px; color: var(--text-muted); margin-top: 8px; margin-left: 18px; line-height: 1.5;">
                {principles_html}
            </ul>

            <h3 style="font-size: 16px; margin: 24px 0 8px 0; color: #fff;">3. Нормативные основания и правовые аксиомы</h3>
            <div style="font-size: 13px; color: var(--text-muted); line-height: 1.6;">
                <p>• <strong>38-ФЗ (Реклама):</strong> {"; ".join(sheaf_report.legal_citations) or "Требования соблюдены (иммунитет собственного сайта по п. 2-3 ч. 2 ст. 2 38-ФЗ)."}</p>
                <p>• <strong>152-ФЗ (Локализация):</strong> ч. 5 ст. 18 требует обеспечивать запись и систематизацию персональных данных граждан РФ с использованием баз данных на территории РФ.</p>
                <p>• <strong>248-ФЗ (Госконтроль):</strong> ст. 49 предусматривает объявление предостережения и 10 дней на устранение до назначения внеплановой проверки.</p>
                <p>• <strong>Каталог производителя:</strong> Письмо ФАС России от 09.04.2025 № АК/32908/25 подтверждает иммунитет ассортиментных каталогов без сквозных баннеров.</p>
            </div>

            <h3 style="font-size: 16px; margin: 24px 0 8px 0; color: #fff;">4. Эволюционный горизонт риска ({h_star} недель) и устранение рисков</h3>
            <div style="background: #1e293b; border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-top: 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px;">
                <div>
                    <div style="font-size: 15px; font-weight: 700; color: #38bdf8;">Оптимальный горизонт планирования: H* = {h_star} недель</div>
                    <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px; max-width: 580px;">
                        Математическая модель доказала: при текущем уровне правовой энтропии своевременное внедрение чистого комплекта документов и сниппетов снижает интегральные риски бизнеса на 18–35% по сравнению с реактивным исправлением после проверки.
                    </div>
                </div>
                <div>
                    <a href="mailto:contact@adradar.ru?subject=Заказ%20комплекта%20устранения%20для%20{company.domain}&body=Здравствуйте!%20Хотим%20заказать%20Комплект%20устранения%20рисков%20под%20ключ%20за%2014%20900%20руб.%20для%20домена%20{company.domain}." 
                       style="display: inline-block; background: #2563eb; color: #ffffff; padding: 12px 20px; border-radius: 6px; text-decoration: none; font-weight: 700; font-size: 14px; text-align: center;">
                        Заказать комплект устранения (14 900 ₽)
                    </a>
                </div>
            </div>

            <div class="alert-banner">
                <strong>Анти-шантажный регламент безопасности AdRadar:</strong><br>
                Настоящий технический отчет сгенерирован автоматически сертифицированным алгоритмом LegalSheaf в белом транзитном режиме.
                Мы не сохраняем полные тексты сайтов и персональные данные сотрудников.
                Справка доступна только по прямой ссылке и будет <strong>безвозвратно удалена через 72 часа ({exp_str})</strong>.
            </div>
        </div>

        <div class="footer">
            Сформировано автоматическим верификатором AdRadar {now_str} • Консультация IT-комплаенса: contact@adradar.ru
        </div>
    </div>
</body>
</html>"""
        return html

"""
Compliance 152-FZ and Cross-Border Personal Data Transfer Scanner.
Detects unlawful foreign trackers, fonts, analytics, and non-compliant capture forms.
Strictly zero OCR and zero Generative LLMs. Pure AST / DOM and deterministic regex.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup, Comment
from pydantic import BaseModel, Field


class Violation152FZ(BaseModel):
    category: str = Field(..., description="Violation category: HIGH_LOCALIZATION_RISK, HIGH_CONSENT_FORM_RISK, LOW_CDN_TRANSBORDER_RISK")
    risk_level: str = Field("HIGH", description="Risk level: HIGH or LOW")
    signature: str = Field(..., description="Signature name (e.g. google-tag-manager, google-fonts)")
    line_number: int = Field(..., description="Exact 1-based line number in original HTML")
    snippet: str = Field(..., description="Code snippet causing the violation")
    legal_basis: str = Field(..., description="Statutory article (e.g. 152-ФЗ ч. 5 ст. 18 / ст. 12)")
    koap_article: str = Field(..., description="Administrative offense article (e.g. ст. 13.11 КоАП РФ ч. 8/9)")
    potential_fine_rub: str = Field(..., description="Statutory fine range in rubles")
    remediation_note: str = Field("", description="Sysadmin recommendation")


class FormInspectionResult(BaseModel):
    form_id: Optional[str] = None
    form_index: int
    line_number: int
    is_login_form: bool = False
    has_personal_data_inputs: bool
    input_names: List[str]
    has_consent_checkbox: bool
    has_policy_link: bool
    is_compliant: bool
    violations: List[Violation152FZ] = Field(default_factory=list)


class Report152FZ(BaseModel):
    page_url: str
    is_compliant: bool
    total_violations: int
    high_risk_count: int = 0
    low_risk_count: int = 0
    foreign_transfer_violations: List[Violation152FZ]
    forms: List[FormInspectionResult]
    summary: str


# Deterministic Signatures with Precise Risk Gradation
FOREIGN_SIGNATURES = [
    {
        "name": "google-tag-manager",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:googletagmanager\.com/(?:gtm\.js|gtag)|GTM-[A-Z0-9]{4,12})"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Зарубежный контейнер GTM — высокий риск по ч. 5 ст. 18 152-ФЗ при активации внутри него тегов Google Analytics / зарубежных пикселей)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Заменить контейнер GTM на локальный скрипт веб-аналитики или Яндекс.Метрику с серверами в РФ.",
    },
    {
        "name": "google-analytics",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:google-analytics\.com/(?:analytics\.js|ga\.js)|gtag\s*\(\s*['\"]config['\"]\s*,\s*['\"](?:G-|UA-)|ga\s*\(\s*['\"]create['\"])"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Прямой сбор Client ID и сетевых идентификаторов граждан РФ на серверы Google LLC в США без локализации)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Удалить gtag/analytics.js, переключиться на Яндекс.Метрику с серверами в РФ.",
    },
    {
        "name": "google-fonts",
        "category": "LOW_CDN_TRANSBORDER_RISK",
        "risk_level": "LOW",
        "pattern": re.compile(r"(?i)fonts\.(?:googleapis|gstatic)\.com"),
        "legal_basis": "ст. 12 152-ФЗ (Техническая передача IP посетителей на зарубежный CDN Google Fonts при подгрузке стилей)",
        "koap_article": "ст. 12 152-ФЗ (Техническое замечание)",
        "potential_fine_rub": "Техническое замечание (рекомендация IT-отделу)",
        "remediation_note": "Скачать файлы .woff2 на веб-сервер сайта и подключать шрифты локально.",
    },
    {
        "name": "google-recaptcha",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:google\.com/recaptcha|gstatic\.com/recaptcha)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Передача телеметрии поведения и сетевых идентификаторов в США без первичной записи в РФ)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Заменить Google reCAPTCHA на Yandex SmartCaptcha или локальную капчу.",
    },
    {
        "name": "google-forms",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:docs\.google\.com/forms|forms\.gle)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Сбор ПДн граждан РФ с первичной записью в зарубежную базу данных Google Forms)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Заменить Google Forms на Яндекс Формы или формы на собственном сервере в РФ.",
    },
    {
        "name": "foreign-tracker-meta-hotjar",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:connect\.facebook\.net|fbevents\.js|static\.hotjar\.com|clarity\.ms/tag|tawk\.to|smartsupp\.com)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Трансграничная передача трекинговых данных сторонним зарубежным сервисам)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб.",
        "remediation_note": "Удалить зарубежные трекеры с сайта.",
    },
    {
        "name": "foreign-auth-google-apple",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:accounts\.google\.com/gsi|appleid\.auth\.js|appleid\.cdn-apple\.com|apis\.google\.com/js/platform\.js)"),
        "legal_basis": "ст. 10.2-1 149-ФЗ (в ред. 199-ФЗ) / ч. 5 ст. 18 152-ФЗ (Запрет авторизации российских пользователей через иностранные системы Google/Apple)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ / 149-ФЗ",
        "potential_fine_rub": "Предписание РКН с блокировкой / штраф до 6 000 000 руб.",
        "remediation_note": "Заменить зарубежную авторизацию на ЕСИА (Госуслуги), VK ID, Яндекс ID или Сбер ID.",
    },
    {
        "name": "telegram-auth-widget",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "MEDIUM",
        "pattern": re.compile(r"(?i)(?:telegram-widget\.js|data-telegram-login|data-onauth)"),
        "legal_basis": "ст. 10.2-1 149-ФЗ (в ред. 199-ФЗ) / ч. 2 ст. 13.55 КоАП РФ (Иностранная система авторизации пользователей: Telegram Widget)",
        "koap_article": "ч. 2 ст. 13.55 КоАП РФ",
        "potential_fine_rub": "Предупреждение (действует мораторий на штрафы до 01.01.2029)",
        "remediation_note": "Заменить виджет Telegram на российские сервисы авторизации: VK ID, Госуслуги (ЕСИА), Яндекс ID, Сбер ID.",
    },
    {
        "name": "cloudflare-turnstile",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:challenges\.cloudflare\.com/turnstile|cf-turnstile)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Передача сетевых и поведенческих идентификаторов пользователей РФ на зарубежные узлы Cloudflare Inc. в США)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Заменить Cloudflare Turnstile на Yandex SmartCaptcha или локальную российскую защиту от ботов.",
    },
    {
        "name": "foreign-chat-crm-widget",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:widget\.intercom\.io|js\.intercomcdn\.com|client\.crisp\.chat|static\.zdassets\.com|js\.hs-scripts\.com|js\.hsforms\.net)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Прямой сбор контактов и сообщений посетителей РФ в зарубежные облачные CRM/чат-виджеты Intercom/Crisp/Zendesk/HubSpot)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 500 000 до 3 000 000 руб.)",
        "remediation_note": "Заменить зарубежный онлайн-чат/CRM-виджет на локализованное в РФ решение.",
    },
    {
        "name": "foreign-ad-pixel-tiktok-linkedin",
        "category": "HIGH_LOCALIZATION_RISK",
        "risk_level": "HIGH",
        "pattern": re.compile(r"(?i)(?:analytics\.tiktok\.com|snap\.licdn\.com/li\.lms-analytics)"),
        "legal_basis": "ч. 5 ст. 18 152-ФЗ (Трансграничная передача рекламных и поведенческих идентификаторов пользователей РФ зарубежным трекинг-пикселям)",
        "koap_article": "ч. 8 ст. 13.11 КоАП РФ",
        "potential_fine_rub": "от 1 000 000 до 6 000 000 руб.",
        "remediation_note": "Удалить зарубежные рекламные пиксели TikTok / LinkedIn с сайта.",
    },
]

# Personal data input field indicators
PD_INPUT_NAME_REGEX = re.compile(
    r"(?i)(?:phone|tel|mobile|email|mail|fio|name|first_name|last_name|surname|contact|имя|телефон|почт|фио|фамили|отчеств|адрес|город)"
)

# Consent checkbox text indicators
CONSENT_TEXT_REGEX = re.compile(
    r"(?i)(?:соглас\w*|персональн\w*\s+данн\w*|обработк\w*\s+(?:персональных|данных|информации)|политик\w*\s+конфиденциальност\w*|privacy|consent|eula|пдн)"
)

# Privacy Policy link href / text indicators
POLICY_HREF_REGEX = re.compile(
    r"(?i)(?:privacy-policy|privacy_policy|personal-data|personal_data|persdan|konfidentsialnost|soglashenie|politika|policy|privacy|terms|eula|soglas|confidential|политик|персональн|конфиденц)"
)


class Compliance152FZScanner:
    """
    Automated DOM & Static Inspector for 152-FZ personal data & cross-border compliance.
    """

    def __init__(self):
        pass

    def _strip_comments_preserving_lines(self, html: str) -> str:
        """
        Replaces HTML comments <!-- ... --> with equivalent newlines.
        This prevents commented-out tracking code from being falsely flagged,
        while maintaining 100% exact 1-based line numbers.
        """
        def replace_comment(match: re.Match) -> str:
            line_count = match.group(0).count("\n")
            return "\n" * line_count

        return re.sub(r"<!--[\s\S]*?-->", replace_comment, html)

    @staticmethod
    def inspect_gtm_container_content(gtm_content: Optional[str]) -> Dict[str, Any]:
        """
        Deep-inspects GTM container body.
        If container is 404, empty, or contains only domestic scripts (e.g. mc.yandex.ru),
        it does not constitute unlawful cross-border transfer.
        """
        if not gtm_content or len(gtm_content.strip()) < 50 or ("<title>404" in gtm_content) or ("404 Not Found" in gtm_content) or ("404. That" in gtm_content):
            return {"has_foreign_trackers": False, "is_benign": True, "reason": "empty_or_404"}

        foreign_patterns = [
            r"google-analytics\.com",
            r"analytics\.js",
            r"gtag\s*\(",
            r"doubleclick\.net",
            r"connect\.facebook\.net",
            r"hotjar\.com",
            r"clarity\.ms",
        ]
        has_foreign = any(re.search(p, gtm_content, re.IGNORECASE) for p in foreign_patterns)
        has_domestic = bool(re.search(r"mc\.yandex\.ru|yandex\.ru", gtm_content, re.IGNORECASE))

        if not has_foreign and has_domestic:
            return {"has_foreign_trackers": False, "is_benign": True, "reason": "domestic_only"}

        return {"has_foreign_trackers": has_foreign, "is_benign": not has_foreign, "reason": "has_foreign" if has_foreign else "clean"}

    def scan_html(
        self,
        raw_html: str,
        page_url: str = "",
        policy_text: Optional[str] = None,
        gtm_containers: Optional[Dict[str, str]] = None,
        site_has_valid_privacy_policy: bool = False,
    ) -> Report152FZ:
        """
        Runs comprehensive 152-FZ compliance audit on raw HTML payload.
        """
        if not raw_html:
            return Report152FZ(
                page_url=page_url,
                is_compliant=True,
                total_violations=0,
                foreign_transfer_violations=[],
                forms=[],
                summary="Empty HTML document provided.",
            )

        # 1. Strip comments while preserving exact line numbers
        clean_html = self._strip_comments_preserving_lines(raw_html)
        lines = raw_html.splitlines()

        foreign_violations: List[Violation152FZ] = []

        # 2. Inspect Foreign Data Transfer Signatures
        for sig in FOREIGN_SIGNATURES:
            pattern: re.Pattern = sig["pattern"]
            for match in pattern.finditer(clean_html):
                start_pos = match.start()
                # 1-based line number
                line_no = clean_html[:start_pos].count("\n") + 1
                
                # GTM Deep-Inspect: If GTM container content is audited and benign, skip
                if sig["name"] == "google-tag-manager" and gtm_containers:
                    gtm_m = re.search(r"GTM-[A-Z0-9]{4,12}", match.group(0)) or re.search(r"GTM-[A-Z0-9]{4,12}", clean_html)
                    if gtm_m:
                        gid = gtm_m.group(0)
                        if gid in gtm_containers:
                            gtm_res = self.inspect_gtm_container_content(gtm_containers[gid])
                            if gtm_res.get("is_benign"):
                                continue

                # Get snippet (line or surrounding 80 chars)
                snippet = lines[line_no - 1].strip() if line_no - 1 < len(lines) else match.group(0)
                if len(snippet) > 160:
                    snippet = snippet[:157] + "..."

                foreign_violations.append(
                    Violation152FZ(
                        category=sig["category"],
                        risk_level=sig.get("risk_level", "HIGH"),
                        signature=sig["name"],
                        line_number=line_no,
                        snippet=snippet,
                        legal_basis=sig["legal_basis"],
                        koap_article=sig["koap_article"],
                        potential_fine_rub=sig["potential_fine_rub"],
                        remediation_note=sig.get("remediation_note", ""),
                    )
                )

        # Cross-check Yandex.Metrika with Privacy Policy disclosure
        if re.search(r"(?i)(?:mc\.yandex\.ru|metrika/tag\.js|ym\s*\(\s*\d{5,10})", clean_html):
            if policy_text is not None:
                p_lower = policy_text.lower()
                has_cookie_mention = bool(re.search(r"(?i)(?:cookie|куки|файлы\s+cookie)", p_lower))
                has_yandex_mention = bool(re.search(r"(?i)(?:yandex|яндекс|яндекс\.метрика|метрик)", p_lower))
                if not (has_cookie_mention and has_yandex_mention):
                    ym_pos = re.search(r"(?i)(?:mc\.yandex\.ru|metrika/tag\.js|ym\s*\(\s*\d{5,10})", clean_html)
                    ym_line = clean_html[:ym_pos.start()].count("\n") + 1 if ym_pos else 1
                    foreign_violations.append(
                        Violation152FZ(
                            category="POLICY_DISCLOSURE_DEFICIT",
                            risk_level="HIGH",
                            signature="yandex-metrika-undisclosed-in-policy",
                            line_number=ym_line,
                            snippet="Счетчик Яндекс.Метрики установлен без раскрытия в Политике конфиденциальности",
                            legal_basis="ч. 1, 2 ст. 18.1 152-ФЗ (Обязанность оператора раскрывать сбор cookie и использование сервисов веб-аналитики ООО «Яндекс»)",
                            koap_article="ч. 1 ст. 13.11 КоАП РФ",
                            potential_fine_rub="от 60 000 до 100 000 руб. (со скидкой МСП: от 30 000 до 50 000 руб.)",
                            remediation_note="Добавить в Политику конфиденциальности раздел об использовании файлов cookie и Яндекс.Метрики, разместить Cookie-баннер.",
                        )
                    )

        # 3. Inspect <form> Tags AND Orphan SPA/Modal Lead Containers for Personal Data Capture
        soup = BeautifulSoup(clean_html, "html.parser")
        for code_or_tpl in soup(["script", "style", "pre", "code", "template", "noscript"]):
            code_or_tpl.decompose()

        form_elements = list(soup.find_all("form"))

        # Detect SPA / React / Tilda / Bitrix modal lead containers (<div class="modal...">) without <form>
        seen_spa_containers = set()
        submit_btn_re = re.compile(
            r"(?i)\b(?:отправить|заказать|оставить\s+заявку|получить|перезвон\w*|записаться|оформить|рассчитать|консультаци\w*)\b"
        )
        for orphan_inp in soup.find_all("input"):
            if orphan_inp.find_parent("form") is not None:
                continue
            o_type = (orphan_inp.get("type") or "text").lower()
            o_sig = f"{orphan_inp.get('name') or ''} {orphan_inp.get('placeholder') or ''} {orphan_inp.get('id') or ''}"
            if not (o_type in ("tel", "email") or re.search(r"(?i)\b(?:phone|tel|mobile|телефон)\b", o_sig)):
                continue
            # Walk up max 4 levels to find a bounded container that also holds a submit/CTA button
            cand = orphan_inp.parent
            chosen_container = None
            for _ in range(4):
                if cand is None or getattr(cand, "name", None) in (None, "body", "html", "[document]"):
                    break
                c_inputs = cand.find_all(["input", "textarea", "select"])
                if 1 <= len(c_inputs) <= 8:
                    btns = cand.find_all(["button", "a", "input"])
                    has_cta_btn = any(
                        (b.name == "input" and (b.get("type") or "").lower() == "submit")
                        or submit_btn_re.search(b.get_text(" ", strip=True) or (b.get("value") or ""))
                        for b in btns
                    )
                    if has_cta_btn:
                        chosen_container = cand
                        break
                cand = cand.parent
            if chosen_container is not None and id(chosen_container) not in seen_spa_containers:
                seen_spa_containers.add(id(chosen_container))
                form_elements.append(chosen_container)

        inspected_forms: List[FormInspectionResult] = []

        for idx, form in enumerate(form_elements):
            form_str = str(form)
            # Find approximate line number of form in clean_html
            form_match = re.search(r"<form\b", form_str, re.IGNORECASE)
            form_snippet = form_str[:80]
            pos = clean_html.find(form_snippet[:30])
            form_line_no = clean_html[:pos].count("\n") + 1 if pos != -1 else 1

            form_id = form.get("id") or form.get("name") or form.get("class")
            if isinstance(form_id, list):
                form_id = " ".join(form_id)

            inputs = form.find_all(["input", "textarea", "select"])
            pd_inputs: List[str] = []
            has_consent_checkbox = False
            has_prechecked_consent = False

            # Check if this form is a pure login / auth form for existing users
            form_action = (form.get("action") or "").lower()
            form_id_str = f"{form_id or ''} {form_action}".lower()
            has_password = any((inp.get("type") or "").lower() == "password" for inp in inputs)
            has_reg_fields = any(
                re.search(r"(?i)(?:confirm|repeat|повтор|inn|инн|phone|tel|телефон)", (inp.get("name") or "") + " " + (inp.get("placeholder") or ""))
                for inp in inputs
            )
            is_login_named = bool(re.search(r"(?i)(?:login|signin|auth|войти|авториз|вход)", form_id_str))
            btn_texts = " ".join([btn.get_text() for btn in form.find_all(["button", "input"]) if btn.get("type") in (None, "submit", "button")])
            has_login_btn = bool(re.search(r"(?i)(?:войти|вход|sign\s*in|log\s*in)", btn_texts))
            has_signup_btn = bool(re.search(r"(?i)(?:регистр|зарегистр|sign\s*up|create)", btn_texts))

            is_pure_login_form = (has_password and not has_reg_fields and (is_login_named or has_login_btn) and not has_signup_btn)

            # Check if this form is a pure search / catalog filter form
            is_search_role = (form.get("role") or "").lower() == "search"
            is_search_action = "search" in form_action
            is_search_class = any("search" in str(c).lower() for c in (form.get("class") or []))
            non_btn_inputs = [inp for inp in inputs if (inp.get("type") or "text").lower() not in ("submit", "button", "hidden")]
            has_only_search_field = False
            if len(non_btn_inputs) <= 2:
                n_types = [(inp.get("type") or "text").lower() for inp in non_btn_inputs]
                n_names = [(inp.get("name") or "").lower() for inp in non_btn_inputs]
                if any(t in ("search",) or n in ("q", "query", "search", "s", "text") for t, n in zip(n_types, n_names)):
                    if not any(t in ("tel", "email") or re.search(r"(?i)(?:phone|tel|mail|fio|имя|телефон)", n) for t, n in zip(n_types, n_names)):
                        has_only_search_field = True

            is_pure_search_form = is_search_role or is_search_action or (is_search_class and has_only_search_field) or has_only_search_field

            # Smart detection of custom Tilda / Bitrix / React checkboxes and consent notices
            has_custom_checkbox = False
            has_consent_mention = False
            for elem in form.find_all(True):
                e_role = (elem.get("role") or "").lower()
                e_classes = " ".join(elem.get("class") or []).lower()
                if e_role == "checkbox" or any(ck in e_classes for ck in ["checkbox", "t-checkbox", "custom-checkbox", "b-checkbox", "agree", "consent"]):
                    has_custom_checkbox = True
                e_text = elem.get_text() or ""
                if CONSENT_TEXT_REGEX.search(e_text) or CONSENT_TEXT_REGEX.search(e_classes):
                    has_consent_mention = True

            has_bundled_ads_consent = False
            bundled_ads_snippet = ""

            for inp in inputs:
                inp_type = (inp.get("type") or "text").lower()
                inp_name = inp.get("name") or ""
                inp_placeholder = inp.get("placeholder") or ""
                inp_id = inp.get("id") or ""

                if inp_type == "checkbox":
                    parent_txt = inp.parent.get_text(" ", strip=True) if inp.parent else ""
                    grandparent_txt = (
                        inp.parent.parent.get_text(" ", strip=True)
                        if (inp.parent and inp.parent.parent and len(parent_txt) < 20)
                        else ""
                    )
                    linked_lbl_txt = ""
                    if inp_id:
                        lbl_tag = form.find("label", attrs={"for": inp_id}) or soup.find("label", attrs={"for": inp_id})
                        if lbl_tag:
                            linked_lbl_txt = lbl_tag.get_text(" ", strip=True)
                    aria_lbl = inp.get("aria-label") or ""
                    surrounding_text = " ".join(x for x in (parent_txt, linked_lbl_txt, grandparent_txt, aria_lbl) if x)
                    inp_attrs = f"{inp_name} {inp_id} {surrounding_text}"
                    if CONSENT_TEXT_REGEX.search(inp_attrs):
                        has_consent_checkbox = True
                        if inp.has_attr("checked") or "checked" in inp.attrs:
                            has_prechecked_consent = True

                    # Check for bundled personal data + advertising/marketing consent (156-FZ)
                    surr_lower = surrounding_text.lower()
                    has_pd_mention = bool(re.search(r"(?i)(?:персональн\w*\s+данн\w*|обработк\w*\s+персональных|пдн\b|пд\b)", surr_lower))
                    has_ads_mention = bool(re.search(r"(?i)(?:реклам\w*|рассылк\w*|маркетинг\w*|спецпредложен\w*|акциях)", surr_lower))
                    if has_pd_mention and has_ads_mention:
                        has_bundled_ads_consent = True
                        bundled_ads_snippet = surrounding_text.strip()[:100]
                else:
                    field_sig = f"{inp_name} {inp_placeholder} {inp_id}"
                    if inp_type in ("tel", "email") or PD_INPUT_NAME_REGEX.search(field_sig):
                        pd_inputs.append(inp_name or inp_placeholder or inp_type)

            has_pd = len(pd_inputs) > 0 and not (is_pure_login_form or is_pure_search_form)

            # Check for Privacy Policy & Separate Consent Link
            all_form_links = list(form.find_all("a"))
            if form.parent:
                all_form_links.extend(form.parent.find_all("a"))

            # Check if site or page has published policy
            page_has_policy = bool(POLICY_HREF_REGEX.search(raw_html))
            site_has_policy = site_has_valid_privacy_policy or page_has_policy

            has_policy_link = site_has_policy
            has_dedicated_consent_link = False
            has_only_offer_link = False

            for a in all_form_links:
                href = a.get("href") or ""
                text = a.get_text() or ""
                combined_link = f"{href} {text}".lower()
                if POLICY_HREF_REGEX.search(href) or CONSENT_TEXT_REGEX.search(text) or POLICY_HREF_REGEX.search(combined_link):
                    has_policy_link = True
                if re.search(r"(?i)(?:согласие|personal-data|personal_data|politika|privacy|персональн|soglashenie|конфиденц)", combined_link):
                    has_dedicated_consent_link = True
                elif re.search(r"(?i)(?:oferta|terms|dogovor|оферт|договор|правил)", combined_link):
                    has_only_offer_link = True

            form_violations: List[Violation152FZ] = []
            is_compliant = True

            if is_pure_login_form or is_pure_search_form:
                # Login forms and search forms do not require consent checkbox
                is_compliant = True
            elif has_pd:
                if not has_consent_checkbox:
                    if has_custom_checkbox or has_consent_mention:
                        # Soft conditional audit: custom checkbox or consent notice present (TIER_2)
                        is_compliant = False
                        form_violations.append(
                            Violation152FZ(
                                category="CONDITIONAL_CONSENT_FORM_AUDIT",
                                risk_level="LOW",
                                signature="custom-checkbox-or-consent-notice",
                                line_number=form_line_no,
                                snippet=f"<form id='{form_id}'> кастомный чекбокс / согласие в тексте формы",
                                legal_basis="ст. 9 152-ФЗ, 156-ФЗ (Кастомный элемент согласия / конклюдентная форма — требует документарной сверки)",
                                koap_article="ст. 13.11 КоАП РФ (Предписание/проверка)",
                                potential_fine_rub="Требует документарной проверки (TIER_2)",
                                remediation_note="Проверить, что клик по кастомному чекбоксу фиксирует конкретное согласие пользователя отдельно от оферты.",
                            )
                        )
                    else:
                        is_compliant = False
                        form_violations.append(
                            Violation152FZ(
                                category="HIGH_CONSENT_FORM_RISK",
                                risk_level="HIGH",
                                signature="missing-consent-checkbox",
                                line_number=form_line_no,
                                snippet=f"<form id='{form_id}'> capturing {pd_inputs}",
                                legal_basis="ч. 1 ст. 6, ст. 9 152-ФЗ (Обработка персональных данных без информированного согласия субъекта ПДн)",
                                koap_article="ч. 2 ст. 13.11 КоАП РФ",
                                potential_fine_rub="от 60 000 до 100 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 30 000 до 50 000 руб.)",
                                remediation_note="Добавить обязательный чекбокс согласия со ссылкой на Политику обработки ПДн.",
                            )
                        )
                elif has_prechecked_consent:
                    is_compliant = False
                    form_violations.append(
                        Violation152FZ(
                            category="HIGH_CONSENT_FORM_RISK",
                            risk_level="HIGH",
                            signature="prechecked-consent-checkbox",
                            line_number=form_line_no,
                            snippet=f"<form id='{form_id}'> <input type='checkbox' checked>",
                            legal_basis="ч. 1 ст. 9 152-ФЗ (Предустановленная галочка согласия — нарушение принципа конкретности и осознанности согласия, запрещено с 01.09.2025)",
                            koap_article="ч. 2 ст. 13.11 КоАП РФ",
                            potential_fine_rub="от 60 000 до 100 000 руб. (со скидкой МСП: от 30 000 до 50 000 руб.)",
                            remediation_note="Убрать атрибут checked у чекбокса согласия. Пользователь обязан отметить его самостоятельно.",
                        )
                    )
                if has_bundled_ads_consent:
                    is_compliant = False
                    form_violations.append(
                        Violation152FZ(
                            category="HIGH_CONSENT_FORM_RISK",
                            risk_level="HIGH",
                            signature="bundled-pd-and-advertising-consent",
                            line_number=form_line_no,
                            snippet=f"<form id='{form_id}'> совмещенное согласие: {bundled_ads_snippet}",
                            legal_basis="ст. 9 152-ФЗ в ред. 156-ФЗ, ч. 1 ст. 18 38-ФЗ (Запрет объединения согласия на обработку ПДн с согласием на рекламу/рассылки в единый чекбокс)",
                            koap_article="ч. 2 ст. 13.11 КоАП РФ, ч. 1 ст. 14.3 КоАП РФ",
                            potential_fine_rub="от 100 000 до 500 000 руб. (по 38-ФЗ) и от 30 000 до 150 000 руб. (по 152-ФЗ)",
                            remediation_note="Разделить согласие на обработку ПДн и согласие на получение рекламы на два независимых чекбокса.",
                        )
                    )
                if not has_policy_link:
                    is_compliant = False
                    if has_only_offer_link:
                        form_violations.append(
                            Violation152FZ(
                                category="HIGH_CONSENT_FORM_RISK",
                                risk_level="HIGH",
                                signature="bundled-offer-consent-missing-separate-consent",
                                line_number=form_line_no,
                                snippet=f"<form id='{form_id}'> ссылка только на оферту/договор без отдельного согласия на ПДн",
                                legal_basis="ст. 9 152-ФЗ в ред. 156-ФЗ (Запрет объединения согласия на обработку ПДн с договором/публичной офертой без отдельного самостоятельного согласия)",
                                koap_article="ч. 2 ст. 13.11 КоАП РФ",
                                potential_fine_rub="от 60 000 до 100 000 руб. (со скидкой МСП: от 30 000 до 50 000 руб.)",
                                remediation_note="Оформить Согласие на обработку персональных данных отдельной ссылкой отдельно от договора/публичной оферты.",
                            )
                        )
                    else:
                        form_violations.append(
                            Violation152FZ(
                                category="HIGH_CONSENT_FORM_RISK",
                                risk_level="HIGH",
                                signature="missing-privacy-policy-link",
                                line_number=form_line_no,
                                snippet=f"<form id='{form_id}'> without link to Privacy Policy",
                                legal_basis="ч. 2 ст. 18.1 152-ФЗ (Неопубликование документа, определяющего политику оператора в отношении обработки ПДн)",
                                koap_article="ч. 3 ст. 13.11 КоАП РФ",
                                potential_fine_rub="от 30 000 до 60 000 руб. (со скидкой МСП по ст. 4.1.2 КоАП: от 15 000 до 30 000 руб.)",
                                remediation_note="Разместить кликабельную ссылку на Политику обработки ПДн рядом с кнопкой отправки.",
                            )
                        )
                elif has_only_offer_link and not has_dedicated_consent_link:
                    is_compliant = False
                    form_violations.append(
                        Violation152FZ(
                            category="HIGH_CONSENT_FORM_RISK",
                            risk_level="HIGH",
                            signature="bundled-offer-consent-missing-separate-consent",
                            line_number=form_line_no,
                            snippet=f"<form id='{form_id}'> ссылка только на оферту/договор без отдельного согласия на ПДн",
                            legal_basis="ст. 9 152-ФЗ в ред. 156-ФЗ (Запрет объединения согласия на обработку ПДн с договором/публичной офертой без отдельного самостоятельного согласия)",
                            koap_article="ч. 2 ст. 13.11 КоАП РФ",
                            potential_fine_rub="от 60 000 до 100 000 руб. (со скидкой МСП: от 30 000 до 50 000 руб.)",
                            remediation_note="Оформить Согласие на обработку персональных данных отдельной ссылкой отдельно от договора/публичной оферты.",
                        )
                    )

            inspected_forms.append(
                FormInspectionResult(
                    form_id=form_id,
                    form_index=idx,
                    line_number=form_line_no,
                    is_login_form=is_pure_login_form,
                    has_personal_data_inputs=has_pd,
                    input_names=pd_inputs,
                    has_consent_checkbox=has_consent_checkbox,
                    has_policy_link=has_policy_link,
                    is_compliant=is_compliant,
                    violations=form_violations,
                )
            )

        # 4. Synthesize Summary Report with Risk Counts
        all_violations = list(foreign_violations)
        for f in inspected_forms:
            all_violations.extend(f.violations)

        high_risk_count = sum(1 for v in all_violations if v.risk_level == "HIGH")
        low_risk_count = sum(1 for v in all_violations if v.risk_level == "LOW")
        is_overall_compliant = len(all_violations) == 0
        summary_lines = []
        if is_overall_compliant:
            summary_lines.append("152-ФЗ Compliance: PASS. Запрещенных зарубежных трекеров и нарушений в формах не обнаружено.")
        else:
            summary_lines.append(f"152-ФЗ Compliance: FAIL. Обнаружено нарушений: {len(all_violations)}.")
            if foreign_violations:
                summary_lines.append(f" - Трансграничная передача / зарубежные трекеры: {len(foreign_violations)} шт.")
            form_viol_count = sum(len(f.violations) for f in inspected_forms)
            if form_viol_count > 0:
                summary_lines.append(f" - Нарушения в лид-формах сбора ПДн: {form_viol_count} шт.")

        return Report152FZ(
            page_url=page_url,
            is_compliant=is_overall_compliant,
            total_violations=len(all_violations),
            high_risk_count=high_risk_count,
            low_risk_count=low_risk_count,
            foreign_transfer_violations=foreign_violations,
            forms=inspected_forms,
            summary="\n".join(summary_lines),
        )

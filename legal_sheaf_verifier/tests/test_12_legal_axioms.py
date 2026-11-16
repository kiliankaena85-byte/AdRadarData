"""
Regression Test Suite for the 12 Immutable Legal Axioms of Russian Ad & PD Compliance.
Grounded in:
- 38-FZ (Advertising Law), 152-FZ (Personal Data), 168-FZ (State Language), 248-FZ (State Control)
- FAS Russia Explanations (Letters No. AT/61279/26, Order No. 821/23)
- Mintsifry Orders No. 511 and 786, RF Government Decrees No. 736, 1087, 1286
- Administrative Offenses Code (KoAP RF: Art. 4.1.1, 4.1.2, 4.4(5), 13.11, 13.55, 14.3)
"""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Ensure package src directory is on sys.path
pkg_src = Path(__file__).resolve().parent.parent / "src"
pkg_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pkg_src))
sys.path.insert(0, str(pkg_dir))

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import MODELS_CACHE_DIR, ARTIFACTS_DIR
from legal_sheaf.inn_erid_validators import detect_untranslated_foreign_words_168fz
from legal_sheaf.it_accreditation import ITAccreditationAuditor
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput, LegalRegime


@pytest.fixture(scope="module")
def champion_bundle():
    """Loads champion_v2.bundle from models cache or artifacts."""
    candidates = [
        MODELS_CACHE_DIR / "champion_v2.bundle",
        ARTIFACTS_DIR / "champion_v2.bundle",
        pkg_dir / "artifacts" / "champion_v2.bundle",
    ]
    for c in candidates:
        if c.exists():
            return AdRadarBrainBundle.load(c)
    pytest.fail(f"champion_v2.bundle not found in any candidate path: {candidates}")


# ---------------------------------------------------------------------------
# AXIOM 1: Own website != Ad (OWN_WEBSITE, 38-FZ Art. 2(2), Decree 1087, FAS Letter)
# ---------------------------------------------------------------------------
def test_axiom_1_own_website_not_ad(champion_bundle):
    doc = DocumentInput(
        clean_text=(
            "Интернет-магазин мебели «Уютный Дом». Каталог нашей продукции собственного производства: "
            "диваны от 35 000 руб., обеденные столы из дуба от 22 000 руб., шкафы-купе от 45 000 руб. "
            "Выберите модель и нажмите кнопку «Купить» для добавления товара в корзину."
        ),
        author_domain="uyutny-dom.ru",
        page_url="https://uyutny-dom.ru/catalog",
        platform_ad_meta={"context_mode": "OWN_WEBSITE"},
    )
    report = champion_bundle.verify_document(doc)
    assert not report.is_violation, "Own catalog with prices and buy button must not be flagged as ad violation"
    assert report.regime == LegalRegime.CLEAN_NON_AD, "Regime must be CLEAN_NON_AD"


# ---------------------------------------------------------------------------
# AXIOM 2: Medical clinics on own site: mandatory informing exempt from 5% disclaimer
# ---------------------------------------------------------------------------
def test_axiom_2_clinic_own_site_informing_exempt(champion_bundle):
    doc = DocumentInput(
        clean_text=(
            "Медицинский центр «Здоровье». Перечень врачей-специалистов, стаж работы, квалификационные категории и график консультаций. "
            "Прайс-лист на первичный и повторный прием специалистов: терапевт 2500 руб., кардиолог 3200 руб., невролог 3000 руб. "
            "Информация размещена в порядке обязательного информирования потребителей медицинских услуг по ст. 79 ФЗ № 323-ФЗ и ПП РФ № 736."
        ),
        author_domain="clinic-zdorovie.ru",
        page_url="https://clinic-zdorovie.ru/doctors",
        platform_ad_meta={"context_mode": "OWN_WEBSITE"},
    )
    report = champion_bundle.verify_document(doc)
    assert not report.is_violation, "Medical clinic informing on own website is strictly non-ad"
    assert report.regime == LegalRegime.CLEAN_NON_AD


# ---------------------------------------------------------------------------
# AXIOM 3: Vacancies / HR / Career and uniform partner/dealer lists are NOT ads
# ---------------------------------------------------------------------------
def test_axiom_3_hr_vacancies_and_partners_not_ad(champion_bundle):
    # HR Vacancy
    doc_hr = DocumentInput(
        clean_text=(
            "Карьера в компании «ДатаСистемс». Открыта вакансия Senior Backend Developer. "
            "Заработная плата от 350 000 руб. на руки, официальное оформление, ДМС со стоматологией, оплата фитнеса. "
            "Стек: Python, FastAPI, PostgreSQL, Docker, Kubernetes. Нажмите кнопку «Откликнуться» для отправки резюме."
        ),
        author_domain="datasystems.ru",
        page_url="https://datasystems.ru/careers",
        platform_ad_meta={"context_mode": "OWN_WEBSITE"},
    )
    report_hr = champion_bundle.verify_document(doc_hr)
    assert not report_hr.is_violation
    assert report_hr.regime == LegalRegime.CLEAN_NON_AD

    # Uniform Dealer List
    doc_dealers = DocumentInput(
        clean_text=(
            "Где купить нашу продукцию? Единообразный перечень официальных региональных дилеров и партнеров: "
            "1. ООО «СеверАвто» — г. Санкт-Петербург, Московский пр-т, 12; "
            "2. ТД «УралЗапчасть» — г. Екатеринбург, ул. Малышева, 45; "
            "3. Компания «СибирьТрейд» — г. Новосибирск, Красный пр-т, 80. "
            "Все авторизованные партнеры работают по единым рекомендованным розничным ценам производителя."
        ),
        author_domain="autocomponent.ru",
        page_url="https://autocomponent.ru/where-to-buy",
        platform_ad_meta={"context_mode": "OWN_WEBSITE"},
    )
    report_dealers = champion_bundle.verify_document(doc_dealers)
    assert not report_dealers.is_violation
    assert report_dealers.regime == LegalRegime.CLEAN_NON_AD


# ---------------------------------------------------------------------------
# AXIOM 4: Real 38-FZ & 168-FZ infractions on own site (third-party ads without erid)
# ---------------------------------------------------------------------------
def test_axiom_4_real_violations_detected_on_own_site(champion_bundle):
    # Third-party promo banner on own site without erid
    doc_ad = DocumentInput(
        clean_text=(
            "Реклама стороннего сервиса: Оформите кредитную карту в Банке 'ФинПартнер' со ставкой от 9.9% годовых! "
            "Кешбэк рублями, кредитный лимит до 1 000 000 руб. Переходите по ссылке и заказывайте: https://finpartner-bank.ru/apply"
        ),
        author_domain="my-portal.ru",
        page_url="https://my-portal.ru/partners",
        platform_ad_meta={},
    )
    report_ad = champion_bundle.verify_document(doc_ad)
    assert report_ad.is_violation, "Unmarked third-party commercial promotion must be flagged as violation"
    assert report_ad.regime == LegalRegime.UNMARKED_AD_NO_ERID


# ---------------------------------------------------------------------------
# AXIOM 5: 248-FZ Procedural Barrier (10-day demand vs unscheduled check risk)
# ---------------------------------------------------------------------------
def test_axiom_5_rkn_248fz_risk_indicator_barrier():
    calc = KoAPSanctionsCalculator()
    
    # < 3 risk indicators: PRECAUTION_OR_DEMAND_10_DAYS
    low_risk = calc.calculate_penalties(["missing-consent-checkbox", "missing-privacy-policy-link"])
    assert low_risk["regulatory_outcome"] == "PRECAUTION_OR_DEMAND_10_DAYS"
    assert "10 дней" in low_risk["outcome_description"]
    
    # >= 3 risk indicators: UNSCHEDULED_INSPECTION_RISK
    high_risk = calc.calculate_penalties([
        "missing-consent-checkbox",
        "missing-privacy-policy-link",
        "foreign-auth-google-apple",
    ])
    assert high_risk["regulatory_outcome"] == "UNSCHEDULED_INSPECTION_RISK"
    assert "внеплановой проверки" in high_risk["outcome_description"]


# ---------------------------------------------------------------------------
# AXIOM 6: GTM Deep-Inspect: inspect container body before flagging cross-border
# ---------------------------------------------------------------------------
def test_axiom_6_gtm_deep_inspect_benign_container():
    scanner = Compliance152FZScanner()
    
    # Case A: Domestic-only GTM container (mc.yandex.ru only)
    html_gtm = '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-DOMESTIC"></script>'
    containers_clean = {"GTM-DOMESTIC": "/* local code */ var ym = 'https://mc.yandex.ru/metrika/tag.js';"}
    rep_clean = scanner.scan_html(html_gtm, gtm_containers=containers_clean)
    assert len(rep_clean.foreign_transfer_violations) == 0, "Benign domestic GTM container must not be flagged"

    # Case B: 404 / empty container
    rep_404 = scanner.scan_html(html_gtm, gtm_containers={"GTM-DOMESTIC": "404 Not Found"})
    assert len(rep_404.foreign_transfer_violations) == 0, "Empty/404 container must not be flagged"

    # Case C: Active foreign tracking inside GTM
    containers_evil = {"GTM-DOMESTIC": "gtag('config', 'UA-123456'); connect.facebook.net/fbevents.js;"}
    rep_evil = scanner.scan_html(html_gtm, gtm_containers=containers_evil)
    assert any(v.signature == "google-tag-manager" for v in rep_evil.foreign_transfer_violations)


# ---------------------------------------------------------------------------
# AXIOM 7: Yandex.Metrika Policy Disclosure Cross-Check
# ---------------------------------------------------------------------------
def test_axiom_7_yandex_metrika_policy_disclosure_cross_check():
    scanner = Compliance152FZScanner()
    html_ym = '<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
    
    # Policy misses Yandex and cookies -> Violation of Art. 18.1 152-FZ
    policy_undisclosed = "Наша организация обрабатывает только имена и телефоны клиентов для обратной связи."
    rep_fail = scanner.scan_html(html_ym, policy_text=policy_undisclosed)
    assert any(v.signature == "yandex-metrika-undisclosed-in-policy" for v in rep_fail.foreign_transfer_violations)
    
    # Policy properly discloses Yandex and cookies -> Compliant
    policy_disclosed = "На сайте используются файлы cookie и аналитический сервис веб-аналитики Яндекс.Метрика."
    rep_pass = scanner.scan_html(html_ym, policy_text=policy_disclosed)
    assert not any(v.signature == "yandex-metrika-undisclosed-in-policy" for v in rep_pass.foreign_transfer_violations)


# ---------------------------------------------------------------------------
# AXIOM 8: Forms compliance (search exempt, lead forms require consent, bundled consent banned)
# ---------------------------------------------------------------------------
def test_axiom_8_forms_data_collection_consent_compliance():
    scanner = Compliance152FZScanner()

    # Search form does NOT need consent
    search_html = '<form role="search"><input type="search" name="q" placeholder="Поиск..."><button type="submit">Найти</button></form>'
    rep_search = scanner.scan_html(search_html)
    assert len(rep_search.forms) == 1
    assert rep_search.forms[0].is_compliant, "Pure search form is fully compliant without consent checkbox"

    # Lead capture form without checkbox is non-compliant
    lead_html = '<form><input type="text" name="name"><input type="tel" name="phone"><button type="submit">Отправить</button></form>'
    rep_lead = scanner.scan_html(lead_html)
    assert not rep_lead.forms[0].is_compliant
    assert any(v.signature == "missing-consent-checkbox" for v in rep_lead.forms[0].violations)

    # Bundled PD + advertising consent in single checkbox is non-compliant (156-FZ)
    bundled_html = '''
    <form>
        <input type="text" name="name">
        <input type="tel" name="phone">
        <label><input type="checkbox"> Согласен на обработку персональных данных и получение рекламных рассылок</label>
        <a href="/privacy">Политика конфиденциальности</a>
    </form>
    '''
    rep_bundled = scanner.scan_html(bundled_html)
    assert any(v.signature == "bundled-pd-and-advertising-consent" for v in rep_bundled.forms[0].violations)


# ---------------------------------------------------------------------------
# AXIOM 9: Russian Language Law (168-FZ): B2B immune, RAS dictionary and tech whitelists
# ---------------------------------------------------------------------------
def test_axiom_9_russian_language_168fz_b2b_and_ran_whitelists(champion_bundle):
    # Text containing RAS dictionary loanwords and technical standards
    text = (
        "Завод промышленного оборудования. Технические стандарты производства: ISO 9001, интерфейс API, "
        "гарантия по договору SLA, OEM-производство. Программа лояльности: кешбэк до 5%, персональный менеджер, "
        "онлайн-сервис и доставка в наш шоурум."
    )
    res_detector = detect_untranslated_foreign_words_168fz(text)
    assert not res_detector["has_violation_signals"], "RAS dictionary words and tech standards must not trigger 168-FZ"

    doc_b2b = DocumentInput(
        clean_text=text,
        author_domain="prom-plant.ru",
        page_url="https://prom-plant.ru/standards",
        platform_ad_meta={"context_mode": "OWN_WEBSITE"},
    )
    report = champion_bundle.verify_document(doc_b2b)
    assert not report.is_violation
    assert report.regime == LegalRegime.CLEAN_NON_AD


# ---------------------------------------------------------------------------
# AXIOM 10: Foreign auth (199-FZ / Art. 13.55 KoAP): monetary fine moratorium until 2029
# ---------------------------------------------------------------------------
def test_axiom_10_foreign_auth_moratorium_until_2029():
    scanner = Compliance152FZScanner()
    tg_html = '<script src="https://telegram.org/js/telegram-widget.js" data-telegram-login="mybot" data-onauth="onTelegramAuth()"></script>'
    rep = scanner.scan_html(tg_html)
    assert any(v.signature == "telegram-auth-widget" for v in rep.foreign_transfer_violations)

    calc = KoAPSanctionsCalculator()
    res = calc.calculate_penalties(["telegram-auth-widget"])
    assert len(res["grouped_penalties"]) == 1
    pen = res["grouped_penalties"][0]
    assert pen["article"] == "13.55_part_2"
    assert pen["status"] == "MORATORIUM_WARNING"
    assert pen["min_rubles"] == 0 and pen["max_rubles"] == 0, "Fine must be 0 rubles under 2029 moratorium"
    assert "2029" in pen["note"]


# ---------------------------------------------------------------------------
# AXIOM 11: Prohibition of fine multiplication (Part 5 Art. 4.4 KoAP) & SME Relief
# ---------------------------------------------------------------------------
def test_axiom_11_koap_fine_non_multiplication_and_sme_relief():
    calc = KoAPSanctionsCalculator()
    
    # 5 identical violations on one website
    five_forms = ["missing-consent-checkbox"] * 5
    
    # First offense for SME -> Article 4.1.1 warning (0 RUB fine)
    res_first = calc.calculate_penalties(five_forms, is_sme=True, first_offense=True)
    assert res_first["unique_articles_count"] == 1, "Must group into exactly 1 article, not 5"
    assert res_first["total_min_rubles"] == 0
    assert res_first["grouped_penalties"][0]["status"] == "SME_WARNING_SUBSTITUTED"
    
    # Repeat offense for SME -> Article 4.1.2 50% discount
    res_repeat = calc.calculate_penalties(five_forms, is_sme=True, first_offense=False)
    assert res_repeat["unique_articles_count"] == 1
    assert res_repeat["grouped_penalties"][0]["status"] == "SME_DISCOUNT_APPLIED"
    # Nominal is 30,000 - 150,000; with 50% SME discount it is 15,000 - 75,000
    assert res_repeat["total_min_rubles"] == 15000
    assert res_repeat["total_max_rubles"] == 75000


# ---------------------------------------------------------------------------
# AXIOM 12: IT companies (Mintsifry Order No. 511 mandatory disclosure)
# ---------------------------------------------------------------------------
def test_axiom_12_it_accreditation_disclosure():
    auditor = ITAccreditationAuditor()

    # Compliant IT website
    compliant_html = """
    <html>
        <body>
            <h1>ИТ-компания «НейроСеть» (ИНН 7707083893)</h1>
            <p>Основной вид деятельности: разработка программного обеспечения (ОКВЭД 62.01) и облачные сервисы (ОКВЭД 63.11).</p>
            <p>Наш технологический стек: Python, FastAPI, Docker, Kubernetes, PostgreSQL.</p>
        </body>
    </html>
    """
    res_ok = auditor.audit_it_company_website(compliant_html, expected_inn="7707083893")
    assert res_ok["is_compliant"]
    assert res_ok["inn_disclosed"]
    assert res_ok["activities_disclosed"]
    assert res_ok["tech_stack_disclosed"]
    assert len(res_ok["missing_requirements"]) == 0

    # Non-compliant IT website (missing INN and tech stack)
    non_compliant_html = """
    <html>
        <body>
            <h1>ИТ-сервис</h1>
            <p>Мы делаем сайты на заказ. Звоните нам прямо сейчас!</p>
        </body>
    </html>
    """
    res_fail = auditor.audit_it_company_website(non_compliant_html, expected_inn="7707083893")
    assert not res_fail["is_compliant"]
    assert not res_fail["inn_disclosed"]
    assert len(res_fail["missing_requirements"]) >= 2

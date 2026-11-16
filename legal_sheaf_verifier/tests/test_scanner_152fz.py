"""
Pytest suite for Compliance152FZScanner.
Tests detection of foreign trackers, comment stripping invariance, and form consent validation.
"""

import pytest
from legal_sheaf.scanner_152fz import Compliance152FZScanner


@pytest.fixture
def scanner():
    return Compliance152FZScanner()


def test_comment_stripping_does_not_flag_commented_trackers(scanner):
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Тестовый сайт завода</title>
    <!--
    <script src="https://www.googletagmanager.com/gtm.js?id=GTM-TEST1234"></script>
    -->
    <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
    <h1>Официальный сайт производства</h1>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://factory.ru")
    assert report.is_compliant is True
    assert len(report.foreign_transfer_violations) == 0


def test_live_google_trackers_flagged_with_line_number(scanner):
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Сайт</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto" rel="stylesheet">
    <script async src="https://www.googletagmanager.com/gtag/js?id=G-XYZ12345"></script>
    <script>
      window.dataLayer = window.dataLayer || [];
      function gtag(){dataLayer.push(arguments);}
      gtag('config', 'G-XYZ12345');
    </script>
</head>
<body>
    <script src="https://www.google.com/recaptcha/api.js"></script>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://example.ru")
    assert report.is_compliant is False
    assert len(report.foreign_transfer_violations) >= 3

    sigs = [v.signature for v in report.foreign_transfer_violations]
    assert "google-fonts" in sigs
    assert "google-tag-manager" in sigs or "google-analytics" in sigs
    assert "google-recaptcha" in sigs

    # Line number check: google-fonts is at line 5
    font_v = next(v for v in report.foreign_transfer_violations if v.signature == "google-fonts")
    assert font_v.line_number == 5


def test_form_without_consent_and_policy_flags_violations(scanner):
    html = """<!DOCTYPE html>
<html>
<body>
    <div class="contact-section">
        <h2>Оставьте заявку на оптовую поставку</h2>
        <form id="lead_form" action="/submit" method="POST">
            <input type="text" name="client_name" placeholder="Ваше имя">
            <input type="tel" name="phone" placeholder="+7 (999) 000-00-00">
            <button type="submit">Отправить заявку</button>
        </form>
    </div>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://plant.ru")
    assert report.is_compliant is False
    assert len(report.forms) == 1
    form_res = report.forms[0]
    assert form_res.has_personal_data_inputs is True
    assert form_res.has_consent_checkbox is False
    assert form_res.has_policy_link is False
    assert form_res.is_compliant is False
    assert len(form_res.violations) == 2


def test_compliant_form_with_consent_and_policy(scanner):
    html = """<!DOCTYPE html>
<html>
<body>
    <form id="order_form">
        <input type="text" name="fio" placeholder="ФИО">
        <input type="email" name="email" placeholder="Электронная почта">
        <label>
            <input type="checkbox" name="agree" value="1">
            Я даю согласие на обработку персональных данных
        </label>
        <a href="/privacy-policy">Политика конфиденциальности</a>
        <button type="submit">Заказать</button>
    </form>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://compliant.ru")
    assert report.is_compliant is True
    assert len(report.forms) == 1
    form_res = report.forms[0]
    assert form_res.has_personal_data_inputs is True
    assert form_res.has_consent_checkbox is True
    assert form_res.has_policy_link is True
    assert form_res.is_compliant is True
    assert len(form_res.violations) == 0


def test_search_form_without_personal_data_passes(scanner):
    html = """<!DOCTYPE html>
<html>
<body>
    <form id="search" action="/search">
        <input type="text" name="q" placeholder="Поиск по каталогу продукции">
        <button type="submit">Найти</button>
    </form>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://clean-search.ru")
    assert report.is_compliant is True
    assert len(report.forms) == 1
    form_res = report.forms[0]
    assert form_res.has_personal_data_inputs is False
    assert form_res.is_compliant is True
    assert len(form_res.violations) == 0


def test_login_form_exempt_from_consent_checkbox(scanner):
    html = """<!DOCTYPE html>
<html>
<body>
    <form id="auth_login_form" action="/login" method="POST">
        <h2>Вход в личный кабинет</h2>
        <input type="text" name="login" placeholder="Логин или Email">
        <input type="password" name="password" placeholder="Пароль">
        <button type="submit">Войти</button>
    </form>
</body>
</html>"""
    report = scanner.scan_html(html, page_url="https://example.ru/login")
    assert report.is_compliant is True
    assert len(report.forms) == 1
    form_res = report.forms[0]
    assert form_res.is_login_form is True
    assert form_res.is_compliant is True
    assert len(form_res.violations) == 0


def test_generated_report_is_100_percent_152fz_clean(scanner, tmp_path):
    """
    Dogfooding verification: the generated HTML report itself must be 100% compliant with 152-FZ.
    Zero foreign trackers, zero external fonts (Google Fonts/CDN), zero non-compliant forms.
    """
    from pathlib import Path
    from ingestion.factory_crawler import CompanyProfile, SiteAuditResult
    from legal_sheaf.lead_packager import LeadPackager
    from legal_sheaf.schema import LegalRegime, VerificationReport

    packager = LeadPackager(output_root=tmp_path)
    comp = CompanyProfile(
        domain="clean-factory.ru",
        inn="7707083893",
        legal_name="ООО «Чистое Производство»",
        management_name="Иванов Иван Иванович",
        lead_tier="TIER_A_CORP",
        revenue_rub=150_000_000,
        revenue_year="2025",
        corporate_emails=["info@clean-factory.ru"],
    )
    dummy_rep_152 = scanner.scan_html("<html><body>OK</body></html>", page_url="https://clean-factory.ru")
    site_res = SiteAuditResult(
        company=comp,
        pages_inspected=["https://clean-factory.ru"],
        compliance_152fz=dummy_rep_152,
    )
    sheaf_rep = VerificationReport(
        is_violation=False,
        regime=LegalRegime.CLEAN_NON_AD,
        regime_name="CLEAN_NON_AD",
        violation_probability=0.05,
        conformal_verdict="CLEAN",
        top_edge_conflicts=[],
        legal_citations=["п. 3 ч. 2 ст. 2 38-ФЗ"],
        procedural_risks=[],
        regime_probabilities={"CLEAN_NON_AD": 0.95},
        total_sheaf_energy=0.01,
        active_slots=[],
        mitigating_factors=[],
        fast_standalone_mode=True,
    )

    pack_info = packager.package_lead(
        company=comp,
        site_audit=site_res,
        sheaf_report=sheaf_rep,
        post_url="https://vc.ru/12345",
        post_title="Обзор рынка оборудования 2026",
    )

    html_file = Path(pack_info["html_path"])
    assert html_file.exists()
    html_code = html_file.read_text(encoding="utf-8")

    # Scan the generated report itself through Compliance152FZScanner
    dogfood_report = scanner.scan_html(html_code, page_url="https://adradar.ru/r/test")

    assert dogfood_report.is_compliant is True
    assert dogfood_report.total_violations == 0
    assert len(dogfood_report.foreign_transfer_violations) == 0
    assert len(dogfood_report.forms) == 0

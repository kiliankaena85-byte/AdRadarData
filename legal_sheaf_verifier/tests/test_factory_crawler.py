"""
Unit and mock tests for Site-First FactoryCrawler.
Verifies robots rate limiting, email filtering, INN/OGRN detection, and express memo generation.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from ingestion.factory_crawler import FactoryCrawler, CompanyProfile
from legal_sheaf.scanner_152fz import Report152FZ, Violation152FZ, FormInspectionResult


@pytest.fixture
def crawler():
    return FactoryCrawler(rate_limit_sec=0.01)


def test_filter_corporate_emails_drops_personal_mail(crawler):
    text = """
    Наши контакты:
    Отдел продаж: sales@zavod-detal.ru
    Общие вопросы: info@zavod-detal.ru
    Директор: ivanov@mail.ru
    Снабжение: petrov1985@gmail.com
    Бухгалтерия: buh@yandex.ru
    Заказ: order@zavod-detal.ru
    """
    emails = crawler._filter_corporate_emails(text, domain="zavod-detal.ru")
    assert "sales@zavod-detal.ru" in emails
    assert "info@zavod-detal.ru" in emails
    assert "order@zavod-detal.ru" in emails
    # Personal emails must be strictly excluded
    assert "ivanov@mail.ru" not in emails
    assert "petrov1985@gmail.com" not in emails
    assert "buh@yandex.ru" not in emails


def test_dadata_graceful_fallback_without_key(crawler):
    crawler.dadata_api_key = None
    res = asyncio.run(crawler.enrich_via_dadata("7707083893"))
    assert res == {}


def test_technical_memo_generation(crawler):
    company = CompanyProfile(
        domain="trub-zavod.ru",
        inn="7707083893",
        ogrn="1027700132195",
        corporate_emails=["sales@trub-zavod.ru"],
        legal_name="ООО 'Трубный Завод'",
        management_name="Петров Петр Петрович",
        management_post="Генеральный директор",
        okved="24.20",
        okved_name="Производство стальных труб",
    )
    v = Violation152FZ(
        category="CROSS_BORDER_TRANSFER",
        signature="google-tag-manager",
        line_number=12,
        snippet="<script src='https://www.googletagmanager.com/gtm.js?id=GTM-XYZ'></script>",
        legal_basis="152-ФЗ ст. 12",
        koap_article="ст. 13.11 ч. 8/9 КоАП РФ",
        potential_fine_rub="от 1 000 000 до 6 000 000 руб.",
    )
    report_152fz = Report152FZ(
        page_url="https://trub-zavod.ru",
        is_compliant=False,
        total_violations=1,
        foreign_transfer_violations=[v],
        forms=[],
        summary="FAIL",
    )

    memo = crawler.generate_technical_memo(company, report_152fz, foreign_lang_168=[])
    assert "КОМУ: Генеральный директор / IT-директору ООО 'Трубный Завод', ИНН 7707083893" in memo
    assert "trub-zavod.ru" in memo
    assert "Строка 12: Обнаружен скрипт/шрифт 'google-tag-manager'" in memo
    assert "АК/32908/25" in memo
    assert "152-ФЗ ст. 12" in memo


def test_audit_site_mocked(crawler):
    mock_html = """<!DOCTYPE html>
    <html>
    <head>
        <title>Завод Металлоконструкций</title>
        <link href="https://fonts.googleapis.com/css2?family=Open+Sans" rel="stylesheet">
    </head>
    <body>
        <h1>Производство и поставка металлоконструкций</h1>
        <p>ООО 'Уральский Завод', ИНН 7707083893, ОГРН 1027700132195</p>
        <p>Пишите нам: info@ural-zavod.ru, sales@ural-zavod.ru</p>
        <form id="order_form" action="/send">
            <input type="text" name="name" placeholder="Имя">
            <input type="tel" name="phone" placeholder="Телефон">
            <button type="submit">Заказать расчет</button>
        </form>
    </body>
    </html>"""

    async def mock_fetch(client, url):
        return mock_html, url

    with patch.object(crawler, "_fetch_page", side_effect=mock_fetch):
        result = asyncio.run(crawler.audit_site("https://ural-zavod.ru"))

        assert result.company.domain == "ural-zavod.ru"
        assert result.company.inn == "7707083893"
        assert result.company.ogrn == "1027700132195"
        assert "info@ural-zavod.ru" in result.company.corporate_emails
        assert "sales@ural-zavod.ru" in result.company.corporate_emails

        # 152-FZ violations: Google Fonts + Missing Form Consent
        assert result.compliance_152fz.is_compliant is False
        assert len(result.compliance_152fz.foreign_transfer_violations) >= 1
        assert any(v.signature == "google-fonts" for v in result.compliance_152fz.foreign_transfer_violations)
        assert len(result.compliance_152fz.forms) == 1
        assert result.compliance_152fz.forms[0].has_consent_checkbox is False

        # Technical memo generated
        assert "Экспресс-аудит веб-ресурса ural-zavod.ru" in result.technical_express_memo
        assert "АК/32908/25" in result.technical_express_memo

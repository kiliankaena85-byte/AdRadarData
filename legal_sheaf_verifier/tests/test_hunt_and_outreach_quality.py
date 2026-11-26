"""
Regression tests for Sprint 8 hunt precision, ICP filtering, KoAP/regulator accuracy,
anti-3% human-readable outreach, and corporate contact / CEO FIO extraction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from ingestion.factory_crawler import CompanyProfile, SiteAuditResult
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.koap_calculator import KoAPSanctionsCalculator
from legal_sheaf.lead_packager import LeadPackager, build_human_salutation
from legal_sheaf.scanner_152fz import Compliance152FZScanner, Report152FZ, Violation152FZ
from legal_sheaf.schema import LegalRegime, VerificationReport


def _stub_external_site_deps(monkeypatch, domain: str = "real-biz.ru") -> None:
    monkeypatch.setattr(
        "legal_sheaf.net_safety.resolve_and_verify_host",
        lambda host: (True, ["93.184.216.34"], ""),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.check_rkn_registry_async",
        AsyncMock(return_value={"status": "NOT_FOUND", "is_registered_in_rkn": False, "operator_name": None}),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.discover_perimeter_hosts",
        AsyncMock(return_value=[domain]),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.inspect_infrastructure_geo_and_mx",
        AsyncMock(return_value={"findings": [], "mx_records": []}),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.inspect_external_js_endpoints",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.resolve_phoenix_clones_in_fns",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.detect_regalia_and_pdf_chronology_mismatch",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.audit_vendor_supply_chain",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        "legal_sheaf.dual_auditor.enforce_fns_rate_limit_async",
        AsyncMock(return_value=None),
    )


def test_audit_article_uses_raw_links_and_title_hint(champion_bundle):
    auditor = DualAuditor(bundle=champion_bundle)
    plain_body = (
        "Обновлено в ноябре 2026 года. В этом разборе делимся практикой выбора "
        "облачной CRM для небольших отделов продаж и автоматизации заявок."
    )
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/services/998877",
            text=plain_body,
            raw_links=["https://shop-example.ru/?ref=777"],
            title_hint="Настоящий заголовок",
            author_hint="Редакция CRM",
        )
    )
    assert res["is_violation_38fz"] is True
    assert res["scenario_38fz"] == "UNMARKED_AD_NO_ERID"
    assert res["title"] == "Настоящий заголовок"
    assert "https://shop-example.ru/?ref=777" in res["website_urls"]


def test_hunt_vcru_filters_gov_legal_messengers_and_foreign_sites(tmp_path, monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="real-biz.ru")

    real_home = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>РеалБиз</title></head>
<body>
  <h1>Автоматизация складского учета РеалБиз</h1>
  <p>Цена от 9900 руб. Заказать внедрение. <a href="/contacts">Контакты</a></p>
  <form method="post" action="/order">
    <input type="text" name="phone" placeholder="Телефон">
    <button type="submit">Оставить заявку</button>
  </form>
</body></html>"""

    real_contacts = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Контакты</title></head>
<body>
  <p>ООО «РеалБиз Софт», ИНН 7704386120, ОГРН 1167746890123</p>
  <p>Email: <a href="mailto:director@real-biz.ru">director@real-biz.ru</a></p>
</body></html>"""

    foreign_home = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Foreign Skins</title></head>
<body>
  <h1>Маркетплейс игровых предметов на русском языке</h1>
  <p>Пополнение и обмен в рублях. <a href="/privacy">Privacy</a></p>
  <form method="post" action="/signup">
    <input type="email" name="email">
    <button type="submit">Отправить</button>
  </form>
</body></html>"""

    art1_blocks = [
        {
            "type": "text",
            "data": {
                "text": (
                    "Обзор регулирования и платформ: "
                    '<a href="https://cbr.ru/press/">ЦБ</a>, '
                    '<a href="https://eec.eaeunion.org/docs">ЕЭК</a>, '
                    '<a href="https://www.consultant.ru/doc/1">Консультант</a>, '
                    '<a href="https://base.garant.ru/123/">Гарант</a>, '
                    '<a href="https://support.google.com/chrome">Google</a>, '
                    '<a href="https://help.steampowered.com/ru">Steam</a>, '
                    '<a href="https://max.ru/chat">MAX</a>, '
                    '<a href="https://teletype.in/@author/post">Teletype</a>.'
                )
            },
        }
    ]
    art2_blocks = [
        {
            "type": "text",
            "data": {
                "text": (
                    "Сравниваем два сервиса: "
                    '<a href="https://foreign-skins.com/ru/?utm_source=vc">зарубежный сервис</a> и '
                    '<a href="https://real-biz.ru/?utm_source=vc">российский сервис</a>.'
                )
            },
        }
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host or ""
        if host == "api.vc.ru" and "/v2.1/timeline" in url:
            if "sorting=date" in url and "lastId=" not in url:
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "items": [
                                {
                                    "data": {
                                        "id": 880001,
                                        "title": "Обзоры документов и платформ",
                                        "author": {"name": "Юрист Тест"},
                                        "url": "https://vc.ru/legal/880001",
                                        "blocks": art1_blocks,
                                    }
                                },
                                {
                                    "data": {
                                        "id": 880002,
                                        "title": "Инструменты для бизнеса в 2026 году",
                                        "author": {"name": "Автор Обзора"},
                                        "url": "https://vc.ru/services/880002",
                                        "blocks": art2_blocks,
                                    }
                                },
                            ],
                            "lastId": 880002,
                            "lastSortingValue": 1,
                        }
                    },
                )
            return httpx.Response(200, json={"result": {"items": []}})
        if host == "bo.nalog.gov.ru":
            return httpx.Response(
                200,
                json={"content": [{"shortName": "ООО «РЕАЛБИЗ СОФТ»", "okved2": "62.01", "bfo": {"period": 2025, "gainSum": 50000}}]},
            )
        if host == "real-biz.ru":
            if request.url.path in ("", "/"):
                return httpx.Response(200, text=real_home, headers={"Content-Type": "text/html; charset=utf-8"})
            if "/contacts" in request.url.path:
                return httpx.Response(200, text=real_contacts, headers={"Content-Type": "text/html; charset=utf-8"})
            return httpx.Response(404, text="Not Found")
        if host == "foreign-skins.com":
            if request.url.path in ("", "/"):
                return httpx.Response(200, text=foreign_home, headers={"Content-Type": "text/html; charset=utf-8"})
            return httpx.Response(404, text="Not Found")
        return httpx.Response(404, text="Not Found")

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), follow_redirects=False) as client:
            return await auditor.hunt_vcru(limit=2, output_dir=tmp_path / "hunt_out", client=client)

    summary = asyncio.run(_run())
    assert summary["packaged_leads_count"] == 1
    assert [l["domain"] for l in summary["packaged_leads"]] == ["real-biz.ru"]
    audited_domains = {r["advertiser_domain"] for r in summary["consolidated_company_reports"]}
    assert "real-biz.ru" in audited_domains
    for noise in ("cbr.ru", "eaeunion.org", "consultant.ru", "base.garant.ru", "garant.ru", "support.google.com", "google.com", "help.steampowered.com", "steampowered.com", "max.ru", "teletype.in"):
        assert noise not in audited_domains


def test_hunt_vcru_red_mapping_and_tier_default(tmp_path, monkeypatch, champion_bundle):
    captured = {}

    class SpyPackager(LeadPackager):
        def package_lead(self, company, site_audit, sheaf_report, **kwargs):
            captured["company"] = company
            captured["site_audit"] = site_audit
            return super().package_lead(company, site_audit, sheaf_report, **kwargs)

    auditor = DualAuditor(bundle=champion_bundle)
    monkeypatch.setattr(
        auditor,
        "harvest_vc_timeline",
        AsyncMock(
            return_value=[
                {
                    "id": 771122,
                    "title": "Как выбрать сервис доставки",
                    "author": "Независимый автор",
                    "post_url": "https://vc.ru/services/771122",
                    "full_text": "Детальный разбор рынка.",
                    "commercial_links": ["https://smb-shop.ru/catalog"],
                    "ad_data": {},
                    "is_promo": False,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        auditor,
        "audit_article",
        AsyncMock(
            return_value={
                "article_url": "https://vc.ru/services/771122",
                "title": "Как выбрать сервис доставки",
                "author": "Независимый автор",
                "scenario_38fz": "CLEAN_NON_AD",
                "is_violation_38fz": False,
                "verdict_38fz": "Аналитика",
                "commercial_links": ["https://smb-shop.ru/catalog"],
                "website_urls": ["https://smb-shop.ru/catalog"],
                "violation_probability": 0.04,
                "conformal_verdict": "CLEAN",
                "sheaf_energy": 0.01,
                "penalties": {},
            }
        ),
    )
    monkeypatch.setattr(
        auditor,
        "audit_site",
        AsyncMock(
            return_value={
                "domain": "smb-shop.ru",
                "final_url": "https://smb-shop.ru",
                "status": "OK",
                "detected_lang": "ru",
                "cyrillic_ratio": 0.9,
                "pages_inspected": ["https://smb-shop.ru"],
                "company_profile": {
                    "inn": "7704386120",
                    "ogrn": "1167746890123",
                    "legal_name": "ООО «СМБ Шоп»",
                    "corporate_emails": ["sales@smb-shop.ru"],
                    "telegram_contacts": [],
                    "management_name": None,
                    "management_post": None,
                    "revenue_rub": None,
                    "revenue_year": None,
                    "is_foreign_kio": False,
                    "is_sme": True,
                },
                "traffic_light": {
                    "RED_REAL_FINE": [
                        {
                            "signature": "missing-consent-checkbox",
                            "tier": "RED_REAL_FINE",
                            "is_real_fine": True,
                            "koap_article": "13.11_part_2",
                            "legal_basis": "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ",
                            "description": "Сбор ПД без отдельного чекбокса согласия",
                            "witness": {
                                "source_url": "https://smb-shop.ru",
                                "line_number": 27,
                                "raw_snippet": "<form><input name='phone'></form>",
                                "quote": "<form>",
                            },
                        }
                    ],
                    "ORANGE_REGULATORY_DEMAND": [],
                    "GRAY_HYGIENE_BONUS": [],
                },
                "penalties": {},
            }
        ),
    )

    spy_packager = SpyPackager(output_root=tmp_path / "leads")
    summary = asyncio.run(auditor.hunt_vcru(limit=1, output_dir=tmp_path / "out", packager=spy_packager))
    assert summary["packaged_leads_count"] == 1
    assert captured["company"].lead_tier == "TIER_B_SMB_IP"
    mapped = captured["site_audit"].compliance_152fz.foreign_transfer_violations[0]
    assert mapped.risk_level == "HIGH"
    assert mapped.legal_basis == "ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ"
    assert mapped.koap_article == "13.11_part_2"
    assert mapped.line_number == 27


def test_koap_calculator_maps_168fz_to_14_8_not_13_11():
    calc = KoAPSanctionsCalculator()
    assert calc.map_violation_to_article("commercial_b2c_foreign_words_168fz") == "14.8_part_1"
    res = calc.calculate_penalties(["commercial_b2c_foreign_words_168fz"], is_sme=False, first_offense=True)
    assert res["total_min_rubles"] == 5000
    assert res["total_max_rubles"] == 10000
    assert res["grouped_penalties"][0]["article"] == "14.8_part_1"


def test_lead_packager_no_raw_slugs_correct_regulator_and_external_author(tmp_path):
    packager = LeadPackager(output_root=tmp_path)
    comp = CompanyProfile(
        domain="market-brand.ru",
        inn="7704386120",
        legal_name="ООО «Маркет Бренд»",
        lead_tier="TIER_A_CORP",
        revenue_rub=120_000_000,
        revenue_year="2025",
        corporate_emails=["info@market-brand.ru"],
    )
    sheaf_clean = VerificationReport(
        is_violation=False,
        regime=LegalRegime.CLEAN_NON_AD,
        regime_name="CLEAN_NON_AD",
        violation_probability=0.03,
        conformal_verdict="CLEAN",
        top_edge_conflicts=[],
        legal_citations=[],
        procedural_risks=[],
        regime_probabilities={"CLEAN_NON_AD": 0.97},
        total_sheaf_energy=0.01,
        active_slots=[],
        mitigating_factors=[],
        fast_standalone_mode=True,
    )

    # Case A: ZoZPP / 168-FZ violations (Rospotrebnadzor, Art. 14.8, line_number=1 -> no "(строка 1)")
    site_rpn = SiteAuditResult(
        company=comp,
        pages_inspected=["https://market-brand.ru"],
        compliance_152fz=Report152FZ(
            page_url="https://market-brand.ru",
            is_compliant=False,
            total_violations=2,
            foreign_transfer_violations=[
                Violation152FZ(
                    category="HIGH_LOCALIZATION_RISK",
                    risk_level="HIGH",
                    signature="missing_seller_requisites_zozpp",
                    line_number=1,
                    snippet="footer",
                    legal_basis="ст. 9 ЗоЗПП, ч. 1 ст. 14.8 КоАП РФ",
                    koap_article="14.8_part_1",
                    potential_fine_rub="5 000 - 10 000 руб.",
                    remediation_note="Скрыты реквизиты продавца",
                ),
                Violation152FZ(
                    category="LOW_CDN_TRANSBORDER_RISK",
                    risk_level="LOW",
                    signature="commercial_b2c_foreign_words_168fz",
                    line_number=1,
                    snippet="sale, discount",
                    legal_basis="Федеральный закон № 168-ФЗ",
                    koap_article="168_fz",
                    potential_fine_rub="5 000 - 10 000 руб.",
                    remediation_note="Иностранные слова без перевода",
                ),
            ],
            forms=[],
            summary="2 замечания",
        ),
    )

    info_a = packager.package_lead(
        company=comp,
        site_audit=site_rpn,
        sheaf_report=sheaf_clean,
        post_url="https://vc.ru/101",
        post_title="Аналитика рынка электроники",
        author_domain="vc.ru",
    )
    outreach_a = Path(info_a["outreach_path"]).read_text(encoding="utf-8")
    html_a = Path(info_a["html_path"]).read_text(encoding="utf-8")

    assert "Заметили упоминание вашего сервиса market-brand.ru" in outreach_a
    assert "с вашим материалом" not in outreach_a
    assert "Роспотребнадзора" in outreach_a
    assert "14.8" in outreach_a
    assert "по ч. 1 ст. 13.11 КоАП РФ" not in outreach_a
    assert "фокусе контрольных мероприятий Роскомнадзора" not in outreach_a
    assert "(строка 1)" not in outreach_a
    assert "стр. 1<" not in html_a
    assert "3%" not in outreach_a and "2%" not in outreach_a
    assert "14 900" not in outreach_a and "14900" not in outreach_a
    for raw_slug in ("missing_seller_requisites_zozpp", "commercial_b2c_foreign_words_168fz", "HIGH_LOCALIZATION_RISK", "LOW_CDN_TRANSBORDER_RISK", "CLEAN_NON_AD"):
        assert raw_slug not in outreach_a
        assert raw_slug not in html_a

    # Case B: 152-FZ checkbox violation at line 42 + own author
    site_rkn = SiteAuditResult(
        company=comp,
        pages_inspected=["https://market-brand.ru"],
        compliance_152fz=Report152FZ(
            page_url="https://market-brand.ru",
            is_compliant=False,
            total_violations=1,
            foreign_transfer_violations=[
                Violation152FZ(
                    category="HIGH_LOCALIZATION_RISK",
                    risk_level="HIGH",
                    signature="missing-consent-checkbox",
                    line_number=42,
                    snippet="<form><input name='tel'></form>",
                    legal_basis="ч. 2 ст. 13.11 КоАП РФ, ст. 9 152-ФЗ",
                    koap_article="13.11_part_2",
                    potential_fine_rub="30 000 - 150 000 руб.",
                    remediation_note="Отсутствует чекбокс согласия",
                ),
            ],
            forms=[],
            summary="1 замечание",
        ),
    )
    info_b = packager.package_lead(
        company=comp,
        site_audit=site_rkn,
        sheaf_report=sheaf_clean,
        post_url="https://vc.ru/102",
        post_title="Кейс собственной команды",
        author_domain="market-brand.ru",
    )
    outreach_b = Path(info_b["outreach_path"]).read_text(encoding="utf-8")
    assert "С интересом ознакомились с вашим материалом" in outreach_b
    assert "Роскомнадзора" in outreach_b
    assert "13.11" in outreach_b
    assert "(строка 42)" in outreach_b
    assert "missing-consent-checkbox" not in outreach_b


def test_audit_site_extracts_contacts_and_ceo_salutation(monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="test-llc.ru")

    home_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Тест ООО</title></head>
<body>
  <h1>Решения для производства</h1>
  <p>ИНН 7704386120, стоимость от 5000 руб. <a href="/contacts">Контакты</a></p>
</body></html>"""

    contacts_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Контакты</title></head>
<body>
  <p>ООО «ТестТех», ИНН 7704386120</p>
  <p>Генеральный директор: Петров Алексей Сергеевич</p>
  <p>Почта: <a href="mailto:petrov@test-llc.ru">petrov@test-llc.ru</a>, Telegram: <a href="https://t.me/testllc_mgr">@testllc_mgr</a></p>
</body></html>"""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "test-llc.ru":
            if request.url.path in ("", "/"):
                return httpx.Response(200, text=home_html, headers={"Content-Type": "text/html; charset=utf-8"})
            if "/contacts" in request.url.path:
                return httpx.Response(200, text=contacts_html, headers={"Content-Type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="Not Found")

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), follow_redirects=False) as client:
            return await auditor.audit_site("test-llc.ru", client=client, max_pages=4, deep=False, gear="qualifier_l1")

    res = asyncio.run(_run())
    cp = res["company_profile"]
    assert cp["corporate_emails"] == ["petrov@test-llc.ru"]
    assert "@testllc_mgr" in cp["telegram_contacts"]
    assert cp["management_name"] == "Петров Алексей Сергеевич"
    assert build_human_salutation("Петров Алексей Сергеевич", ["petrov@test-llc.ru"]) == "Алексей Сергеевич, добрый день!"
    assert "для Алексея Сергеевича" in build_human_salutation("Петров Алексей Сергеевич", ["sales@test-llc.ru"])


def test_no_refund_b2c_clause_requires_real_clause_not_missing_address():
    from legal_sheaf.forensics_v3 import policy_and_terms_forensics

    clean_oferta = "<p>Пользовательское соглашение регулирует порядок доступа к сервису без указания почтового индекса.</p>"
    res_clean = policy_and_terms_forensics("biz.ru", oferta_html=clean_oferta)
    assert not any(f["signature"] == "illegal_no_refund_b2c_clause" for f in res_clean)

    bad_oferta = "<p>При отказе заказчика оплаченные суммы возврату не подлежат ни при каких обстоятельствах.</p>"
    res_bad = policy_and_terms_forensics("biz.ru", oferta_html=bad_oferta)
    matched = [f for f in res_bad if f["signature"] == "illegal_no_refund_b2c_clause"]
    assert len(matched) == 1
    assert matched[0]["source_page_type"] == "oferta"
    assert "возврату не подлежат" in matched[0]["raw_snippet"].lower()


def test_owner_inn_extraction_ignores_yandex_subprocessor_in_policy(monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="owner-ip.ru")

    home_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Сервис ИП</title></head>
    <body>
      <h1>Автоматизация договоров для юристов</h1>
      <p>ИП Залуцкий Дмитрий Дмитриевич, ОГРНИП 325930100056570</p>
      <a href="/privacy.html">Политика</a> <a href="/contacts.html">Контакты</a>
    </body></html>"""

    policy_with_yandex = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Политика</title></head>
    <body>
      <p>На сайте используется Яндекс.Метрика (ООО «ЯНДЕКС», ИНН 7736207543, ОГРН 1027700229193).</p>
    </body></html>"""

    contacts_with_ip_inn = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Контакты</title></head>
    <body>
      <p>Индивидуальный предприниматель Залуцкий Дмитрий Дмитриевич, ИНН 781443687899, ОГРНИП 325930100056570</p>
    </body></html>"""

    def handler_no_own_inn(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(200, text=home_html, headers={"Content-Type": "text/html; charset=utf-8"})
        if "privacy" in request.url.path:
            return httpx.Response(200, text=policy_with_yandex, headers={"Content-Type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="Not Found")

    def handler_with_ip_inn(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(200, text=home_html, headers={"Content-Type": "text/html; charset=utf-8"})
        if "privacy" in request.url.path:
            return httpx.Response(200, text=policy_with_yandex, headers={"Content-Type": "text/html; charset=utf-8"})
        if "contacts" in request.url.path:
            return httpx.Response(200, text=contacts_with_ip_inn, headers={"Content-Type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="Not Found")

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run(fn):
        async with httpx.AsyncClient(transport=httpx.MockTransport(fn), follow_redirects=False) as client:
            return await auditor.audit_site("owner-ip.ru", client=client, max_pages=4, deep=False, gear="qualifier_l1")

    res_no_inn = asyncio.run(_run(handler_no_own_inn))
    assert res_no_inn["company_profile"]["inn"] is None

    res_with_inn = asyncio.run(_run(handler_with_ip_inn))
    assert res_with_inn["company_profile"]["inn"] == "781443687899"


def test_beget_hoster_inn_excluded_and_gir_bo_html_stripped(monkeypatch, champion_bundle):
    from legal_sheaf.forensics_v3 import resolve_phoenix_clones_in_fns

    _stub_external_site_deps(monkeypatch, domain="client-print.ru")

    home_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Печать</title></head>
    <body>
      <h1>Промышленные решения печати</h1>
      <p>ООО «Передовые системы печати», ИНН 7447227974</p>
      <footer>Работает на хостинге ООО "БЕГЕТ", ИНН 7801451618, ОГРН 1077847645590</footer>
    </body></html>"""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if host == "client-print.ru":
            return httpx.Response(200, text=home_html, headers={"Content-Type": "text/html; charset=utf-8"})
        if host == "bo.nalog.gov.ru":
            q = str(request.url.params.get("query", ""))
            if "7447227974" in q:
                return httpx.Response(
                    200,
                    json={
                        "content": [{
                            "shortName": 'ООО "<strong>ПЕРЕДОВЫЕ</strong> СИСТЕМЫ ПЕЧАТИ"',
                            "okved2": "46.69",
                            "bfo": {"period": 2025, "gainSum": 277563},
                        }]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"inn": "9724129559", "ogrn": "1237700282555", "shortName": 'ООО "<strong>БЕГЕТ</strong>"', "statusCode": "INACTIVE", "statusDate": "2026-02-20"},
                        {"inn": "7801451618", "ogrn": "1077847645590", "shortName": 'ООО "<strong>БЕГЕТ</strong>"', "statusCode": "ACTIVE", "statusDate": "2007-10-22", "bfo": {"gainSum": 1812602}},
                    ]
                },
            )
        return httpx.Response(404, text="Not Found")

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler), follow_redirects=False) as client:
            site_res = await auditor.audit_site("client-print.ru", client=client, max_pages=3, deep=False, gear="qualifier_l1")
            phx_res = await resolve_phoenix_clones_in_fns(["ООО \"БЕГЕТ\""], client=client, site_inns={"7447227974"})
            return site_res, phx_res

    site_res, phx_res = asyncio.run(_run())
    assert site_res["company_profile"]["inn"] == "7447227974"
    assert site_res["company_profile"]["legal_name"] == 'ООО "ПЕРЕДОВЫЕ СИСТЕМЫ ПЕЧАТИ"'
    assert "<strong>" not in site_res["company_profile"]["legal_name"]
    assert phx_res == []


def test_regalia_chronology_ignores_editorial_2022_year_mentions():
    from legal_sheaf.forensics_v3 import detect_regalia_and_pdf_chronology_mismatch

    editorial_html = "<html><body><p>В 2022 году изменились требования закона, а в 2021 году вышел обзор практики.</p></body></html>"
    res_clean = asyncio.run(detect_regalia_and_pdf_chronology_mismatch("325930100056570", editorial_html, []))
    assert res_clean == []

    real_mismatch_html = "<html><body><p>© 2021 Компания. Патент RU 190486 на конструкцию.</p></body></html>"
    res_real = asyncio.run(detect_regalia_and_pdf_chronology_mismatch("1246900012017", real_mismatch_html, []))
    assert len(res_real) == 1
    assert res_real[0]["signature"] == "regalia_chronology_mismatch"
    assert "2021" in res_real[0]["raw_snippet"]


def test_meta_extremist_ignores_blog_article_cover_webp():
    from legal_sheaf.forensics_v3 import meta_extremist_vs_blocked_rule

    blog_cover_html = '<html><body><img src="/static/blog/kak-sdelat-rils-v-instagram-likemania.webp" alt="Рилс в Инстаграм"></body></html>'
    assert meta_extremist_vs_blocked_rule(blog_cover_html) == []

    icon_html = '<html><body><img src="/icons/instagram.svg" alt="Instagram"></body></html>'
    res_icon = meta_extremist_vs_blocked_rule(icon_html)
    assert len(res_icon) == 1 and res_icon[0]["signature"] == "extremist_meta_social_links_20_3"

    link_html = '<html><body><a href="https://instagram.com/shop/">IG</a></body></html>'
    res_link = meta_extremist_vs_blocked_rule(link_html)
    assert len(res_link) == 1 and res_link[0]["signature"] == "extremist_meta_social_links_20_3"


def test_superlative_ignores_aria_label_and_scripts_triggers_on_visible_h1(monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="sup-check.ru")

    clean_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Сервис</title></head>
    <body>
      <span aria-label="лучшее условие">99,9%</span>
      <script>var msg = "лучшая защита вашего аккаунта";</script>
      <p>Сервис платежей и онлайн-доставки.</p>
    </body></html>"""

    dirty_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Сервис</title></head>
    <body>
      <h1>Мы лучший сервис №1 по доставке в Москве</h1>
      <p>Оформляйте заказы онлайн каждый день.</p>
    </body></html>"""

    def make_transport(body: str):
        def _h(request: httpx.Request) -> httpx.Response:
            if request.url.path in ("", "/"):
                return httpx.Response(200, text=body, headers={"Content-Type": "text/html; charset=utf-8"})
            return httpx.Response(404, text="Not Found")
        return httpx.MockTransport(_h)

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run(body: str):
        async with httpx.AsyncClient(transport=make_transport(body), follow_redirects=False) as client:
            return await auditor.audit_site("sup-check.ru", client=client, max_pages=1, deep=False, gear="qualifier_l1")

    res_clean = asyncio.run(_run(clean_html))
    assert not any(f["signature"] == "superlative_without_asterisk" for f in res_clean["traffic_light"]["RED_REAL_FINE"])

    res_dirty = asyncio.run(_run(dirty_html))
    sup_reds = [f for f in res_dirty["traffic_light"]["RED_REAL_FINE"] if f["signature"] == "superlative_without_asterisk"]
    assert len(sup_reds) == 1
    assert "aria-label" not in sup_reds[0]["witness"]["raw_snippet"]
    assert "сервис" in sup_reds[0]["witness"]["raw_snippet"].lower()


def test_outreach_link_framing_distinguishes_ref_utm_and_direct_links(tmp_path):
    packager = LeadPackager(output_root=tmp_path)
    comp = CompanyProfile(
        domain="print-co.ru",
        inn="7447227974",
        legal_name="ООО «Принт Ко»",
        lead_tier="TIER_A_CORP",
        revenue_rub=200_000_000,
        corporate_emails=["info@print-co.ru"],
    )
    site_ok = SiteAuditResult(
        company=comp,
        pages_inspected=["https://print-co.ru"],
        compliance_152fz=Report152FZ(
            page_url="https://print-co.ru",
            is_compliant=True,
            total_violations=0,
            foreign_transfer_violations=[],
            forms=[],
            summary="OK",
        ),
    )
    sheaf_viol = VerificationReport(
        is_violation=True,
        regime=LegalRegime.UNMARKED_AD_NO_ERID,
        regime_name="UNMARKED_AD_NO_ERID",
        violation_probability=0.92,
        conformal_verdict="VIOLATION",
        top_edge_conflicts=[],
        legal_citations=["ч. 16 ст. 14.3 КоАП РФ"],
        procedural_risks=[],
        regime_probabilities={"UNMARKED_AD_NO_ERID": 0.92},
        total_sheaf_energy=1.2,
        active_slots=[],
        mitigating_factors=[],
        fast_standalone_mode=True,
    )

    res_direct = packager.package_lead(
        company=comp,
        site_audit=site_ok,
        sheaf_report=sheaf_viol,
        post_url="https://vc.ru/id1/100",
        post_title="Обзор оборудования",
        target_link="https://print-co.ru/catalog/product/model-1/",
        author_domain="vc.ru",
    )
    txt_direct = Path(res_direct["outreach_path"]).read_text(encoding="utf-8")
    assert res_direct["template_type"] == "DIRECT_POST_OR_OVERMARKED"
    assert "партнерских или реферальных ссылок" not in txt_direct
    assert "14.3 КоАП РФ" in txt_direct and "821/23" in txt_direct

    res_utm = packager.package_lead(
        company=comp,
        site_audit=site_ok,
        sheaf_report=sheaf_viol,
        post_url="https://vc.ru/id1/101",
        post_title="Озвучка рекламы",
        target_link="https://print-co.ru/?utm_source=vc&utm_medium=promo",
        author_domain="vc.ru",
    )
    txt_utm = Path(res_utm["outreach_path"]).read_text(encoding="utf-8")
    assert res_utm["template_type"] == "AFFILIATE_SHIELD"
    assert "UTM-ссылкой" in txt_utm
    assert "партнерских или реферальных ссылок" not in txt_utm

    res_ref = packager.package_lead(
        company=comp,
        site_audit=site_ok,
        sheaf_report=sheaf_viol,
        post_url="https://vc.ru/id1/102",
        post_title="ТОП сервисов",
        target_link="https://print-co.ru/?ref=777",
        author_domain="vc.ru",
    )
    txt_ref = Path(res_ref["outreach_path"]).read_text(encoding="utf-8")
    assert res_ref["template_type"] == "AFFILIATE_SHIELD"
    assert "партнерских или реферальных ссылок" in txt_ref


def test_hunt_non_icp_hosts_blocks_vk_ru_and_google_dev():
    from legal_sheaf.dual_auditor import HUNT_NON_ICP_HOSTS
    from legal_sheaf.inn_erid_validators import _is_host_in_set

    for host in ("vk.ru", "google.dev", "vercel.com", "deepseek.com"):
        assert _is_host_in_set(host, HUNT_NON_ICP_HOSTS), f"{host} must be filtered by HUNT_NON_ICP_HOSTS"


def test_tildacdn_treated_as_static_cdn_not_toxic_widget():
    from legal_sheaf.vendor_chain import audit_vendor_supply_chain, match_known_vendor

    for cdn_host in ("neo.tildacdn.com", "static.tildacdn.info"):
        m = match_known_vendor(cdn_host)
        assert m is not None and m.get("is_cdn") is True

    html_page = '<html><body><script src="https://neo.tildacdn.com/js/tilda.min.js"></script></body></html>'
    findings, dossiers = asyncio.run(
        audit_vendor_supply_chain(
            client_domain="client-tilda.ru",
            all_html_pages=[html_page],
            policy_html="<html><body>Политика конфиденциальности ООО Клиент.</body></html>",
            max_unknown_vendors=1,
        )
    )
    assert not any(f["signature"] == "toxic_supply_chain_widget" for f in findings)
    assert not any(f["signature"] == "undisclosed_third_party_processor" for f in findings)
    assert dossiers == []


def test_superlative_ignores_customer_reviews_and_blockquotes(monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="reviews-check.ru")

    reviews_only_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Продукт</title></head>
    <body>
      <div class="reviews-list"><p>Антон Из RU Store. Пользуюсь ХК много лет. Лучшее приложение!</p></div>
      <blockquote>Честно говорю, это лучший образовательный продукт, который я видела.</blockquote>
      <p>Нейросеть явно советует его как лучший выбор.</p>
      <p>Регистрация и вход в личный кабинет.</p>
    </body></html>"""

    ad_h1_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Продукт</title></head>
    <body>
      <h1>Мы лучший сервис №1 по доставке по России</h1>
      <p>Заказывайте доставку прямо сейчас.</p>
    </body></html>"""

    def make_transport(body: str):
        def _h(request: httpx.Request) -> httpx.Response:
            if request.url.path in ("", "/"):
                return httpx.Response(200, text=body, headers={"Content-Type": "text/html; charset=utf-8"})
            return httpx.Response(404, text="Not Found")
        return httpx.MockTransport(_h)

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run(body: str):
        async with httpx.AsyncClient(transport=make_transport(body), follow_redirects=False) as client:
            return await auditor.audit_site("reviews-check.ru", client=client, max_pages=1, deep=False, gear="qualifier_l1")

    res_reviews = asyncio.run(_run(reviews_only_html))
    assert not any(f["signature"] == "superlative_without_asterisk" for f in res_reviews["traffic_light"]["RED_REAL_FINE"])

    res_ad = asyncio.run(_run(ad_h1_html))
    sup_reds = [f for f in res_ad["traffic_light"]["RED_REAL_FINE"] if f["signature"] == "superlative_without_asterisk"]
    assert len(sup_reds) == 1


def test_hunt_non_icp_hosts_blocks_example_and_pages_dev():
    from legal_sheaf.dual_auditor import HUNT_NON_ICP_HOSTS
    from legal_sheaf.inn_erid_validators import _is_host_in_set

    for host in ("example.ru", "example.com", "pages.dev", "workers.dev"):
        assert _is_host_in_set(host, HUNT_NON_ICP_HOSTS), f"{host} must be filtered by HUNT_NON_ICP_HOSTS"


def test_tildacdn_skipped_by_inspect_external_js_endpoints():
    from legal_sheaf.forensics_v3 import inspect_external_js_endpoints

    html = '<html><body><script src="https://neo.tildacdn.com/js/tilda-fallback-1.0.min.js"></script></body></html>'
    leaks = asyncio.run(inspect_external_js_endpoints({"client-tilda.ru": html}))
    assert leaks == []


def test_superlative_ignores_editorial_media_path_and_quoted_lists(monkeypatch, champion_bundle):
    _stub_external_site_deps(monkeypatch, domain="media-test.ru")

    home_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Сервис</title></head>
    <body>
      <p>Платформа аналитики брендов. В отчетах встречаются списки «лучших» моделей.</p>
      <a href="/media/">Медиа</a>
    </body></html>"""

    media_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Медиа</title></head>
    <body>
      <h1>Исследование 2026: 35,7% цитат — это списки «лучших»</h1>
      <p>Подборки лучших сервисов по версии читателей.</p>
    </body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("", "/"):
            return httpx.Response(200, text=home_html, headers={"Content-Type": "text/html; charset=utf-8"})
        if request.url.path.startswith("/media"):
            return httpx.Response(200, text=media_html, headers={"Content-Type": "text/html; charset=utf-8"})
        return httpx.Response(404, text="Not Found")

    auditor = DualAuditor(bundle=champion_bundle)

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            res = await auditor.audit_site("media-test.ru", client=client, max_pages=3, deep=False, gear="qualifier_l1")
            res_direct_media = await auditor.audit_site("media-test.ru/media/", client=client, max_pages=1, deep=False, gear="qualifier_l1")
            return res, res_direct_media

    res, res_direct_media = asyncio.run(_run())
    assert not any(f["signature"] == "superlative_without_asterisk" for f in res["traffic_light"]["RED_REAL_FINE"])
    assert not any(f["signature"] == "superlative_without_asterisk" for f in res_direct_media["traffic_light"]["RED_REAL_FINE"])

"""
Unit & Integration Tests for Vendor Chain (Hop 0 -> Hop 1) and Deep Forensics.
Strict compliance with GEMINI.md, 12 Legal Axioms v2.0, and Oracle F Citation Lock.
"""

import asyncio
import time
import pytest
from legal_sheaf.vendor_chain import (
    KNOWN_INFRA_VENDORS,
    match_known_vendor,
    get_cached_vendor_dossier,
    save_vendor_dossier,
    save_discovered_vendor_lead,
    init_vendor_db,
    audit_vendor_supply_chain,
)
from legal_sheaf.forensics_v3 import (
    meta_extremist_vs_blocked_rule,
    policy_and_terms_forensics,
    detect_missing_seller_requisites,
)


def test_known_infra_vendor_matching():
    # 1. Russian known vendors
    yandex_match = match_known_vendor("mc.yandex.ru")
    assert yandex_match is not None
    assert yandex_match["brand"] == "Яндекс.Метрика"
    assert yandex_match["inn"] == "7736207543"
    assert yandex_match["skip_hop"] is True

    jivo_match = match_known_vendor("code.jivo.ru")
    assert jivo_match is not None
    assert jivo_match["brand"] == "JivoSite"
    assert jivo_match["inn"] == "7722744877"

    # 2. Known foreign vendors
    gtm_match = match_known_vendor("www.googletagmanager.com")
    assert gtm_match is not None
    assert gtm_match["brand"] == "Google Tag Manager"
    assert gtm_match["is_foreign"] is True

    # 3. Static CDN
    cdn_match = match_known_vendor("cdnjs.cloudflare.com")
    assert cdn_match is not None
    assert cdn_match.get("is_cdn") is True

    # 4. Unknown vendor
    unknown_match = match_known_vendor("widget-js.athenachat.ai")
    assert unknown_match is None


def test_vendor_dossier_sqlite_cache_roundtrip():
    sample_dossier = {
        "vendor_root": "testvendor.ai",
        "vendor_host": "widget.testvendor.ai",
        "brand_name": "TestVendor",
        "inn": "7704386120",
        "entity_name": "ООО «ТестВендор»",
        "is_registered_in_rkn": True,
        "rkn_reg_number": "77-22-000000",
        "has_cross_border_declared": False,
        "servers_geo": [{"host": "widget.testvendor.ai", "ip": "1.2.3.4", "country": "RU", "is_foreign": False}],
        "has_foreign_servers": False,
        "terms_url": "https://testvendor.ai/terms",
        "policy_url": "https://testvendor.ai/policy",
        "is_russian_entity": True,
        "hop_level": 1,
    }

    save_vendor_dossier(sample_dossier)
    cached = get_cached_vendor_dossier("testvendor.ai")
    assert cached is not None
    assert cached["inn"] == "7704386120"
    assert cached["entity_name"] == "ООО «ТестВендор»"
    assert cached["is_registered_in_rkn"] is True


def test_discovered_vendor_lead_persistence():
    save_discovered_vendor_lead(
        inn="254008176561",
        entity_name="ИП Борисов Юрий Николаевич",
        vendor_root="athenachat.ai",
        found_on_client_domain="c3.c3.ru",
        risk_reasons="Серверы приема данных размещены за пределами РФ (ч. 8 ст. 13.11 КоАП РФ)",
    )

    conn = init_vendor_db()
    cursor = conn.cursor()
    cursor.execute("SELECT inn, entity_name, vendor_root, found_on_client_domain FROM discovered_vendor_leads WHERE inn = '254008176561'")
    row = cursor.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "254008176561"
    assert "Борисов" in row[1]
    assert row[2] == "athenachat.ai"
    assert row[3] == "c3.c3.ru"


def test_vendor_supply_chain_audit_undisclosed_processor():
    html_page = """
    <html><body>
        <h1>Добро пожаловать</h1>
        <script src="https://code.jivo.ru/widget/abc12345"></script>
    </body></html>
    """
    # Policy does NOT mention Jivo or Живой сайт
    policy_html = "<html><body>Политика конфиденциальности компании ООО Ромашка. Мы обрабатываем данные самостоятельно.</body></html>"

    findings, dossiers = asyncio.run(audit_vendor_supply_chain(
        client_domain="romashka.ru",
        all_html_pages=[html_page],
        policy_html=policy_html,
        max_unknown_vendors=1,
    ))

    # Should detect undisclosed third party processor
    assert any(f["signature"] == "undisclosed_third_party_processor" for f in findings)
    jivo_finding = next(f for f in findings if f["signature"] == "undisclosed_third_party_processor")
    assert "JivoSite" in jivo_finding["description"]
    assert "ч. 3 ст. 6, ст. 18.1 152-ФЗ" in jivo_finding["legal_basis"]


def test_meta_extremist_vs_blocked_social_networks_comprehensive():
    # 1. LinkedIn & X must NOT trigger 20.3
    html_blocked = """
    <footer>
        <a href="https://linkedin.com/company/test"><i class="fa fa-linkedin"></i></a>
        <a href="https://x.com/test"><svg class="x-icon"></svg></a>
    </footer>
    """
    findings_b = meta_extremist_vs_blocked_rule(html_blocked)
    assert len(findings_b) == 0

    # 2. WhatsApp* with Meta footnote must NOT trigger 20.3
    html_wa_footnote = """
    <div>Напишите нам в WhatsApp* для консультации</div>
    <footer>
        <small>*WhatsApp принадлежит компании Meta, признанной экстремистской организацией в РФ</small>
    </footer>
    """
    findings_wa = meta_extremist_vs_blocked_rule(html_wa_footnote)
    assert len(findings_wa) == 0

    # 3. Direct Instagram profile link triggers 20.3
    html_insta = '<div><a href="https://www.instagram.com/my_company/">Наш блог в Инстаграм</a></div>'
    findings_insta = meta_extremist_vs_blocked_rule(html_insta)
    assert len(findings_insta) == 1
    assert findings_insta[0]["signature"] == "extremist_meta_social_links_20_3"
    assert "ч. 1 ст. 20.3 КоАП РФ" in findings_insta[0]["legal_basis"]


def test_policy_and_terms_forensics_all_defects():
    audited = "example.com"

    # Defect 1: policy domain mismatch
    p_mismatch = "1.2. Данная политика регламентирует сайт https://www.otherdomain.ru."
    f1 = policy_and_terms_forensics(audited, policy_html=p_mismatch)
    assert any(f["signature"] == "policy_domain_mismatch_paradox" for f in f1)

    # Defect 2: implicit browse consent
    p_implicit = "Продолжая использование сайта, вы безоговорочно соглашаетесь с обработкой ПДн."
    f2 = policy_and_terms_forensics(audited, policy_html=p_implicit)
    assert any(f["signature"] == "implicit_browse_consent_invalid" for f in f2)

    # Defect 3: technical analytics denied as PD (strictly citing 05-1297/422/2022 from whitelist)
    p_analytics = "Техническая информация, IP-адреса и файлы cookies не являются персональными данными."
    f3 = policy_and_terms_forensics(audited, policy_html=p_analytics)
    assert any(f["signature"] == "analytics_denied_as_pd" for f in f3)
    assert "05-1297/422/2022" in f3[0]["legal_basis"]

    # Defect 4: hidden subprocessors list
    p_sub = "Перечень субобработчиков и третьих лиц предоставляется по письменному запросу пользователя."
    f4 = policy_and_terms_forensics(audited, policy_html=p_sub)
    assert any(f["signature"] == "hidden_subprocessors_list" for f in f4)

    # Defect 5: illegal no-refund B2C clause in oferta
    o_no_refund = "Услуга считается оказанной в момент списания. Оплаченные суммы возврату не подлежат."
    f5 = policy_and_terms_forensics(audited, oferta_html=o_no_refund)
    assert any(f["signature"] == "illegal_no_refund_b2c_clause" for f in f5)

    # Defect 6: unprotected affiliate program without erid
    site_aff = "Станьте нашим партнером! Реферальная программа: комиссия за переход 20% каждому агенту."
    f6 = policy_and_terms_forensics(audited, site_html=site_aff)
    assert any(f["signature"] == "unprotected_affiliate_program" for f in f6)

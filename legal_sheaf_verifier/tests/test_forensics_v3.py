"""
Unit tests for Forensics v3.0 (Cross-Forensics & Perimeter Investigation).
"""

import pytest
from legal_sheaf.forensics_v3 import (
    extract_root_domain,
    detect_missing_seller_requisites,
    detect_policy_domain_and_entity_paradox,
    detect_instant_protocol_triggers,
)


def test_extract_root_domain():
    assert extract_root_domain("c3.c3.ru") == "c3.ru"
    assert extract_root_domain("https://promo.c3.ru/catalog") == "c3.ru"
    assert extract_root_domain("sub.example.com") == "example.com"
    assert extract_root_domain("example.com") == "example.com"


def test_missing_seller_requisites_detector():
    # Commercial site without INN or OGRN
    commercial_html = """
    <html><body>
        <a href="tel:88007751038">8 800 775-10-38</a>
        <button>Заказать расчет</button>
        <p>© 2021 Все права защищены</p>
    </body></html>
    """
    res = detect_missing_seller_requisites(commercial_html, "", found_inns=[], found_ogrns=[])
    assert res is not None
    assert res["signature"] == "missing_seller_requisites_zozpp"
    assert "ст. 9 Закона РФ № 2300-1" in res["legal_basis"]

    # Non-commercial or with valid INN
    res_compliant = detect_missing_seller_requisites(
        commercial_html, "", found_inns=[("7704386120", 10)], found_ogrns=[("1246900012017", 13)]
    )
    assert res_compliant is None


def test_policy_domain_mismatch_paradox():
    policy_text = """
    1.2. Настоящая политика применяется ко всей информации,
    которую Оператор может получить о посетителях веб-сайта https://www.promo.c3.ru.
    """
    findings = detect_policy_domain_and_entity_paradox(
        audited_domain="c3.c3.ru",
        policy_text=policy_text,
    )
    assert len(findings) == 1
    assert findings[0]["signature"] == "policy_domain_mismatch_paradox"
    assert findings[0]["referenced_domain"] == "promo.c3.ru"


def test_instant_protocol_triggers():
    # Extremist links
    html_meta = '<div>Мы в соцсетях: <a href="https://instagram.com/my_brand/">Instagram</a></div>'
    res_meta = detect_instant_protocol_triggers([html_meta])
    assert any(f["signature"] == "extremist_meta_social_links_20_3" for f in res_meta)

    # Installments without bank
    html_credit = '<p>Только сегодня: рассрочка 0% на все товары!</p>'
    res_credit = detect_instant_protocol_triggers([html_credit])
    assert any(f["signature"] == "unmarked_credit_installment_38fz" for f in res_credit)

    # Clean HTML
    html_clean = '<p>Доставка по всей России. Оплата картой при получении.</p>'
    assert len(detect_instant_protocol_triggers([html_clean])) == 0


def test_regalia_chronology_mismatch_detection():
    import asyncio
    from legal_sheaf.forensics_v3 import detect_regalia_and_pdf_chronology_mismatch
    # Active OGRN from 2024 with 2021 copyright and patent mentions
    html_sample = "<p>© 2021 Все права защищены. Патент RU 190486 на элементы ступеней.</p>"
    findings = asyncio.run(detect_regalia_and_pdf_chronology_mismatch(
        active_ogrn="1246900012017",  # Year 2024
        html_text=html_sample,
        pdf_urls=[],
    ))
    assert len(findings) == 1
    assert findings[0]["signature"] == "regalia_chronology_mismatch"
    assert findings[0]["ogrn_year"] == 2024


def test_meta_extremist_vs_blocked_social_networks_rule():
    from legal_sheaf.forensics_v3 import meta_extremist_vs_blocked_rule

    # 1. LinkedIn and X/Twitter icons and links MUST NOT be considered a violation
    html_blocked_not_extremist = """
    <html><body>
        <a href="https://linkedin.com/company/mybrand"><i class="fa-linkedin"></i></a>
        <a href="https://x.com/mybrand"><svg class="x-twitter-icon"></svg></a>
    </body></html>
    """
    findings_blocked = meta_extremist_vs_blocked_rule(html_blocked_not_extremist)
    assert len(findings_blocked) == 0, f"LinkedIn/X must not trigger 20.3: {findings_blocked}"

    # 2. Text mention of WhatsApp* with Meta footnote in footer -> CLEAN (0 violations)
    html_whatsapp_with_footnote = """
    <html><body>
        <div>Свяжитесь с нами в WhatsApp* или Telegram.</div>
        <footer>
            <small>*Деятельность организации Meta признана экстремистской и запрещена на территории РФ.</small>
        </footer>
    </body></html>
    """
    findings_wa = meta_extremist_vs_blocked_rule(html_whatsapp_with_footnote)
    assert len(findings_wa) == 0, f"WhatsApp* with footnote must give 0 violations: {findings_wa}"

    # 3. Instagram direct profile link or graphic icon -> RED violation
    html_insta_violation = """
    <html><body>
        <div class="social-bar">
            <a href="https://instagram.com/fashion_shop/"><img src="/icons/instagram.svg" alt="Instagram" /></a>
        </div>
    </body></html>
    """
    findings_insta = meta_extremist_vs_blocked_rule(html_insta_violation)
    assert len(findings_insta) == 1
    assert findings_insta[0]["signature"] == "extremist_meta_social_links_20_3"


def test_policy_and_terms_forensics_defects():
    from legal_sheaf.forensics_v3 import policy_and_terms_forensics

    # 1. Implicit browse consent invalid
    policy_implicit = "<p>Использование сайта означает безоговорочное согласие с условиями обработки.</p>"
    f_implicit = policy_and_terms_forensics("mybrand.ru", policy_html=policy_implicit)
    assert any(f["signature"] == "implicit_browse_consent_invalid" for f in f_implicit)

    # 2. Analytics denied as personal data (Case 05-1297/422/2022)
    policy_analytics = "<p>Техническая информация и файлы cookie не являются персональными данными.</p>"
    f_analytics = policy_and_terms_forensics("mybrand.ru", policy_html=policy_analytics)
    assert any(f["signature"] == "analytics_denied_as_pd" for f in f_analytics)
    for f in f_analytics:
        if f["signature"] == "analytics_denied_as_pd":
            assert "05-1297/422/2022" in f["legal_basis"]

    # 3. Hidden subprocessors list
    policy_hidden = "<p>Полный перечень субобработчиков предоставляется по запросу субъекта.</p>"
    f_hidden = policy_and_terms_forensics("mybrand.ru", policy_html=policy_hidden)
    assert any(f["signature"] == "hidden_subprocessors_list" for f in f_hidden)

    # 4. Illegal no-refund B2C clause in Oferta
    oferta_no_refund = "<p>Оплаченные суммы возврату не подлежат и не влекут перерасчета.</p>"
    f_refund = policy_and_terms_forensics("mybrand.ru", oferta_html=oferta_no_refund)
    assert any(f["signature"] == "illegal_no_refund_b2c_clause" for f in f_refund)

    # 5. Unprotected affiliate program
    site_affiliate = '<a href="/affiliate">Реферальная программа: комиссия за переходы до 20%</a>'
    f_affiliate = policy_and_terms_forensics("mybrand.ru", site_html=site_affiliate)
    assert any(f["signature"] == "unprotected_affiliate_program" for f in f_affiliate)



"""
Tests for Dual-Contour High-Accuracy Violation Detection:
1. Contour 1 (38-FZ Articles):
   - Subdomain neutral host exclusion (ru.wikipedia.org, companies.rbc.ru, pro.rbc.ru)
   - Multi-brand shield (`is_uniform_multibrand_shield`) disabled when single-vendor UTM / bot referral / promo code exists
   - USER2-base / n-gram Legal Prototype semantic projections in LinkCentricFeatureExtractor (no random untrained noise)
   - Conformal ABSTAIN_REVIEW routing in DualAuditor.audit_article & anchor href preservation
2. Contour 2 (152-FZ / RKN Corporate Websites):
   - Sibling <label for="..."> consent checkbox recognition (zero false positive missing-consent-checkbox)
   - SPA / Modal <div>-container lead capture detection without <form> tag (zero false negative)
   - Foreign tracker signatures registered in TIER_SIGNATURE_REGISTRY -> RED_REAL_FINE (not downgraded to GRAY)
   - RKN undeclared cross-border transfer detection (cross_border=False + active foreign tracker)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.enforcement_reality import EnforcementTier, classify_finding
from legal_sheaf.inn_erid_validators import extract_commercial_targets
from legal_sheaf.link_features import FEATURE_NAMES, LinkCentricFeatureExtractor
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.schema import DocumentInput, LegalRegime


def test_contour1_subdomain_neutral_host_filtering():
    txt = (
        "Аналитический разбор сделок M&A и макростатистики. "
        "Источники: https://companies.rbc.ru/news/12345/ и https://ru.wikipedia.org/wiki/Инвестиции "
        "а также https://forklog.com/news/test и seoded.blogspot.com."
    )
    res = extract_commercial_targets(txt, page_url="https://vc.ru/finance/999")
    assert res["commercial_urls"] == []
    assert res["website_urls"] == []
    assert res["text_domains"] == []

    # However, if a URL carries an explicit UTM/referral campaign, it MUST be caught!
    txt_utm = "Читайте наш кейс: https://ai.cdnvideo.ru/?utm_source=vc&utm_campaign=promo2026"
    res_utm = extract_commercial_targets(txt_utm, page_url="https://vc.ru/ai/998")
    assert len(res_utm["commercial_urls"]) == 1


def test_contour1_slot10_multi_brand_shield_blocked_by_utm_or_bot_ref():
    extractor = LinkCentricFeatureExtractor(device="cpu", use_fast_ngram_fallback=True)
    doc = DocumentInput(
        clean_text=(
            "Сравнение решений Anthropic Claude, OpenAI и Google для команд разработки. "
            "Официальный список стран: https://www.anthropic.com/supported-countries "
            "Подключить корпоративный шлюз со скидкой: https://ai.cdnvideo.ru/?utm_source=vc&utm_campaign=aihub"
        ),
        page_url="https://vc.ru/ai/3258589",
        author_domain="vc.ru",
        context_mode="EXTERNAL_PUBLICATION",
    )
    feat = extractor.build_feature_vector(doc).cpu().numpy()
    # Multi-brand shield MUST be 0.0 when single-vendor UTM link has no erid!
    assert float(feat[FEATURE_NAMES.index("is_uniform_multibrand_shield")]) == 0.0
    # Link topology and UTM features MUST be 1.0
    assert float(feat[FEATURE_NAMES.index("has_any_commercial_target")]) == 1.0
    assert float(feat[FEATURE_NAMES.index("has_affiliate_or_utm_or_shortener")]) == 1.0
    # Semantic prototype projections [28:48] must be deterministic and non-NaN
    f2 = extractor.build_feature_vector(doc).cpu().numpy()
    assert (feat[28:48] == f2[28:48]).all()


def test_contour1_audit_article_abstain_grey_zone_routing(champion_bundle):
    # Mock bundle returning ABSTAIN_REVIEW on an ambiguous non-affiliate mention
    fake_bundle = SimpleNamespace(
        verify_document=lambda doc: SimpleNamespace(
            is_violation=True,
            violation_probability=0.52,
            conformal_verdict="ABSTAIN_REVIEW",
            regime=LegalRegime.CLEAN_NON_AD,
            regime_name="CLEAN_NON_AD",
            total_sheaf_energy=0.2,
            active_slots=[],
        )
    )
    auditor = DualAuditor(bundle=fake_bundle)
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/life/777111",
            text="Житейская история про ремонт квартиры, плитку брали в локальном салоне https://plitka-dom.ru без промокодов.",
        )
    )
    assert res["scenario_38fz"] == "ABSTAIN_REVIEW"
    assert res["is_violation_38fz"] is False


def test_contour2_checkbox_label_for_and_spa_div_form_and_foreign_sigs():
    scanner = Compliance152FZScanner()

    # 1. Compliant form where <input type="checkbox" id="cb_pd"> is separated from <label for="cb_pd">
    compliant_html = """
    <html><body>
      <form id="lead-ok">
        <input type="tel" name="phone" placeholder="+7 999 000-00-00">
        <div class="chk-wrap"><input type="checkbox" id="cb_pd"></div>
        <label for="cb_pd">Даю согласие на обработку персональных данных согласно <a href="/privacy">Политике</a></label>
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    rep_ok = scanner.scan_html(compliant_html, page_url="https://example.ru")
    assert rep_ok.is_compliant is True
    assert rep_ok.total_violations == 0

    # 2. Non-compliant SPA <div> modal lead capture WITHOUT <form> tag + Cloudflare Turnstile + Google Analytics
    violating_spa_html = """
    <html><head>
      <script src="https://www.google-analytics.com/analytics.js"></script>
      <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
    </head><body>
      <div class="modal-callback">
        <input type="tel" name="user_phone" placeholder="Ваш телефон">
        <button type="button">Оставить заявку</button>
      </div>
    </body></html>
    """
    rep_bad = scanner.scan_html(violating_spa_html, page_url="https://bad-spa.ru")
    assert rep_bad.is_compliant is False
    sigs = {v.signature for v in rep_bad.foreign_transfer_violations}
    assert "google-analytics" in sigs
    assert "cloudflare-turnstile" in sigs
    form_sigs = {fv.signature for f in rep_bad.forms for fv in f.violations}
    assert "missing-consent-checkbox" in form_sigs

    # 3. Verify TIER_SIGNATURE_REGISTRY promotes google-analytics & cloudflare-turnstile to RED_REAL_FINE (not GRAY!)
    from legal_sheaf.enforcement_reality import EvidenceWitness
    w = EvidenceWitness(source_url="https://bad-spa.ru", line_number=2, raw_snippet="analytics.js", quote="analytics.js")
    w_rkn = EvidenceWitness(
        source_url="https://bad-spa.ru",
        line_number=1,
        raw_snippet="cross_border=False",
        quote="INN 7704386120",
        rkn_registry_response={"status": "SUCCESS", "is_registered_in_rkn": True, "has_cross_border_declared": False},
    )
    c_ga = classify_finding("google-analytics", context_mode="OWN_WEBSITE", witness=w)
    c_cf = classify_finding("cloudflare-turnstile", context_mode="OWN_WEBSITE", witness=w)
    c_rkn_unbacked = classify_finding("rkn_undeclared_cross_border_transfer", context_mode="OWN_WEBSITE", witness=w)
    c_rkn_backed = classify_finding("rkn_undeclared_cross_border_transfer", context_mode="OWN_WEBSITE", witness=w_rkn)
    assert c_ga["tier"] == EnforcementTier.RED_REAL_FINE.value
    assert c_cf["tier"] == EnforcementTier.RED_REAL_FINE.value
    assert c_rkn_unbacked["tier"] == EnforcementTier.GRAY_HYGIENE_BONUS.value
    assert c_rkn_backed["tier"] == EnforcementTier.ORANGE_REGULATORY_DEMAND.value

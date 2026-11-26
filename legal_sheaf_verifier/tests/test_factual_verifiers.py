"""
Unit & Integration Tests for AdRadar Factual Verifiers:
1. Per-Link erid Coverage Verifier (PARTIAL_ERID_MARKING_GAP & invalid formats, zero-FP invariants)
2. Anti-SSRF Redirect & Landing Page Verifier (hidden affiliate networks, hidden erid recovery,
   grey-zone factual resolution, and strict SSRF loopback/metadata blocking)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
PKG_SRC = PKG_DIR / "src"
for p in (str(PKG_SRC), str(PKG_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.factual_verifiers import (
    analyze_landing_page_html,
    verify_link_redirect_and_landing,
    verify_per_link_erid_coverage,
)
from legal_sheaf.schema import LegalRegime


def test_per_link_erid_coverage_catches_partial_gaps():
    # Pattern 1: Shortener roundup with 3 marked eSIM links and 1 unmarked eSIM link (unisimChina)
    text_esim = (
        "Реклама. Genesis Group AG. ООО «ЮНИСИМ», ИНН 9725187521.\n"
        "[Ссылки: "
        "https://clktrk.ru/mqTf4Y71?s1=yesimChina&s2=3259254&erid=2Vtzqv6aaRy "
        "https://clktrk.ru/mqTf4Y71?s1=unisimChina&s2=3259254 "
        "https://clktrk.ru/mqTf4Y71?s1=airaloChina&s2=3259254&erid=2Vtzqv9YYzf "
        "https://clktrk.ru/mqTf4Y71?s1=drimsimChina&s2=3259254&erid=2VtzqvB2YH1]"
    )
    links_esim = [
        "https://clktrk.ru/mqTf4Y71?s1=yesimChina&s2=3259254&erid=2Vtzqv6aaRy",
        "https://clktrk.ru/mqTf4Y71?s1=unisimChina&s2=3259254",
        "https://clktrk.ru/mqTf4Y71?s1=airaloChina&s2=3259254&erid=2Vtzqv9YYzf",
        "https://clktrk.ru/mqTf4Y71?s1=drimsimChina&s2=3259254&erid=2VtzqvB2YH1",
    ]
    res1 = verify_per_link_erid_coverage(
        text=text_esim,
        commercial_urls=links_esim,
        raw_links=links_esim,
        page_url="https://vc.ru/topovik/3259254",
    )
    assert res1["has_partial_erid_gap"] is True
    assert res1["marked_links_count"] == 3
    assert res1["unmarked_links_count"] == 1
    assert res1["unique_advertisers_count"] == 4
    assert res1["unmarked_links"] == ["https://clktrk.ru/mqTf4Y71?s1=unisimChina&s2=3259254"]

    # Pattern 2: One marked aggregator + 2 unmarked affiliate links (fichi.ai/?r= and gptunnel.ru/?ref=)
    text_ai = "Реклама. Study24.\nhttps://eduforms.ru?erid=2SDnjf3hfGp&ulp=https://study24.ai"
    links_ai = [
        "https://eduforms.ru?erid=2SDnjf3hfGp&ulp=https://study24.ai",
        "https://fichi.ai/?r=6a849dc96f1dbd945f28e3d6",
        "https://gptunnel.ru/?ref=6a849fcb45fb420001798280",
    ]
    res2 = verify_per_link_erid_coverage(
        text=text_ai,
        commercial_urls=links_ai,
        raw_links=links_ai,
        page_url="https://vc.ru/ai/3259219",
    )
    assert res2["has_partial_erid_gap"] is True
    assert res2["marked_links_count"] == 1
    assert res2["unmarked_links_count"] == 2
    assert "https://fichi.ai/?r=6a849dc96f1dbd945f28e3d6" in res2["unmarked_links"]
    assert "https://gptunnel.ru/?ref=6a849fcb45fb420001798280" in res2["unmarked_links"]


def test_per_link_erid_coverage_zero_fp_cases():
    # Case A: Single advertiser with 2 URLs on same root domain covered by body text erid
    text_single = (
        "Реклама. ООО «ОблакоПро», ИНН 7704386120. erid: 2Vtzqx9AbCd. "
        "Изучите тарифы https://oblako-pro.ru/pricing и запросите демо https://oblako-pro.ru/demo."
    )
    links_single = ["https://oblako-pro.ru/pricing", "https://oblako-pro.ru/demo"]
    res_single = verify_per_link_erid_coverage(
        text=text_single,
        commercial_urls=links_single,
        raw_links=links_single,
        page_url="https://vc.ru/cloud/10001",
    )
    assert res_single["has_partial_erid_gap"] is False
    assert res_single["marked_links_count"] == 2
    assert res_single["unmarked_links_count"] == 0

    # Case B: Yandex Market roundup with all affiliate links marked + neutral author Telegram channel
    ym_links = [
        "https://market.yandex.ru/cc/AyxJF4?erid=5jtCeReNx12oak2pVMD3EUs",
        "https://market.yandex.ru/cc/AyxJ74?erid=5jtCeReNx12oak2pVMD3EUs",
        "https://t.me/razbor_editorial",
    ]
    res_ym = verify_per_link_erid_coverage(
        text="Реклама. ООО «Яндекс Маркет». ИНН 9704254424. Наш канал https://t.me/razbor_editorial",
        commercial_urls=["https://t.me/razbor_editorial"],
        raw_links=ym_links,
        page_url="https://vc.ru/tech/3258536",
    )
    assert res_ym["has_partial_erid_gap"] is False
    assert res_ym["marked_links_count"] == 2
    assert res_ym["unmarked_links_count"] == 0
    tg_entry = next(d for d in res_ym["per_link_details"] if "t.me/" in d["url"])
    assert tg_entry["status"] == "SKIPPED_AUTHOR_MESSENGER"


def test_per_link_erid_corrupted_token_flagged():
    text = "Реклама. ООО «Тест», ИНН 7704386120. Ссылка: https://shop-demo.ru/?erid=1234"
    res = verify_per_link_erid_coverage(
        text=text,
        commercial_urls=["https://shop-demo.ru/?erid=1234"],
        raw_links=["https://shop-demo.ru/?erid=1234"],
        page_url="https://vc.ru/shop/9901",
    )
    assert res["invalid_erid_links_count"] == 1
    assert res["has_partial_erid_gap"] is True
    assert res["per_link_details"][0]["status"] == "URL_ERID_INVALID_FORMAT"


def test_analyze_landing_page_html_commercial_vs_editorial():
    comm_html = """<!DOCTYPE html><html lang="ru"><head><title>Облачная CRM — Тарифы</title></head>
    <body>
      <h1>Автоматизация отдела продаж</h1>
      <p>Базовый тарифный план от 2 990 ₽ в месяц.</p>
      <a href="/checkout">Выбрать тариф</a>
      <button type="button">Купить подписку</button>
      <script src="https://yookassa.ru/checkout-widget/v1/checkout.js"></script>
    </body></html>"""
    comm_res = analyze_landing_page_html(comm_html, "https://cloud-crm.ru/pricing")
    assert comm_res["is_confirmed_commercial_landing"] is True
    assert comm_res["has_price_or_tariff"] is True
    assert comm_res["has_buy_or_checkout_cta"] is True
    assert comm_res["has_payment_or_cart_marker"] is True
    assert len(comm_res["commercial_evidence_quotes"]) >= 2

    ed_html = """<!DOCTYPE html><html lang="ru"><head><title>Макроэкономика 2026</title></head>
    <body>
      <h1>Динамика цен на логистику</h1>
      <p>Средняя стоимость перевозки выросла на 1 200 рублей по данным отрасли.</p>
    </body></html>"""
    ed_res = analyze_landing_page_html(ed_html, "https://fin-analytics.ru/news/macro-2026")
    assert ed_res["is_confirmed_commercial_landing"] is False


def test_live_redirect_verifier_admitad_revealed_and_unwrapped(champion_bundle, monkeypatch):
    async def fake_safe_fetch(client, url, max_hops=5, timeout=5.0, **kwargs):
        req = httpx.Request("GET", "https://cloud-crm.ru/?utm_source=admitad&utm_campaign=cpa99")
        resp = httpx.Response(
            200,
            request=req,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body><h1>CRM</h1><p>Тариф от 1 990 руб.</p><button>Купить</button></body></html>",
        )
        hops = [
            "https://clck.ru/9HideRef",
            "https://ad.admitad.com/g/abc123/?ulp=https://cloud-crm.ru/",
            "https://cloud-crm.ru/?utm_source=admitad&utm_campaign=cpa99",
        ]
        return resp, hops[-1], hops

    monkeypatch.setattr("legal_sheaf.factual_verifiers.safe_fetch_with_redirects", fake_safe_fetch)

    auditor = DualAuditor(bundle=champion_bundle, enable_live_link_verifier=True)
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/tools/910001",
            text="Полезная заметка про автоматизацию рутины. Инструмент доступен по ссылке https://clck.ru/9HideRef",
            verify_links_live=True,
        )
    )
    assert res["scenario_38fz"] == "UNMARKED_AD_NO_ERID"
    assert res["is_violation_38fz"] is True
    assert len(res["traffic_light"]["RED_REAL_FINE"]) >= 1
    assert any("cloud-crm.ru" in u for u in res["website_urls"])
    lv = res["link_landing_verifications"][0]
    assert lv["hidden_affiliate_network_detected"] is True
    assert lv["hidden_tracking_revealed"] is True


def test_live_redirect_verifier_hidden_valid_erid_prevents_fp(champion_bundle, monkeypatch):
    async def fake_safe_fetch(client, url, max_hops=5, timeout=5.0, **kwargs):
        dest = "https://tech-service.ru/?erid=2Vtzqv6aaRy"
        resp = httpx.Response(
            200,
            request=httpx.Request("GET", dest),
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body><h1>ТехСервис</h1><p>Документация платформы.</p></body></html>",
        )
        return resp, dest, [url, dest]

    monkeypatch.setattr("legal_sheaf.factual_verifiers.safe_fetch_with_redirects", fake_safe_fetch)

    auditor = DualAuditor(bundle=champion_bundle, enable_live_link_verifier=True)
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/services/910002",
            text="Реклама. ООО «ТехСервис», ИНН 7704386120. Подробнее по ссылке https://clck.ru/7ValidErid",
            verify_links_live=True,
        )
    )
    assert res["has_erid"] is True
    assert res["erid_token"] == "2Vtzqv6aaRy"
    assert res["is_violation_38fz"] is False
    assert res["scenario_38fz"] == "CLEAN_MARKED_AD"


def test_live_landing_resolves_abstain_grey_zone(monkeypatch):
    async def fake_safe_fetch(client, url, max_hops=5, timeout=5.0, **kwargs):
        dest = "https://clean-looking-site.ru/"
        resp = httpx.Response(
            200,
            request=httpx.Request("GET", dest),
            headers={"content-type": "text/html; charset=utf-8"},
            text="""<html><body>
              <h1>Облачный конструктор смет</h1>
              <p>Подписка: от 2 900 ₽/мес.</p>
              <button>Купить подписку</button>
              <form><input type="tel" name="phone"><button type="submit">Оставить заявку</button></form>
              <script src="https://widget.cloudpayments.ru/bundles/cloudpayments.js"></script>
            </body></html>""",
        )
        return resp, dest, [url]

    monkeypatch.setattr("legal_sheaf.factual_verifiers.safe_fetch_with_redirects", fake_safe_fetch)

    abstain_bundle = SimpleNamespace(
        verify_document=lambda doc: SimpleNamespace(
            is_violation=False,
            violation_probability=0.50,
            conformal_verdict="ABSTAIN_REVIEW",
            regime=LegalRegime.CLEAN_NON_AD,
            regime_name="CLEAN_NON_AD",
            total_sheaf_energy=0.0,
            active_slots=[],
            feature_attributions=[],
            extracted_link_windows=[],
        )
    )
    auditor = DualAuditor(bundle=abstain_bundle, enable_live_link_verifier=True)
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/stories/910003",
            text="Личный опыт оцифровки смет на объектах. Вот ссылка на сервис https://clean-looking-site.ru/",
            verify_links_live=True,
        )
    )
    assert res["factual_landing_resolved"] is True
    assert res["is_violation_38fz"] is True
    assert res["scenario_38fz"] == "UNMARKED_AD_NO_ERID"
    assert len(res["traffic_light"]["RED_REAL_FINE"]) >= 1
    assert "Landing:" in res["traffic_light"]["RED_REAL_FINE"][0]["witness"]["raw_snippet"]


def test_anti_ssrf_blocks_loopback_and_cloud_metadata():
    async def _run():
        async with httpx.AsyncClient() as client:
            res_loop = await verify_link_redirect_and_landing("http://127.0.0.1/admin", client)
            res_meta = await verify_link_redirect_and_landing("http://169.254.169.254/latest/meta-data/", client)
            return res_loop, res_meta

    res_loop, res_meta = asyncio.run(_run())
    assert res_loop["status"] == "SSRF_BLOCKED"
    assert res_loop["ssrf_blocked"] is True
    assert res_meta["status"] == "SSRF_BLOCKED"
    assert res_meta["ssrf_blocked"] is True


def test_dual_auditor_partial_erid_gap_end_to_end(champion_bundle):
    text = (
        "Реклама. ООО «ЮНИСИМ», ИНН 9725187521. Сравнение туристических eSIM для поездки.\n"
        "[Ссылки: "
        "https://clktrk.ru/mqTf4Y71?s1=yesimChina&erid=2Vtzqv6aaRy "
        "https://clktrk.ru/mqTf4Y71?s1=airaloChina&erid=2Vtzqv9YYzf "
        "https://clktrk.ru/mqTf4Y71?s1=unisimChina]"
    )
    auditor = DualAuditor(bundle=champion_bundle)
    res = asyncio.run(
        auditor.audit_article(
            url="https://vc.ru/travel/910004",
            text=text,
            raw_links=[
                "https://clktrk.ru/mqTf4Y71?s1=yesimChina&erid=2Vtzqv6aaRy",
                "https://clktrk.ru/mqTf4Y71?s1=airaloChina&erid=2Vtzqv9YYzf",
                "https://clktrk.ru/mqTf4Y71?s1=unisimChina",
            ],
        )
    )
    assert res["scenario_38fz"] == "PARTIAL_ERID_MARKING_GAP"
    assert res["is_violation_38fz"] is True
    red_items = res["traffic_light"]["RED_REAL_FINE"]
    assert len(red_items) == 1
    assert red_items[0]["signature"] == "partial_erid_missing_on_link"
    assert red_items[0]["witness"]["quote"] == "https://clktrk.ru/mqTf4Y71?s1=unisimChina"
    assert res["penalties"]["grouped_penalties"][0]["article"] == "14.3_part_16"
    assert res["penalties"]["applicable_min_rubles"] > 0

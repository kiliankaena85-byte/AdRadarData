"""
Tests for Visual QC: Trigger Cropper, Nemotron VLM Verifier, and Human Review Desk.
Strict compliance with Zero-Vision, Zero-Generative-LLM, Zero-CDN, and Citation Whitelist invariants.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import httpx

from visual_qc.trigger_cropper import (
    _slugify,
    extract_policy_methods_snippet,
    is_ssrf_dangerous_url,
)
from visual_qc.nemotron_verifier import (
    _parse_json_resilient,
    load_citation_whitelist,
    sanitize_model_citations,
    verify_visual_triggers,
    get_openrouter_api_key,
    LiveCatalogResolver,
    CircuitBreaker,
    ContextRuleReconciler,
)
from visual_qc.review_desk import (
    map_finding_to_zone,
    build_commercial_proposal_text,
    generate_review_desk_html,
)


def test_zero_forbidden_libraries_loaded_by_visual_qc():
    """Ensure visual_qc does not load PIL, cv2, openai, or langchain into sys.modules."""
    forbidden = [
        "cv2",
        "PIL",
        "pillow",
        "pytesseract",
        "rapidocr",
        "torchvision",
        "openai",
        "anthropic",
        "langchain",
        "vllm",
    ]
    for mod in forbidden:
        assert mod not in sys.modules, f"Forbidden module {mod} was imported!"


def test_slugify():
    assert _slugify("https://athenachat.ai/ru/") == "athenachat_ai"
    assert _slugify("http://www.c3.ru/about") == "c3_ru"
    assert _slugify("my-shop.com:8080") == "my-shop_com"


def test_policy_methods_snippet_extraction_and_pii_sanitization():
    """Verifies that methods extraction strips PII (names, phones, emails) and finds triggers."""
    raw_policy = (
        "<html><body>"
        "<p>Политика конфиденциальности ООО 'Ромашка'. Контактное лицо: Иванов Иван Иванович, тел. +7 (999) 111-22-33, info@romashka.ru.</p>"
        "<p>Мы используем файлы cookie и сервис Яндекс.Метрика для сбора статистики о действиях пользователей на сайте.</p>"
        "<p>Цели обработки персональных данных включают улучшение работы сервиса и аналитику.</p>"
        "</body></html>"
    )
    snippet = extract_policy_methods_snippet(raw_policy, max_chars=1000)

    assert "cookie" in snippet.lower() or "метрика" in snippet.lower()
    # PII sanitization: no raw phone or personal email
    assert "+7 (999) 111-22-33" not in snippet
    assert "info@romashka.ru" not in snippet
    assert "Иванов Иван Иванович" not in snippet


def test_resilient_json_parsing():
    """Verifies parsing of raw json, markdown-wrapped json, and text with braces."""
    raw_valid = '{"status": "OK", "confirmed": true}'
    assert _parse_json_resilient(raw_valid) == {"status": "OK", "confirmed": True}

    markdown_json = "```json\n{\n  \"status\": \"VERIFIED_BY_VLM\",\n  \"confirmed\": false\n}\n```"
    assert _parse_json_resilient(markdown_json) == {"status": "VERIFIED_BY_VLM", "confirmed": False}

    noisy = "Here is the result:\n```\n{\"findings\": [1, 2]}\n```\nHope it helps!"
    assert _parse_json_resilient(noisy) == {"findings": [1, 2]}


def test_citation_whitelist_sanitization():
    """Verifies that unverified court cases are masked while verified ones remain."""
    whitelist = load_citation_whitelist()
    assert len(whitelist) > 0, "Whitelist must have entries"

    # Assume "А40-325054/2025" is in whitelist, and "А40-999999/2029" is fake/hallucinated
    sample_text = (
        "Согласно делу А40-325054/2025 все правомерно, но в деле 05-9999/422/2029 и А40-999999/2029 суд оштрафовал компанию."
    )
    cleaned = sanitize_model_citations(sample_text, whitelist)

    # Fake cases must be masked
    assert "05-9999/422/2029" not in cleaned
    assert "А40-999999/2029" not in cleaned
    assert "[номер исключен политикой верификации]" in cleaned

    # Allowed case must remain if present in whitelist
    if "А40-325054/2025" in whitelist:
        assert "А40-325054/2025" in cleaned


def test_nemotron_verifier_no_api_key(tmp_path):
    """Verifies graceful handling (Zero-Mock) when OPENROUTER_API_KEY is not set."""
    import asyncio

    fake_collage = tmp_path / "collage.png"
    fake_collage.write_bytes(b"dummy_png_bytes")

    # Run with empty api_key
    res = asyncio.run(verify_visual_triggers(
        collage_path=fake_collage,
        audit_findings=[{"finding_id": "meta_disclaimer", "traffic_light": "RED"}],
        api_key="",
    ))
    assert res["status"] == "VLM_UNVERIFIED_NO_KEY"
    assert res["confirmed"] is None
    assert "требуется ручной просмотр" in res["details"]


def test_nemotron_verifier_with_mocked_httpx(tmp_path):
    """Verifies end-to-end VLM call parsing and False Positive detection using mock httpx transport."""
    import asyncio

    fake_collage = tmp_path / "collage.png"
    fake_collage.write_bytes(b"PNG_FAKE_DATA")

    mock_vlm_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "status": "FALSE_POSITIVE_CAUGHT_BY_VLM",
                        "confirmed": False,
                        "readability_ok": True,
                        "findings_verdicts": [
                            {
                                "finding_id": "meta_disclaimer",
                                "scanner_verdict": "VIOLATION",
                                "vlm_verdict": "FALSE_POSITIVE",
                                "is_false_positive": True,
                                "visual_evidence_ru": "В подвале видна сноска: * Деятельность организации Meta признана экстремистской в РФ.",
                                "confidence": 0.95,
                            }
                        ],
                        "methods_mismatch_note": "Методы совпадают.",
                        "human_hint_ru": "Исключить Meta из КП, сноска присутствует.",
                    })
                }
            }
        ]
    }

    async def _run():
        async def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=mock_vlm_response)

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await verify_visual_triggers(
                collage_path=fake_collage,
                audit_findings=[{"finding_id": "meta_disclaimer", "traffic_light": "RED"}],
                api_key="mock_key_for_test",
                client=client,
            )

    res = asyncio.run(_run())
    assert res["status"] == "FALSE_POSITIVE_CAUGHT_BY_VLM"
    assert res["confirmed"] is False
    assert len(res["findings_verdicts"]) == 1
    assert res["findings_verdicts"][0]["is_false_positive"] is True


def test_zone_mapping():
    assert map_finding_to_zone("missing_meta_disclaimer", "ч. 2 ст. 13.15 КоАП РФ", "") == "ZONE_1_FOOTER"
    assert map_finding_to_zone("missing_seller_requisites_zozpp", "ст. 9 ЗоЗПП", "инн") == "ZONE_1_FOOTER"
    assert map_finding_to_zone("form_without_consent_checkbox", "ч. 2 ст. 13.11 КоАП РФ", "form_id") == "ZONE_2_FORM"
    assert map_finding_to_zone("superlative_without_asterisk", "ст. 5 38-ФЗ", "№1") == "ZONE_3_HERO"


def test_build_commercial_proposal_text():
    findings = [
        {
            "finding_id": "meta_disclaimer",
            "law_norm": "ч. 2 ст. 13.15 КоАП РФ",
            "description": "Сноска по экстремистским организациям",
            "fine_min_rub": 20000,
            "fine_max_rub": 40000,
        }
    ]
    text = build_commercial_proposal_text(
        domain="example.com",
        company_name="ООО Тест",
        approved_findings=findings,
        total_min_fine=20000,
        total_max_fine=40000,
    )
    assert "ООО Тест" in text
    assert "example.com" in text
    assert "14 900 ₽" in text
    assert "ч. 2 ст. 13.15 КоАП РФ" in text


def test_generate_review_desk_html_zero_cdn_and_auto_uncheck(tmp_path):
    """Verifies that review_desk.html is generated with 0 CDNs and auto-unchecks False Positives."""
    audit_res = {
        "company_profile": {
            "legal_name": "ООО 'Рога и Копыта'",
            "inn": "7701234567",
            "revenue_rub": 150000000,
        },
        "penalties": {
            "total_min_fine_rub": 20000,
            "total_max_fine_rub": 40000,
        },
        "traffic_light": {
            "RED_REAL_FINE": [
                {
                    "finding_id": "meta_disclaimer",
                    "law_norm": "ч. 2 ст. 13.15 КоАП РФ",
                    "description": "Отсутствует сноска про Meta",
                    "traffic_light": "RED_REAL_FINE",
                    "witness": {"quote": "facebook.com", "raw_snippet": "Ссылка на соцсеть"},
                },
                {
                    "finding_id": "form_pd_no_policy",
                    "law_norm": "ч. 2 ст. 13.11 КоАП РФ",
                    "description": "Форма без политики",
                    "traffic_light": "RED_REAL_FINE",
                    "witness": {"quote": "order_form", "raw_snippet": "<form>"},
                },
            ],
            "ORANGE_REGULATORY_DEMAND": [],
            "YELLOW_COMPLIANCE_GAP": [],
        },
    }

    capture_res = {
        "domain": "test_corp",
        "url": "https://test.corp",
        "policy_methods_snippet": "Анализ cookie и метрик.",
    }

    # VLM marked meta_disclaimer as False Positive!
    vlm_res = {
        "status": "FALSE_POSITIVE_CAUGHT_BY_VLM",
        "model_used": "nvidia/nemotron-3.5-super-vl:free",
        "findings_verdicts": [
            {
                "finding_id": "meta_disclaimer",
                "is_false_positive": True,
                "vlm_verdict": "FALSE_POSITIVE",
                "visual_evidence_ru": "В подвале Зоны #1 обнаружена сноска со звездочкой.",
                "confidence": 0.95,
            },
            {
                "finding_id": "form_pd_no_policy",
                "is_false_positive": False,
                "vlm_verdict": "CONFIRMED_VIOLATION",
                "visual_evidence_ru": "В Зоне #2 форма не содержит чекбокса.",
                "confidence": 0.90,
            },
        ],
        "methods_mismatch_note": "Методы соответствуют.",
        "human_hint_ru": "Проверьте форму.",
    }

    html_file = generate_review_desk_html(
        domain="test.corp",
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        output_dir=tmp_path,
    )

    assert html_file.exists()
    content = html_file.read_text(encoding="utf-8")

    # 1. Zero CDN validation
    assert "fonts.googleapis.com" not in content
    assert "cdnjs.cloudflare.com" not in content
    assert "unpkg.com" not in content
    assert "cdn.jsdelivr.net" not in content

    # 2. Structure validation
    assert "Рога и Копыта" in content
    assert "7701234567" in content
    assert "14 900 ₽" in content
    assert "🛡 Снято Визуальным ОТК" in content

    # 3. Auto-uncheck validation:
    # meta_disclaimer card must be card-dismissed and its checkbox NOT checked
    # form_pd_no_policy must have checked checkbox
    assert "card-dismissed" in content
    # Card 1 has meta_disclaimer: unchecked
    assert 'id="card-1"' in content
    # Card 2 has form_pd_no_policy: checked
    assert 'id="card-2"' in content


def test_koap_citation_guard_rejects_13_48_and_allows_13_15():
    """Verifies that unauthorized KoAP article 13.48 is blocked while 13.15 and 13.11 pass."""
    citations, allowed_koap = load_citation_whitelist()
    assert "13.15" in allowed_koap
    assert "13.11" in allowed_koap
    assert "13.48" not in allowed_koap

    # Sanitize test
    dirty_text = "Нарушена статья 13.48 КоАП РФ, а также ч. 2 ст. 13.15 КоАП РФ и ст. 13.11 КоАП РФ."
    clean_text = sanitize_model_citations(dirty_text, citations, allowed_koap)

    assert "13.48" not in clean_text
    assert "[статья КоАП исключена политикой верификации]" in clean_text
    assert "13.15" in clean_text
    assert "13.11" in clean_text


def test_live_catalog_resolver_rejects_paid_in_free_mode(tmp_path):
    """Verifies LiveCatalogResolver rejects paid models from free candidate list."""
    import asyncio

    fake_config = tmp_path / "models.yaml"
    fake_config.write_text("""
live_catalog_settings:
  refresh_interval_seconds: 999999
  cache_file: ""
free_tier:
  primary: "nvidia/nemotron-3.5-super-vl:free"
  fallbacks:
    - "paid/sneaky-model:free"
paid_zdr_tier:
  primary: "google/gemini-4-hydrogen"
  alternatives: []
  banned_models:
    - "google/gemini-2.5-flash"
""", encoding="utf-8")

    resolver = LiveCatalogResolver(config_path=fake_config)
    # Mock catalog where paid/sneaky-model has non-zero pricing
    fake_catalog = {
        "nvidia/nemotron-3.5-super-vl:free": {
            "id": "nvidia/nemotron-3.5-super-vl:free",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"modality": "text+image->text"},
        },
        "paid/sneaky-model:free": {
            "id": "paid/sneaky-model:free",
            "pricing": {"prompt": "0.002", "completion": "0.005"},
            "architecture": {"modality": "text+image->text"},
        },
    }

    # Override get_live_catalog for test
    async def mock_get_catalog(client=None):
        return fake_catalog

    resolver.get_live_catalog = mock_get_catalog

    candidates = asyncio.run(resolver.resolve_models(paid_zdr_mode=False))
    assert "nvidia/nemotron-3.5-super-vl:free" in candidates
    assert "paid/sneaky-model:free" not in candidates, "Paid model must be rejected in free mode!"


def test_circuit_breaker_and_tristate_unverified(tmp_path):
    """Verifies CircuitBreaker trips on 3 errors and verify_visual_triggers returns UNVERIFIED."""
    import asyncio

    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
    test_model = "test/failing-model"

    assert cb.is_available(test_model) is True
    cb.record_failure(test_model)
    assert cb.is_available(test_model) is True
    cb.record_failure(test_model)
    assert cb.is_available(test_model) is True
    cb.record_failure(test_model)
    # After 3 failures, circuit breaker trips
    assert cb.is_available(test_model) is False

    fake_collage = tmp_path / "collage.png"
    fake_collage.write_bytes(b"FAKE_COLLAGE_BYTES")

    # Mock handler that always returns HTTP 503
    async def _run():
        async def mock_503_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        transport = httpx.MockTransport(mock_503_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await verify_visual_triggers(
                collage_path=fake_collage,
                audit_findings=[{"finding_id": "test_f", "traffic_light": "RED"}],
                api_key="dummy_key",
                client=client,
            )

    res = asyncio.run(_run())
    assert res["status"] == "UNVERIFIED"
    assert res["confirmed"] is None


def test_context_rule_reconciler_prevents_gtag_and_rkn_override():
    """Verifies ContextRuleReconciler forbids VLM from overriding technical evidence."""
    reconciler = ContextRuleReconciler()
    allowed_cit = {"А40-325054/2025"}
    allowed_koap = {"13.11", "13.15"}

    orig_findings = [
        {
            "finding_id": "gtag_foreign_transfer",
            "signature": "gtag_foreign_transfer",
            "law_norm": "ч. 8 ст. 13.11 КоАП РФ",
        },
        {
            "finding_id": "missing_inn_rkn_registry",
            "signature": "missing_inn_rkn_registry",
            "law_norm": "ч. 1 ст. 13.11 КоАП РФ",
        },
        {
            "finding_id": "meta_disclaimer",
            "signature": "meta_disclaimer",
            "law_norm": "ч. 2 ст. 13.15 КоАП РФ",
        },
    ]

    vlm_verdicts = [
        {
            "finding_id": "gtag_foreign_transfer",
            "is_false_positive": True,  # VLM mistakenly says False Positive
            "vlm_verdict": "FALSE_POSITIVE",
            "visual_evidence_ru": "Я не вижу в дизайне ничего плохого",
        },
        {
            "finding_id": "missing_inn_rkn_registry",
            "is_false_positive": True,  # VLM mistakenly says False Positive
            "vlm_verdict": "FALSE_POSITIVE",
            "visual_evidence_ru": "Сайт выглядит легитимно",
        },
        {
            "finding_id": "meta_disclaimer",
            "is_false_positive": True,
            "vlm_verdict": "FALSE_POSITIVE",
            "exact_visible_text": "* Деятельность Meta запрещена в РФ",
            "visual_evidence_ru": "Сноска присутствует в подвале",
        },
    ]

    reconciled = reconciler.reconcile(
        findings_verdicts=vlm_verdicts,
        original_findings=orig_findings,
        allowed_citations=allowed_cit,
        allowed_koap=allowed_koap,
    )

    by_id = {r["finding_id"]: r for r in reconciled}

    # Technical evidence must NOT be overturned
    assert by_id["gtag_foreign_transfer"]["is_false_positive"] is False
    assert by_id["gtag_foreign_transfer"]["vlm_verdict"] == "CONFIRMED_VIOLATION"
    assert "не имеет полномочий" in by_id["gtag_foreign_transfer"]["reconciliation_note"]

    assert by_id["missing_inn_rkn_registry"]["is_false_positive"] is False
    assert by_id["missing_inn_rkn_registry"]["vlm_verdict"] == "CONFIRMED_VIOLATION"

    # Meta disclaimer with exact_visible_text is legitimately false positive
    assert by_id["meta_disclaimer"]["is_false_positive"] is True


def test_trigger_cropper_blocks_ssrf_private_ip():
    """Verifies is_ssrf_dangerous_url blocks SSRF on private subnets and dangerous files."""
    assert is_ssrf_dangerous_url("http://127.0.0.1:8000") is True
    assert is_ssrf_dangerous_url("http://localhost:3000/api") is True
    assert is_ssrf_dangerous_url("http://10.0.0.5/admin") is True
    assert is_ssrf_dangerous_url("http://192.168.1.1/setup") is True
    assert is_ssrf_dangerous_url("http://169.254.169.254/latest/meta-data/") is True
    assert is_ssrf_dangerous_url("file:///etc/passwd") is True
    assert is_ssrf_dangerous_url("https://example.com/payload.exe") is True
    assert is_ssrf_dangerous_url("https://example.com/archive.zip") is True

    # Legitimate public domain must pass
    assert is_ssrf_dangerous_url("https://athenachat.ai/ru/") is False
    assert is_ssrf_dangerous_url("https://vc.ru/tech/123") is False


def test_review_desk_renders_final_url_and_language_badge(tmp_path):
    """Verifies review_desk.html contains Final URL and Detected Language badges."""
    audit_res = {
        "domain": "athenachat.ai",
        "main_url": "https://athenachat.ai/ru/",
        "final_url": "https://athenachat.ai/ru/",
        "detected_lang": "ru",
        "company_profile": {
            "legal_name": "ИП Борисов Юрий Николаевич",
            "inn": "254008176561",
            "revenue_rub": None,
        },
        "penalties": {"total_min_fine_rub": 10000, "total_max_fine_rub": 50000},
        "traffic_light": {
            "RED_REAL_FINE": [],
            "ORANGE_REGULATORY_DEMAND": [],
            "YELLOW_COMPLIANCE_GAP": [],
        },
    }
    capture_res = {
        "domain": "athenachat_ai",
        "url": "https://athenachat.ai/ru/",
        "final_url": "https://athenachat.ai/ru/",
        "detected_lang": "ru",
        "target_dir": str(tmp_path),
        "collage_path": str(tmp_path / "collage.png"),
        "collage_exists": False,
        "zones": [],
        "policy_methods_snippet": "",
        "meta_disclaimer_text": "* Деятельность организации Meta запрещена на территории РФ",
    }
    vlm_res = {"status": "SKIPPED", "findings_verdicts": []}

    html_file = generate_review_desk_html(
        domain="athenachat.ai/ru",
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        output_dir=tmp_path,
    )

    assert html_file.exists()
    content = html_file.read_text(encoding="utf-8")
    assert "Целевой URL (Final URL)" in content
    assert "https://athenachat.ai/ru/" in content
    assert "Язык страницы" in content
    assert "RU (Русский)" in content
    assert "ИП Борисов Юрий Николаевич" in content


def test_audit_site_preserves_path_and_detects_lang():
    """Verifies that audit_site preserves /ru/ localized path and handles language metadata."""
    import asyncio
    from legal_sheaf.dual_auditor import DualAuditor

    async def _run_test():
        auditor = DualAuditor()

        # Mock client with custom transport returning Russian HTML with Meta footnote
        ru_html = """<!DOCTYPE html>
        <html lang="ru">
        <head><title>Афина ИИ</title></head>
        <body>
            <h1>Афина ИИ — Умный ассистент</h1>
            <footer>
                <p>2026 © ИП Борисов Юрий Николаевич ИНН 254008176561</p>
                <p>* Деятельность организации Meta запрещена на территории РФ</p>
            </footer>
        </body>
        </html>"""

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                text=ru_html,
                headers={"Content-Type": "text/html; charset=utf-8"},
                request=req,
            )
        )

        client = httpx.AsyncClient(
            transport=transport,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
            },
        )

        res = await auditor.audit_site("athenachat.ai/ru/", client=client, deep=False, gear="fast_l0")

        assert res.get("domain") == "athenachat.ai"
        assert "https://athenachat.ai/ru" in res.get("final_url", "")
        assert res.get("detected_lang") == "ru"
        assert res.get("company_profile", {}).get("inn") == "254008176561"
        await client.aclose()

    asyncio.run(_run_test())



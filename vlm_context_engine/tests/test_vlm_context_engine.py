"""
Comprehensive unit tests for vlm_context_engine.
Covers LiveCatalogResolver, Anti-SSRF router, OpenRouterGateway Track A & B,
CircuitBreaker, RateLimiter, ContextRuleReconciler, and ReviewDesk Zero-CDN HTML generation.
Strictly Zero-Vision-Library isolation (NO cv2, NO PIL).
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vlm_context_engine.live_catalog import LiveCatalogResolver, FinancialLockViolation
from vlm_context_engine.trigger_cropper import (
    is_ssrf_dangerous_url,
    extract_policy_methods_snippet,
)
from vlm_context_engine.openrouter_gateway import (
    OpenRouterGateway,
    CircuitBreaker,
    TokenBucketRateLimiter,
    VlmSqliteCache,
    extract_json_resilient,
    sanitize_model_citations,
    load_citation_whitelist,
)
from vlm_context_engine.reconciler import ContextRuleReconciler
from vlm_context_engine.review_desk import (
    ReviewDeskBuilder,
    generate_review_desk_html,
    map_finding_to_zone,
    build_commercial_proposal_text,
)


# =============================================================================
# 1. LIVE CATALOG RESOLVER & FINANCIAL LOCK TESTS
# =============================================================================
def test_live_catalog_financial_lock():
    resolver = LiveCatalogResolver()

    mock_catalog = {
        "free_model": {
            "id": "free_model",
            "pricing": {"prompt": "0.0", "completion": "0.0"},
            "architecture": {"input_modalities": ["text", "image"]},
        },
        "paid_model": {
            "id": "paid_model",
            "pricing": {"prompt": "0.000001", "completion": "0.000005"},
            "architecture": {"input_modalities": ["text", "image"]},
        },
        "no_image_model": {
            "id": "no_image_model",
            "pricing": {"prompt": "0.0", "completion": "0.0"},
            "architecture": {"input_modalities": ["text"]},
        },
    }

    assert resolver.verify_model_free("free_model", mock_catalog) is True
    assert resolver.verify_model_free("paid_model", mock_catalog) is False
    assert resolver.verify_model_free("no_image_model", mock_catalog) is False
    assert resolver.verify_model_free("openrouter/free", mock_catalog) is True
    assert resolver.verify_model_free("non_existent", mock_catalog) is False


def test_live_catalog_resolve_models_banned_exclusion():
    async def _run():
        resolver = LiveCatalogResolver()
        models = await resolver.resolve_models(paid_zdr_mode=False)
        for m in models:
            assert "ling-3.2-flash" not in m

    asyncio.run(_run())


# =============================================================================
# 2. SSRF BLOCKER TESTS
# =============================================================================
def test_is_ssrf_dangerous_url():
    # Private IPs and localhost must be blocked
    assert is_ssrf_dangerous_url("http://127.0.0.1/admin") is True
    assert is_ssrf_dangerous_url("http://localhost:8080/") is True
    assert is_ssrf_dangerous_url("http://0.0.0.0/") is True
    assert is_ssrf_dangerous_url("http://169.254.169.254/latest/meta-data/") is True
    assert is_ssrf_dangerous_url("http://10.0.0.15/internal") is True
    assert is_ssrf_dangerous_url("http://172.16.5.1/api") is True
    assert is_ssrf_dangerous_url("http://192.168.1.1/router") is True

    # Dangerous schemes
    assert is_ssrf_dangerous_url("file:///etc/passwd") is True
    assert is_ssrf_dangerous_url("ftp://ftp.example.com/secret") is True
    assert is_ssrf_dangerous_url("gopher://127.0.0.1:70") is True

    # Prohibited executable / script downloads
    assert is_ssrf_dangerous_url("https://example.com/setup.exe") is True
    assert is_ssrf_dangerous_url("https://example.com/payload.bat") is True
    assert is_ssrf_dangerous_url("https://example.com/archive.zip") is True
    assert is_ssrf_dangerous_url("https://example.com/script.sh") is True

    # Legitimate external URLs must be allowed
    assert is_ssrf_dangerous_url("https://athenachat.ai/ru") is False
    assert is_ssrf_dangerous_url("https://yulko.ru/policy/") is False
    assert is_ssrf_dangerous_url("https://example.com/about-us") is False


def test_extract_policy_methods_snippet():
    html_sample = """
    <html><body>
    <p>Мы используем файлы cookie и сервис Яндекс.Метрика для сбора статистики.</p>
    <p>Цели обработки персональных данных: исполнение договоров.</p>
    <p>Связаться с нами: test@example.com, +7 (999) 123-45-67.</p>
    </body></html>
    """
    snippet = extract_policy_methods_snippet(html_sample, max_chars=500)
    assert "cookie" in snippet or "метрика" in snippet.lower()
    # PII sanitization: raw email and phone must be sanitized
    assert "test@example.com" not in snippet
    assert "+7 (999) 123-45-67" not in snippet


# =============================================================================
# 3. OPENROUTER GATEWAY, CIRCUIT BREAKER, CACHE & CITATIONS TESTS
# =============================================================================
def test_circuit_breaker(tmp_path):
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0, db_path=tmp_path / "cb_isolated.sqlite")
    model = "test/model:free"

    assert cb.is_available(model) is True

    cb.record_failure(model)
    assert cb.is_available(model) is True

    cb.record_failure(model)
    assert cb.is_available(model) is True

    cb.record_failure(model)
    # Tripped after 3 failures
    assert cb.is_available(model) is False

    # Reset on success
    cb.record_success(model)
    assert cb.is_available(model) is True


def test_circuit_breaker_sqlite_persistence(tmp_path):
    db_file = tmp_path / "breaker_test.sqlite"
    cb1 = CircuitBreaker(failure_threshold=2, cooldown_seconds=300.0, db_path=db_file)
    model = "model/test_persistent"

    assert cb1.is_available(model) is True
    cb1.record_failure(model)
    cb1.record_failure(model)
    # Tripped
    assert cb1.is_available(model) is False

    # Instantiate cb2 with the same database file - must load tripped state from SQLite
    cb2 = CircuitBreaker(failure_threshold=2, cooldown_seconds=300.0, db_path=db_file)
    assert cb2.is_available(model) is False

    # Record success via cb2 - resets in SQLite
    cb2.record_success(model)
    assert cb2.is_available(model) is True

    # cb3 should now see available
    cb3 = CircuitBreaker(failure_threshold=2, cooldown_seconds=300.0, db_path=db_file)
    assert cb3.is_available(model) is True


def test_extract_json_resilient():
    # 1. Strips <think>...</think> reasoning tags
    raw_with_think = "<think>Let me analyze the zones carefully.</think>{\"status\": \"VERIFIED\", \"confirmed\": true}"
    res1 = extract_json_resilient(raw_with_think)
    assert res1["status"] == "VERIFIED"
    assert res1["confirmed"] is True

    # 2. Strips markdown fences
    raw_markdown = "```json\n{\"status\": \"CONFIRMED_VIOLATION\", \"fine\": 10000}\n```"
    res2 = extract_json_resilient(raw_markdown)
    assert res2["status"] == "CONFIRMED_VIOLATION"
    assert res2["fine"] == 10000

    # 3. Detects API error payload and raises ValueError
    raw_error = "{\"error\": {\"message\": \"Free quota exhausted\", \"code\": 429}}"
    with pytest.raises(ValueError) as exc:
        extract_json_resilient(raw_error)
    assert "API Error payload" in str(exc.value)


def test_rate_limiter():
    async def _run():
        rl = TokenBucketRateLimiter(max_rpm=60)
        t0 = time.time()
        await rl.acquire()
        await rl.acquire()
        assert (time.time() - t0) < 1.0

    asyncio.run(_run())


def test_sqlite_cache(tmp_path):
    db_file = tmp_path / "test_vlm_cache.sqlite"
    cache = VlmSqliteCache(db_path=db_file)

    key = cache.compute_key(b"fake_image_bytes", [{"id": "test_finding"}], "v1")
    assert cache.get(key) is None

    cache.set(key, {"verdict": "CONFIRMED"}, "test_model")
    cached = cache.get(key)
    assert cached is not None
    assert cached["verdict"] == "CONFIRMED"
    assert cached["model_used"] == "test_model"
    assert cached["from_cache"] is True


def test_sanitize_model_citations():
    allowed_citations = {"А40-325054/2025", "05-1234/2024"}
    allowed_koap = {"13.11", "13.15", "14.7", "20.3"}

    # Verified citations should be retained
    text_ok = "Дело А40-325054/2025 и ч. 2 ст. 13.15 КоАП РФ"
    sanitized_ok = sanitize_model_citations(text_ok, allowed_citations, allowed_koap)
    assert "А40-325054/2025" in sanitized_ok
    assert "13.15" in sanitized_ok

    # Hallucinated citations should be replaced
    text_bad = "Дело А40-999999/2025 и ст. 99.99 КоАП РФ"
    sanitized_bad = sanitize_model_citations(text_bad, allowed_citations, allowed_koap)
    assert "А40-999999/2025" not in sanitized_bad
    assert "[номер исключен политикой верификации]" in sanitized_bad
    assert "[статья КоАП исключена политикой верификации]" in sanitized_bad


def test_gateway_zero_mock_without_api_key(tmp_path):
    async def _run():
        gateway = OpenRouterGateway(api_key="")

        res_b = await gateway.track_b_chat_verify(
            collage_path=tmp_path / "non_existent.png",
            audit_findings=[{"finding_id": "test"}],
        )
        assert res_b["status"] == "VLM_UNVERIFIED_NO_KEY"
        assert res_b["confirmed"] is None

        res_a = await gateway.track_a_decide(
            instructions="test",
            context_data={},
            decision_questions=[{"id": "q1", "question": "test?"}],
        )
        assert res_a["status"] == "UNVERIFIED"

    asyncio.run(_run())


# =============================================================================
# 4. CONTEXT RULE RECONCILER TESTS
# =============================================================================
def test_reconciler_asymmetric_lock():
    reconciler = ContextRuleReconciler()

    # VLM trying to cancel technical code evidence (GTM or RKN registry)
    findings = [
        {"finding_id": "google_tag_manager_transfer", "signature": "google_tag_manager"},
        {"finding_id": "rkn_registry_missing", "signature": "missing_inn_rkn_registry"},
    ]
    vlm_verdicts = [
        {
            "finding_id": "google_tag_manager_transfer",
            "is_false_positive": True,
            "vlm_verdict": "FALSE_POSITIVE",
        },
        {
            "finding_id": "rkn_registry_missing",
            "is_false_positive": True,
            "vlm_verdict": "FALSE_POSITIVE",
        },
    ]

    reconciled = reconciler.reconcile(vlm_verdicts, findings)
    assert len(reconciled) == 2
    # Both false positives MUST be blocked!
    for r in reconciled:
        assert r["is_false_positive"] is False
        assert r["vlm_verdict"] == "CONFIRMED_VIOLATION"
        assert "VLM не имеет полномочий" in r["reconciliation_note"]


def test_reconciler_micro_font_requalification():
    reconciler = ContextRuleReconciler()

    findings = [
        {"finding_id": "meta_disclaimer", "signature": "art_13_15_meta_footnote"},
    ]
    # Disclaimer visually present, but font is unreadable micro-font
    vlm_verdicts = [
        {
            "finding_id": "meta_disclaimer",
            "is_false_positive": True,
            "element_visually_present": True,
            "readability_grade": "MICRO_FONT_UNREADABLE",
            "exact_visible_text": "*деятельность запрещена",
        },
    ]

    reconciled = reconciler.reconcile(vlm_verdicts, findings)
    assert len(reconciled) == 1
    item = reconciled[0]
    # Must NOT be marked false positive! Requalified to UNREADABLE_DISCLAIMER_RISK
    assert item["is_false_positive"] is False
    assert item["vlm_verdict"] == "UNREADABLE_DISCLAIMER_RISK"
    assert "микрошрифтом" in item["reconciliation_note"]


def test_reconciler_contextual_quarantine():
    reconciler = ContextRuleReconciler()

    # (A) Portfolio case: advertising claims quarantined
    findings = [
        {"finding_id": "ad_claim", "signature": "art_5_advertising_superlative"},
    ]
    vlm_verdicts = [
        {
            "finding_id": "ad_claim",
            "is_false_positive": False,
            "vlm_verdict": "CONFIRMED_VIOLATION",
        },
    ]

    reconciled = reconciler.reconcile(vlm_verdicts, findings, page_genre="PORTFOLIO_CASE")
    assert reconciled[0]["is_false_positive"] is True
    assert "Контекстный карантин" in reconciled[0]["reconciliation_note"]

    # (B) B2B Corporate: Anglicisms quarantined
    findings_b2b = [
        {"finding_id": "term_claim", "signature": "law_168_anglicism_untranslated"},
    ]
    vlm_verdicts_b2b = [
        {
            "finding_id": "term_claim",
            "is_false_positive": False,
            "vlm_verdict": "CONFIRMED_VIOLATION",
        },
    ]

    reconciled_b2b = reconciler.reconcile(vlm_verdicts_b2b, findings_b2b, page_genre="B2B_CORPORATE_ONLY")
    assert reconciled_b2b[0]["is_false_positive"] is True
    assert "Контекстный карантин" in reconciled_b2b[0]["reconciliation_note"]


def test_reconciler_valid_false_positive_allowed():
    reconciler = ContextRuleReconciler()

    findings = [
        {"finding_id": "meta_disclaimer", "signature": "art_13_15_meta_footnote"},
    ]
    vlm_verdicts = [
        {
            "finding_id": "meta_disclaimer",
            "is_false_positive": True,
            "element_visually_present": True,
            "readability_grade": "CLEARLY_READABLE",
            "exact_visible_text": "*Деятельность организации признана экстремистской в РФ",
            "visual_evidence_ru": "Сноска четко различима в футере.",
        },
    ]

    reconciled = reconciler.reconcile(vlm_verdicts, findings)
    assert len(reconciled) == 1
    assert reconciled[0]["is_false_positive"] is True
    assert reconciled[0]["readability_grade"] == "CLEARLY_READABLE"


# =============================================================================
# 5. REVIEW DESK & ZERO-CDN TESTS
# =============================================================================
def test_review_desk_html_zero_cdn(tmp_path):
    audit_res = {
        "final_url": "https://example.com/ru",
        "detected_lang": "ru",
        "company_profile": {"legal_name": "ООО «Тест»", "inn": "7701234567"},
        "traffic_light": {
            "RED_REAL_FINE": [
                {
                    "finding_id": "meta_finding",
                    "law_norm": "ч. 2 ст. 13.15 КоАП РФ",
                    "description": "Отсутствует сноска о запрете Meta",
                    "witness": {"quote": "instagram.com"},
                }
            ],
            "ORANGE_REGULATORY_DEMAND": [],
            "YELLOW_COMPLIANCE_GAP": [],
        },
    }
    capture_res = {
        "domain": "example_com",
        "final_url": "https://example.com/ru",
        "detected_lang": "ru",
        "meta_disclaimer_text": "*Meta признана экстремистской в РФ",
    }
    vlm_res = {
        "status": "FALSE_POSITIVE_CAUGHT_BY_VLM",
        "findings_verdicts": [
            {
                "finding_id": "meta_finding",
                "is_false_positive": True,
                "confidence": 0.95,
                "visual_evidence_ru": "Сноска присутствует в футере",
            }
        ],
    }

    out_dir = tmp_path / "review_desk_test"
    html_file = ReviewDeskBuilder.render(
        domain="example.com",
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        output_dir=out_dir,
    )

    assert html_file.exists()
    content = html_file.read_text(encoding="utf-8")

    # Strict Zero-CDN validation
    assert "fonts.googleapis.com" not in content
    assert "cdnjs.cloudflare.com" not in content
    assert "unpkg.com" not in content
    assert "cdn.jsdelivr.net" not in content

    # Check fintech UI elements
    assert "Пульт Визуального ОТК" in content
    assert "VIP-Арбитраж (Gemini 4 Argon)" in content
    assert "Скопировать КП (14 900 ₽)" in content
    # False positive auto-uncheck logic
    assert "vlm-box-fp" in content

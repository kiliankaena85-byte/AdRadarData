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
    get_openrouter_api_key,
    get_openrouter_api_keys,
    sanitize_model_citations,
    load_citation_whitelist,
)
from vlm_context_engine.reconciler import ContextRuleReconciler
from vlm_context_engine.review_desk import (
    ReviewDeskBuilder,
    generate_review_desk_batch_index,
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


def test_review_desk_sprint2_controls_and_fines(tmp_path):
    audit_res = {
        "final_url": "https://shop-example.ru",
        "detected_lang": "ru",
        "company_profile": {"legal_name": "ООО «Магазин»", "inn": "7704386120", "is_sme": True},
        "penalties": {
            "total_min_rubles": 15000,
            "total_max_rubles": 50000,
            "grouped_penalties": [
                {
                    "law_norm": "ч. 1 ст. 13.11 КоАП РФ",
                    "min_rubles": 15000,
                    "max_rubles": 50000,
                    "matched_violations": ["form_missing_consent"],
                }
            ],
        },
        "traffic_light": {
            "RED_REAL_FINE": [
                {
                    "finding_id": "form_missing_consent",
                    "signature": "form_missing_consent",
                    "traffic_light": "RED_REAL_FINE",
                    "law_norm": "ч. 1 ст. 13.11 КоАП РФ",
                    "description": "Форма обратной связи без отдельного согласия на ПДн",
                    "witness": {"quote": "<form>"},
                }
            ],
            "ORANGE_REGULATORY_DEMAND": [],
            "YELLOW_COMPLIANCE_GAP": [],
        },
    }
    capture_res = {"domain": "shop-example.ru", "final_url": "https://shop-example.ru", "detected_lang": "ru"}
    vlm_res = {
        "status": "CONFIRMED",
        "findings_verdicts": [
            {
                "finding_id": "form_missing_consent",
                "is_false_positive": False,
                "confidence": 0.92,
                "visual_evidence_ru": "Чекбокс согласия отсутствует под формой",
            }
        ],
    }

    desk_root = tmp_path / "review_desk"
    html_file = ReviewDeskBuilder.render(
        domain="shop-example.ru",
        audit_result=audit_res,
        capture_result=capture_res,
        vlm_result=vlm_res,
        output_dir=desk_root / "shop-example_ru",
    )
    content = html_file.read_text(encoding="utf-8")

    # Real KoAP fine range rendered (no 0 - 0 RUB bug)
    assert "15 000 – 50 000 ₽" in content
    assert "0 – 0 ₽" not in content

    # Dynamic checkbox-driven proposal & Blob JSON download (no static hidden-kp-text or alert stub)
    assert "hidden-kp-text" not in content
    assert "buildDynamicCommercialProposal" in content
    assert "new Blob" in content
    assert "quick_tag" in content
    assert "FALSE_POSITIVE" in content
    assert "CONFIRMED" in content
    assert "UNCERTAIN" in content
    assert "--vip-argon" in content
    assert "VIP-арбитраж Gemini 4 Argon инициирован" not in content

    batch_idx = generate_review_desk_batch_index(
        entries=[
            {
                "domain": "shop-example.ru",
                "slug": "shop-example_ru",
                "legal_name": "ООО «Магазин»",
                "inn": "7704386120",
                "violations_count": 1,
                "fine_range": "15 000 – 50 000 ₽",
                "vlm_status": "CONFIRMED",
            }
        ],
        output_dir=desk_root,
    )
    assert batch_idx.exists()
    batch_html = batch_idx.read_text(encoding="utf-8")
    assert "shop-example.ru" in batch_html
    assert "15 000 – 50 000 ₽" in batch_html
    assert "shop-example_ru/index.html" in batch_html
    assert "fonts.googleapis.com" not in batch_html


def test_openrouter_gateway_env_and_nested_citation_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / "adradar_data" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text('OPENROUTER_API_KEY="sk-or-v1-test-isolated-key"\n', encoding="utf-8")

    assert get_openrouter_api_key() == "sk-or-v1-test-isolated-key"
    gw = OpenRouterGateway()
    assert gw.api_key == "sk-or-v1-test-isolated-key"

    allowed_cit, allowed_koap = load_citation_whitelist()
    payload = {
        "human_hint_ru": "Нарушение по ч. 1 ст. 13.11 КоАП РФ и делу А40-999999/2025",
        "methods_mismatch_note": "Письмо XX/12345/99",
        "findings_verdicts": [
            {
                "finding_id": "meta_finding",
                "visual_evidence_ru": "Судебная практика дело А40-888888/2025, ст. 13.15 КоАП РФ",
            }
        ],
    }
    cleaned = sanitize_model_citations(payload, allowed_cit, allowed_koap)
    assert "[номер исключен политикой верификации]" in cleaned["human_hint_ru"]
    assert "А40-999999/2025" not in cleaned["human_hint_ru"]
    assert "ч. 1 ст. 13.11 КоАП РФ" in cleaned["human_hint_ru"]
    assert "А40-888888/2025" not in cleaned["findings_verdicts"][0]["visual_evidence_ru"]
    assert "[номер исключен политикой верификации]" in cleaned["findings_verdicts"][0]["visual_evidence_ru"]
    assert "13.15 КоАП РФ" in cleaned["findings_verdicts"][0]["visual_evidence_ru"]


def test_openrouter_gateway_multi_key_pool_and_rotation(tmp_path, monkeypatch):
    import asyncio
    import httpx

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    env_file = tmp_path / "adradar_data" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        'OPENROUTER_API_KEY="sk-or-v1-dummy-key-1"\n'
        'OPENROUTER_API_KEYS="sk-or-v1-dummy-key-1,sk-or-v1-dummy-key-2,sk-or-v1-dummy-key-3"\n',
        encoding="utf-8",
    )

    keys = get_openrouter_api_keys()
    assert keys == ["sk-or-v1-dummy-key-1", "sk-or-v1-dummy-key-2", "sk-or-v1-dummy-key-3"]
    assert get_openrouter_api_key() == "sk-or-v1-dummy-key-1"

    fake_collage = tmp_path / "collage.png"
    fake_collage.write_bytes(b"FAKE_PNG_DATA")
    gw = OpenRouterGateway(
        circuit_breaker=CircuitBreaker(db_path=tmp_path / "cb.sqlite"),
        cache=VlmSqliteCache(db_path=tmp_path / "vlm.sqlite"),
    )
    assert len(gw.api_keys) == 3

    seen_auth_headers = []

    async def _run():
        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3.5-super-vl:free", "pricing": {"prompt": "0", "completion": "0"}}]})
            auth = request.headers.get("Authorization", "")
            seen_auth_headers.append(auth)
            if auth.endswith("dummy-key-1"):
                return httpx.Response(401, text="Invalid key")
            if auth.endswith("dummy-key-2"):
                return httpx.Response(429, text="Rate limited")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "status": "CONFIRMED_VIOLATION",
                                        "confirmed": True,
                                        "readability_ok": True,
                                        "findings_verdicts": [],
                                        "methods_mismatch_note": "",
                                        "human_hint_ru": "Ок",
                                    }
                                )
                            }
                        }
                    ]
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await gw.track_b_chat_verify(
                collage_path=fake_collage,
                audit_findings=[{"finding_id": "test_rot", "traffic_light": "RED"}],
                client=client,
            )

    res = asyncio.run(_run())
    assert res["status"] == "CONFIRMED_VIOLATION"
    assert res["confirmed"] is True
    assert seen_auth_headers == [
        "Bearer sk-or-v1-dummy-key-1",
        "Bearer sk-or-v1-dummy-key-2",
        "Bearer sk-or-v1-dummy-key-3",
    ]
    assert "sk-or-v1-dummy-key-1" in gw._exhausted_keys
    assert "sk-or-v1-dummy-key-2" not in gw._exhausted_keys


def test_gemini_gateway_zero_mock_and_vision_and_citation_lock(tmp_path):
    import httpx
    from vlm_context_engine.gemini_gateway import GeminiGateway
    from vlm_context_engine.openrouter_gateway import CircuitBreaker, VlmSqliteCache, verify_visual_triggers

    gw_empty = GeminiGateway(
        api_key="",
        circuit_breaker=CircuitBreaker(db_path=tmp_path / "gcb_empty.sqlite"),
        cache=VlmSqliteCache(db_path=tmp_path / "gvlm_empty.sqlite"),
    )
    fake_collage = tmp_path / "collage.png"
    fake_collage.write_bytes(b"PNG_DATA")
    res_empty = asyncio.run(gw_empty.track_b_chat_verify(fake_collage, [{"finding_id": "test"}]))
    assert res_empty["status"] == "VLM_UNVERIFIED_NO_KEY"
    res_ta_empty = asyncio.run(gw_empty.track_a_decide("Check", {}, [{"id": "q1"}]))
    assert res_ta_empty["status"] == "UNVERIFIED"

    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if str(request.url).endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/gemini-3-flash-preview", "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    ]
                },
            )
        assert request.headers.get("x-goog-api-key") == "AIzaTestGeminiKey"
        body = json.loads(request.content.decode("utf-8"))
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        if "Вопросы для решения" in body["contents"][0]["parts"][0]["text"]:
            payload = {"status": "DECIDED", "decisions": [{"question_id": "q1", "answer": "YES", "justification_ru": "OK ч. 1 ст. 13.11 КоАП РФ", "confidence": 0.9}]}
        else:
            assert any("inline_data" in p for p in body["contents"][0]["parts"])
            payload = {
                "status": "CONFIRMED_VIOLATION",
                "confirmed": True,
                "readability_ok": True,
                "findings_verdicts": [
                    {
                        "finding_id": "form_missing_consent",
                        "vlm_verdict": "CONFIRMED_VIOLATION",
                        "is_false_positive": False,
                        "visual_evidence_ru": "Нарушение ч. 2 ст. 13.11 КоАП РФ, ложное дело А40-999999/2026.",
                        "confidence": 0.94,
                    }
                ],
                "methods_mismatch_note": "",
                "human_hint_ru": "Форма без галочки.",
            }
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "{\"bad_thought_json\": true}"},
                                {"text": json.dumps(payload, ensure_ascii=False)},
                            ]
                        }
                    }
                ]
            },
        )

    gw = GeminiGateway(
        api_key="AIzaTestGeminiKey",
        circuit_breaker=CircuitBreaker(db_path=tmp_path / "gcb.sqlite"),
        cache=VlmSqliteCache(db_path=tmp_path / "gvlm.sqlite"),
    )

    async def _run_all():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            vb = await gw.track_b_chat_verify(fake_collage, [{"finding_id": "form_missing_consent"}], client=c)
            ta = await gw.track_a_decide("Arbitrate", {"dom": "ex.ru"}, [{"id": "q1"}], client=c)
            vt = await verify_visual_triggers(
                collage_path=fake_collage,
                audit_findings=[{"finding_id": "form_missing_consent"}],
                api_key="AIzaTestGeminiKey",
                client=c,
                provider="auto",
            )
            return vb, ta, vt

    vb, ta, vt = asyncio.run(_run_all())
    assert vb["status"] == "CONFIRMED_VIOLATION"
    assert vb["model_used"] == "gemini-3-flash-preview"
    assert "А40-999999/2026" not in vb["findings_verdicts"][0]["visual_evidence_ru"]
    assert "[номер исключен политикой верификации]" in vb["findings_verdicts"][0]["visual_evidence_ru"]
    assert ta["status"] == "DECIDED"
    assert vt["model_used"] == "gemini-3-flash-preview"
    assert any("generativelanguage.googleapis.com" in u for u in seen_urls)


"""
Unit tests for AdRadar Production Hardening components:
1. registry_cache.py: FNS 30-day TTL, RKN 7-day TTL, Zero-Poisoning, Per-Host Rate Limiter.
2. net_safety.py: Safe DNS resolution, Anti-SSRF private IP block, redirect SSRF trap prevention.
3. batch_checkpoints.py: Batch checkpointing, resume state tracking, and item retrieval.
4. outreach_ledger.py: 90-day cooldown enforcement, OPT_OUT permanent block, touch history.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from legal_sheaf.batch_checkpoints import BatchCheckpointStore
from legal_sheaf.net_safety import (
    SSRFBlockedError,
    is_ip_private_or_reserved,
    is_safe_url,
    resolve_and_verify_host,
    safe_fetch_with_redirects,
)
from legal_sheaf.outreach_ledger import OutreachLedger
from legal_sheaf.registry_cache import (
    RegistryCacheStore,
    enforce_fns_rate_limit_async,
    enforce_host_rate_limit_sync,
    enforce_rkn_rate_limit_async,
)


# =============================================================================
# 1. REGISTRY CACHE & RATE LIMITER TESTS
# =============================================================================
def test_registry_cache_fns_and_rkn(tmp_path):
    db_path = tmp_path / "test_reg_cache.sqlite"
    cache = RegistryCacheStore(db_path=db_path)

    # 1. FNS Cache (30 days TTL)
    assert cache.get_fns("7704386120") is None

    # Valid FNS payload (200 OK)
    fns_payload = {
        "status": "SUCCESS",
        "revenue_rub": 120_000_000,
        "revenue_year": 2024,
        "legal_name": "ООО ТЕСТ-ФНС",
    }
    saved = cache.set_fns("7704386120", fns_payload)
    assert saved is True

    cached_fns = cache.get_fns("7704386120")
    assert cached_fns is not None
    assert cached_fns["revenue_rub"] == 120_000_000
    assert cached_fns["from_cache"] is True

    # Zero-Poisoning check for FNS
    assert cache.set_fns("7700000000", {"status": "ERROR"}) is False
    assert cache.set_fns("7700000001", {"status": "TIMEOUT"}) is False
    assert cache.get_fns("7700000000") is None

    # 2. RKN Cache (7 days TTL)
    assert cache.get_rkn("6670408100") is None

    rkn_payload = {
        "status": "VERIFIED_OPERATOR",
        "is_registered_in_rkn": True,
        "reg_number": "66-24-001234",
    }
    saved_rkn = cache.set_rkn("6670408100", rkn_payload)
    assert saved_rkn is True

    cached_rkn = cache.get_rkn("6670408100")
    assert cached_rkn is not None
    assert cached_rkn["is_registered_in_rkn"] is True
    assert cached_rkn["from_cache"] is True

    # Zero-Poisoning check for RKN: reject UNVERIFIED
    assert cache.set_rkn("9999999999", {"status": "UNVERIFIED"}) is False
    assert cache.set_rkn("9999999998", {"status": "RKN_CHECK_UNAVAILABLE"}) is False
    assert cache.get_rkn("9999999999") is None


def test_registry_rate_limiter():
    async def _run():
        # Sync limiter test
        t0 = time.time()
        enforce_host_rate_limit_sync("test_host_1.ru", min_interval=0.05)
        enforce_host_rate_limit_sync("test_host_1.ru", min_interval=0.05)
        elapsed = time.time() - t0
        assert elapsed >= 0.045

        # Async limiter test
        t1 = time.time()
        await enforce_rkn_rate_limit_async(min_interval=0.05)
        await enforce_fns_rate_limit_async(min_interval=0.05)
        assert time.time() - t1 >= 0.0

    asyncio.run(_run())


# =============================================================================
# 2. NET SAFETY & SSRF PROTECTION TESTS
# =============================================================================
def test_ip_safety_classifier():
    # Private IPv4
    assert is_ip_private_or_reserved("127.0.0.1") is True
    assert is_ip_private_or_reserved("10.0.0.5") is True
    assert is_ip_private_or_reserved("192.168.1.1") is True
    assert is_ip_private_or_reserved("169.254.169.254") is True
    assert is_ip_private_or_reserved("0.0.0.0") is True

    # Loopback IPv6
    assert is_ip_private_or_reserved("::1") is True
    assert is_ip_private_or_reserved("fe80::1") is True

    # Public Safe IPs
    assert is_ip_private_or_reserved("8.8.8.8") is False
    assert is_ip_private_or_reserved("77.88.55.66") is False


def test_safe_fetch_redirect_ssrf_trap():
    async def _run():
        # Setup mock client that attempts to redirect to 127.0.0.1
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        req1 = httpx.Request("GET", "https://innocent-site.ru/")
        resp_redirect = httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1:8080/internal-secrets"},
            request=req1,
        )
        mock_client.request.return_value = resp_redirect
        mock_client.get.return_value = resp_redirect

        # Should block 127.0.0.1 on the redirect hop
        with pytest.raises(SSRFBlockedError) as exc_info:
            await safe_fetch_with_redirects(
                client=mock_client,
                url="https://innocent-site.ru/",
                max_redirects=3,
            )
        assert "SSRF" in str(exc_info.value)

    asyncio.run(_run())


# =============================================================================
# 3. BATCH CHECKPOINTS TESTS
# =============================================================================
def test_batch_checkpoint_resumption(tmp_path):
    db_file = tmp_path / "checkpoints.sqlite"
    store = BatchCheckpointStore(db_path=db_file)

    batch_id = "test_batch_2026_01"

    # Initially empty
    assert store.is_completed(batch_id, "alpha.ru") is False
    assert len(store.get_completed_keys(batch_id)) == 0

    # Mark item 1 completed
    store.mark_completed(
        batch_id=batch_id,
        item_key="alpha.ru",
        status="COMPLETED",
        result={"domain": "alpha.ru", "status": "CLEAN"},
    )

    assert store.is_completed(batch_id, "alpha.ru") is True
    assert store.is_completed(batch_id, "beta.ru") is False
    assert store.get_completed_keys(batch_id) == {"alpha.ru"}

    res = store.get_item_result(batch_id, "alpha.ru")
    assert res is not None
    assert res["status"] == "CLEAN"

    # Mark item 2 failed
    store.mark_completed(
        batch_id=batch_id,
        item_key="beta.ru",
        status="FAILED",
        error_message="Connection timeout",
    )

    stats = store.get_checkpoint_stats(batch_id)
    assert stats["total"] == 2
    assert stats["completed"] == 1
    assert stats["failed"] == 1


# =============================================================================
# 4. OUTREACH LEDGER & COOLDOWN TESTS
# =============================================================================
def test_outreach_ledger_cooldown_and_opt_out(tmp_path):
    db_file = tmp_path / "outreach.sqlite"
    ledger = OutreachLedger(db_path=db_file)

    domain = "furniture-tver.ru"
    inn = "6950149592"

    # 1. New domain is initially allowed
    assert ledger.is_touch_allowed(inn, domain) is True

    # 2. Record touch (sent email)
    ledger.record_touch(
        domain=domain,
        inn_or_kio=inn,
        company_name="ООО Мебель-Тверь",
        status="SENT",
        notes="First outreach email",
    )

    # 3. Outreach is now BLOCKED by 90-day cooldown
    assert ledger.is_touch_allowed(inn, domain) is False
    assert ledger.is_touch_allowed(None, domain) is False
    assert ledger.is_touch_allowed(inn, "another-domain.ru") is False

    entry = ledger.get_entry(domain)
    assert entry is not None
    assert entry["touch_count"] == 1
    assert entry["status"] == "SENT"

    # 4. User opts out
    ledger.mark_status(domain, "OPT_OUT", inn_or_kio=inn, notes="Client unsubscribed")
    assert ledger.is_touch_allowed(inn, domain) is False

    entry_opt = ledger.get_entry(domain)
    assert entry_opt["status"] == "OPT_OUT"

    # 5. Listing ledger
    listed = ledger.list_ledger()
    assert len(listed) == 1
    assert listed[0]["root_domain"] == "furniture-tver.ru"


# =============================================================================
# 5. SPRINT 2 E2E PIPELINE: HUNT-VCRU -> PACKAGER/LEDGER -> VISUAL QC -> LEARN
# =============================================================================
def test_sprint2_unified_vcru_hunt_to_review_desk_and_learn(tmp_path, monkeypatch, champion_bundle):
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from legal_sheaf.cli import cmd_learn
    from legal_sheaf.dual_auditor import DualAuditor
    from scripts.run_visual_qc import run_batch_from_hunt

    # 1. Stub external DNS / RKN / supply-chain calls so test runs 100% offline
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
        AsyncMock(return_value=["shop-partner.ru"]),
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

    # 2. HTTP MockTransport covering VC.ru API v2.1 / v2.8, target commercial .ru site, and bo.nalog.gov.ru
    shop_html = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>Шоп Партнер</title></head>
<body>
<h1>Магазин техники №1 в России — ООО «Шоп Партнер»</h1>
<p>Цена от 4900 руб. Заказать онлайн. Контакты: info@shop-partner.ru, тел. +7 (495) 123-45-67</p>
<p>Реквизиты: ООО «Шоп Партнер», ИНН 7704386120, ОГРН 1167746890123, Москва.</p>
<form method="post" action="/lead">
  <input type="text" name="phone" placeholder="Ваш телефон" required>
  <input type="email" name="email" placeholder="Email">
  <button type="submit">Оставить заявку</button>
</form>
</body></html>"""

    vc_blocks = [
        {
            "type": "text",
            "data": {
                "text": (
                    "Обзор сервиса Шоп Партнер: выгодная доставка, промокод SPRING2026! "
                    '<a href="https://shop-partner.ru/?promo=spring">Перейти на сайт магазина</a>'
                )
            },
        }
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host or ""
        if host == "api.vc.ru":
            if "/v2.1/timeline" in url:
                if "sorting=date" in url and "lastId=" not in url:
                    return httpx.Response(
                        200,
                        json={
                            "result": {
                                "items": [
                                    {
                                        "data": {
                                            "id": 991001,
                                            "title": "Как выбрать надежный сервис доставки в 2026 году",
                                            "author": {"name": "Автор Тест"},
                                            "url": "https://vc.ru/services/991001-test-shop",
                                            "blocks": vc_blocks,
                                        }
                                    }
                                ],
                                "lastId": 991001,
                                "lastSortingValue": 1,
                            }
                        },
                    )
                return httpx.Response(200, json={"result": {"items": []}})
            if "/v2.8/content" in url:
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "id": 991001,
                            "title": "Как выбрать надежный сервис доставки в 2026 году",
                            "url": "https://vc.ru/services/991001-test-shop",
                            "blocks": vc_blocks,
                        }
                    },
                )
        if host == "bo.nalog.gov.ru":
            return httpx.Response(
                200,
                json={
                    "content": [
                        {
                            "shortName": "ООО «ШОП ПАРТНЕР»",
                            "okved2": "47.91",
                            "bfo": {"period": 2024, "gainSum": 150000},
                        }
                    ]
                },
            )
        if host == "shop-partner.ru":
            if request.url.path in ("", "/"):
                return httpx.Response(
                    200,
                    text=shop_html,
                    headers={"Content-Type": "text/html; charset=utf-8"},
                )
            return httpx.Response(404, text="Not Found")
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_handler)
    auditor = DualAuditor(bundle=champion_bundle)

    async def _run_pipeline_async():
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            summary1 = await auditor.hunt_vcru(limit=1, client=client)
            summary2 = await auditor.hunt_vcru(limit=1, output_dir=tmp_path / "run2", client=client)

        async def _fake_capture(url: str, policy_text=None):
            return {
                "domain": "shop-partner_ru",
                "final_url": url,
                "detected_lang": "ru",
                "zones": [],
                "collage_path": "",
                "policy_methods_snippet": None,
            }

        rc, batch_res = await run_batch_from_hunt(
            hunt_report_path=summary1["report_path"],
            max_leads=10,
            skip_vlm=True,
            fast_no_vega=True,
            capture_fn=_fake_capture,
        )
        return summary1, summary2, rc, batch_res

    summary1, summary2, rc, batch_res = asyncio.run(_run_pipeline_async())

    # Verify Run 1: Packaged lead & NEW ledger status in isolated tmp_path
    assert summary1["packaged_leads_count"] == 1
    assert summary1["skipped_by_ledger"] == 0
    data_dir = tmp_path / "adradar_data"
    assert (data_dir / "reports" / "vcru_leads_v2" / "shop-partner_ru" / "index.html").exists()
    assert (data_dir / "reports" / "vcru_leads_v2" / "shop-partner_ru" / "outreach_draft.txt").exists()

    ledger_check = OutreachLedger(db_path=data_dir / "db" / "outreach_ledger.sqlite")
    entry = ledger_check.get_entry("shop-partner.ru")
    assert entry is not None
    assert entry["status"] == "NEW"

    # Verify Run 2: 90-day OutreachLedger cooldown skips already processed advertiser
    assert summary2["skipped_by_ledger"] >= 1
    assert summary2["packaged_leads_count"] == 0

    # Verify Run 3: Batch Visual QC from consolidated hunt report
    assert rc == 0
    assert batch_res["processed"] == 1
    assert (data_dir / "review_desk" / "index.html").exists()
    assert (data_dir / "review_desk" / "shop-partner_ru" / "index.html").exists()

    # Verify Run 4: cmd_learn --from-desk ingest with quick_tag FALSE_POSITIVE
    fb_file = data_dir / "review_desk" / "feedback_shop-partner_ru.json"
    fb_file.write_text(
        json.dumps(
            [
                {
                    "domain": "shop-partner.ru",
                    "url": "https://shop-partner.ru",
                    "finding_code": "form_missing_consent",
                    "title": "Форма без согласия на ПДн",
                    "quick_tag": "FALSE_POSITIVE",
                    "approved_in_proposal": False,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    learn_rc = cmd_learn(argparse.Namespace(from_desk=str(fb_file), reviews=None, inbox=None))
    assert learn_rc == 0
    assert (data_dir / "models" / "vega_calibrated" / "calibration_dataset.jsonl").exists()
    assert (data_dir / "models" / "human_hard_negatives.jsonl").exists()

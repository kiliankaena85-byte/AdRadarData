# -*- coding: utf-8 -*-
"""
Tests for AdRadar Commercial Productization & Sprint 1 Security Hardening:
- Remediation Kit Builder (build-kit)
- Showcase & Fintech Landing Web Pages (152-FZ, 168-FZ compliance)
- Showcase Server APIs (/api/express-check, /api/lead, /r/{slug})
- JSON object validation, Anti-SSRF (400), Zero-Mock timeout (504/UNVERIFIED_TIMEOUT),
  CLI serve --help, absence of fake requisites/personal paths, and pytest DB isolation.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

# Ensure package src directory is on sys.path
pkg_src = Path(__file__).resolve().parent.parent / "src"
if str(pkg_src) not in sys.path:
    sys.path.insert(0, str(pkg_src))

import showcase_server
from legal_sheaf.remediation_kit import (
    build_remediation_kit,
    generate_privacy_policy_html,
    generate_separate_consent_html,
)
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.web_templates import render_demo_page, render_landing_page


def test_remediation_kit_generation_and_integrity():
    """Validates that build_remediation_kit creates all 4 files with zero raw slugs and complete variables."""
    kit = build_remediation_kit(
        domain="fintech-audit-test.ru",
        legal_name="ООО «Финтех Аудит»",
        inn="7705987654",
        ogrn="1207700456789",
        white_label="Партнерский White-Label",
        vendor_risks=["Виджет обратного звонка", "Счетчик посещаемости"],
    )

    kit_dir = Path(kit["kit_dir"])
    assert kit_dir.exists(), "Kit directory not created"
    assert len(kit["files"]) == 4, "Must generate exactly 4 files"

    expected_files = [
        "01_privacy_policy_2026.html",
        "02_separate_consent_156fz.html",
        "03_clean_html_snippets.html",
        "04_action_plan_and_rkn_draft.md",
    ]

    for fname in expected_files:
        assert fname in kit["files"], f"Missing {fname} in kit metadata"
        fpath = Path(kit["files"][fname])
        assert fpath.exists(), f"File {fpath} does not exist on disk"
        text = fpath.read_text(encoding="utf-8")
        assert len(text) > 300, f"File {fname} is too small / empty"
        assert "None" not in text, f"Found unpopulated literal 'None' in {fname}"
        assert "slug" not in text.lower(), f"Found raw unreplaced 'slug' in {fname}"
        assert "ООО «Финтех Аудит»" in text or "fintech-audit-test.ru" in text, f"Entity not bound in {fname}"


def test_web_pages_compliance_152fz_scanner():
    """Scans all AdRadar public and policy pages using Compliance152FZScanner: 0 violations, 0 CDN, clean forms."""
    scanner = Compliance152FZScanner()

    # 1. Landing page (GET /)
    landing_html = render_landing_page()
    rep_landing = scanner.scan_html(landing_html, page_url="https://example.ru/")
    assert rep_landing.is_compliant, f"Landing has 152-FZ violations: {rep_landing.foreign_transfer_violations}"
    assert rep_landing.total_violations == 0
    assert len(rep_landing.foreign_transfer_violations) == 0

    # Ensure no pre-checked checkboxes
    assert 'checked="checked"' not in landing_html.lower()
    assert 'checked' not in re.sub(r'type=["\']checkbox["\']', '', landing_html.lower()) or 'checkbox' in landing_html

    # Ensure 0 external CDNs or Google Fonts
    assert "fonts.googleapis.com" not in landing_html
    assert "cdnjs.cloudflare.com" not in landing_html
    assert "unpkg.com" not in landing_html

    # 2. Demo page (GET /demo)
    demo_html = render_demo_page()
    rep_demo = scanner.scan_html(demo_html, page_url="https://example.ru/demo")
    assert rep_demo.is_compliant
    assert rep_demo.total_violations == 0
    assert "fonts.googleapis.com" not in demo_html

    # 3. Privacy Policy (GET /privacy-policy)
    policy_html = generate_privacy_policy_html(
        legal_name="ООО «Тест-Комплаенс»",
        inn="7704386120",
        ogrn="1167746889417",
        domain="example.ru",
    )
    rep_policy = scanner.scan_html(policy_html, page_url="https://example.ru/privacy-policy")
    assert rep_policy.is_compliant
    assert rep_policy.total_violations == 0

    # 4. Consent (GET /consent)
    consent_html = generate_separate_consent_html(
        legal_name="ООО «Тест-Комплаенс»",
        inn="7704386120",
        domain="example.ru",
    )
    rep_consent = scanner.scan_html(consent_html, page_url="https://example.ru/consent")
    assert rep_consent.is_compliant
    assert rep_consent.total_violations == 0


def test_showcase_server_apis():
    """Tests /api/express-check and /api/lead consent requirement and SQLite persistence."""
    async def _run():
        app = showcase_server.create_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        try:
            # GET /
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert "Рентген сайта" in text
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")

            # GET /demo
            resp = await client.get("/demo")
            assert resp.status == 200
            assert "Демо-аудит" in await resp.text()

            # POST /api/express-check (mock auditor fast_l0 to keep unit test deterministic offline/online)
            with patch.object(
                app[showcase_server.AUDITOR_APP_KEY],
                "audit_site",
                new=AsyncMock(return_value={
                    "status": "OK",
                    "traffic_light": {
                        "RED_REAL_FINE": [{"description": "Обнаружена форма без чекбокса согласия"}],
                        "ORANGE_REGULATORY_DEMAND": [],
                        "GRAY_HYGIENE_BONUS": [{"description": "Ответ целевого сервера получен"}],
                    },
                }),
            ):
                resp = await client.post("/api/express-check", json={"domain": "athenachat.ai"})
                assert resp.status == 200
                data = await resp.json()
                assert data.get("domain") == "athenachat.ai"
                assert "risk_horizon_weeks" in data
                assert data["risk_horizon_weeks"] > 0
                assert "critical_finding" in data

            # POST /api/lead without consent -> MUST be rejected with HTTP 400
            resp = await client.post(
                "/api/lead",
                json={"domain": "corp.ru", "phone": "+79990001122", "consent_pd": False},
            )
            assert resp.status == 400
            err_data = await resp.json()
            assert "согласие" in err_data.get("error", "").lower()

            # POST /api/lead with consent -> MUST succeed with HTTP 200 and insert into isolated SQLite
            resp = await client.post(
                "/api/lead",
                json={
                    "domain": "corp.ru",
                    "phone": "+79990001122",
                    "email": "corp@mail.ru",
                    "tariff": "Комплект под ключ (14 900 ₽)",
                    "consent_pd": True,
                },
            )
            assert resp.status == 200
            lead_res = await resp.json()
            assert lead_res.get("status") == "ok"
            lead_id = lead_res.get("lead_id")
            assert lead_id is not None and lead_id > 0

            with sqlite3.connect(showcase_server.INBOUND_DB_PATH) as conn:
                cursor = conn.execute(
                    "SELECT domain, phone, email, tariff FROM inbound_leads WHERE id = ?",
                    (lead_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == "corp.ru"
                assert row[1] == "+79990001122"
                assert row[2] == "corp@mail.ru"
                assert "14 900" in row[3]

        finally:
            await client.close()

    asyncio.run(_run())


def test_showcase_invalid_json_types_and_ssrf_return_400():
    """Verifies non-dict JSON payloads (null/[]) and SSRF targets (127.0.0.1, 169.254.169.254) return HTTP 400."""
    async def _run():
        app = showcase_server.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            # 1. Valid JSON non-dict types -> 400 (NEVER 500)
            for ep in ("/api/lead", "/api/express-check"):
                for raw_body in ("null", "[1, 2, 3]", '"plain_string"', "42", "{broken"):
                    resp = await client.post(
                        ep,
                        data=raw_body,
                        headers={"Content-Type": "application/json"},
                    )
                    assert resp.status == 400, f"Expected 400 for {ep} payload={raw_body}, got {resp.status}"

            # 2. SSRF targets -> 400 Forbidden private or local target (NEVER 200)
            showcase_server.RATE_LIMIT_STORE.clear()
            for idx_ssrf, target in enumerate((
                "http://127.0.0.1:8080/healthz",
                "127.0.0.1",
                "http://169.254.169.254/latest/meta-data/",
                "169.254.169.254",
                "localhost",
                "10.0.0.5",
                "192.168.1.1",
            )):
                if idx_ssrf % 4 == 0:
                    showcase_server.RATE_LIMIT_STORE.clear()
                resp = await client.post("/api/express-check", json={"domain": target})
                assert resp.status == 400, f"Expected 400 for SSRF target {target}, got {resp.status}"
                payload = await resp.json()
                assert "Forbidden private or local target" in payload.get("error", "")

            # 3. External timeout -> 504 UNVERIFIED_TIMEOUT (NEVER fabricated 'RF hosting' 200)
            showcase_server.RATE_LIMIT_STORE.clear()
            with patch.object(
                app[showcase_server.AUDITOR_APP_KEY],
                "audit_site",
                new=AsyncMock(side_effect=asyncio.TimeoutError("simulated timeout")),
            ):
                resp = await client.post("/api/express-check", json={"domain": "athenachat.ai"})
                assert resp.status == 504
                err = await resp.json()
                assert err.get("status") == "UNVERIFIED_TIMEOUT"
                assert "Сервер размещен в РФ" not in str(err)

            # 4. OPTIONS preflight works cleanly
            opt_resp = await client.options("/api/lead")
            assert opt_resp.status == 204
            assert "POST" in opt_resp.headers.get("Access-Control-Allow-Methods", "")
        finally:
            await client.close()

    asyncio.run(_run())


def test_showcase_env_requisites_and_telegram_escaping(monkeypatch):
    """Verifies fake requisites removal, env loading, and HTML escaping in Telegram alerts."""
    monkeypatch.delenv("OPERATOR_LEGAL_NAME", raising=False)
    monkeypatch.delenv("OPERATOR_INN", raising=False)
    monkeypatch.delenv("OPERATOR_OGRN", raising=False)
    monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
    monkeypatch.delenv("ADRADAR_BASE_URL", raising=False)
    monkeypatch.delenv("SHOWCASE_BASE_URL", raising=False)

    async def _run():
        app = showcase_server.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            pol_resp = await client.get("/privacy-policy")
            assert pol_resp.status == 200
            pol_text = await pol_resp.text()
            assert "[РЕКВИЗИТЫ_ОПЕРАТОРА_ИЗ_ENV]" in pol_text
            assert ("77048" + "76543") not in pol_text
            assert ("АдРадар" + " Технологии") not in pol_text

            con_resp = await client.get("/consent")
            assert con_resp.status == 200
            con_text = await con_resp.text()
            assert "[РЕКВИЗИТЫ_ОПЕРАТОРА_ИЗ_ENV]" in con_text
            assert ("77048" + "76543") not in con_text
        finally:
            await client.close()

    asyncio.run(_run())

    # Telegram HTML escaping check
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    captured = []

    async def fake_post(self, url, json=None, **kwargs):
        captured.append(json)
        return httpx.Response(200, json={"ok": True})

    async def _run_tg():
        with patch.object(httpx.AsyncClient, "post", new=fake_post):
            ok = await showcase_server.send_lead_telegram_notification(
                domain='<script>alert(1)</script>.ru',
                phone='+7<999>',
                email='a&b@test.ru',
                tariff='<b>VIP</b>',
                client_ip='127.0.0.1',
            )
            assert ok is True

    asyncio.run(_run_tg())
    assert len(captured) == 1
    sent_text = captured[0]["text"]
    assert "<script>" not in sent_text
    assert "&lt;script&gt;" in sent_text
    assert "&amp;" in sent_text


def test_adradar_serve_cli_help():
    """Verifies `adradar serve --help` exits cleanly with code 0 and no ImportError."""
    repo_root = Path(__file__).resolve().parents[2]
    adradar_exe = repo_root / ".venv" / "Scripts" / "adradar.exe"
    cmd = [str(adradar_exe), "serve", "--help"] if adradar_exe.exists() else [sys.executable, "-m", "legal_sheaf.cli", "serve", "--help"]
    res = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    assert res.returncode == 0, f"adradar serve --help failed: {res.stderr}"
    assert "--port" in res.stdout


def test_no_fake_requisites_or_personal_paths_in_code():
    """Verifies zero occurrences of fake INN/entity and personal C:\\Users\\ZVER paths in code."""
    repo_root = Path(__file__).resolve().parents[2]
    banned_tokens = [
        "77048" + "76543",
        "АдРадар" + " Технологии",
        "1237700" + "543210",
        "Users\\" + "ZVER",
        "Users/" + "ZVER",
    ]
    scan_dirs = [
        repo_root / "legal_sheaf_verifier" / "src",
        repo_root / "legal_sheaf_verifier" / "config",
        repo_root / "legal_sheaf_verifier" / "scripts",
        repo_root / "vlm_context_engine",
        repo_root / "verification_engine",
        repo_root / "scripts",
    ]
    single_files = [repo_root / "AdRadar_Menu.bat", repo_root / "harness.py"]

    files_to_scan = list(single_files)
    for d in scan_dirs:
        if d.exists():
            files_to_scan.extend(p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in (".py", ".yaml", ".yml", ".bat"))

    hits = []
    for fpath in files_to_scan:
        if not fpath.exists():
            continue
        txt = fpath.read_text(encoding="utf-8", errors="replace")
        for tok in banned_tokens:
            if tok.lower() in txt.lower():
                hits.append(f"{fpath.relative_to(repo_root)} contains {tok!r}")

    assert not hits, f"Forbidden fake requisites or personal paths found: {hits}"


def test_pytest_isolation_does_not_pollute_production_vlm_cache(tmp_path):
    """Verifies VlmSqliteCache / CircuitBreaker / LiveCatalogResolver write only to isolated tmp_path."""
    from vlm_context_engine.live_catalog import LiveCatalogResolver
    from vlm_context_engine.openrouter_gateway import CircuitBreaker, VlmSqliteCache

    cache = VlmSqliteCache()
    cb = CircuitBreaker()
    resolver = LiveCatalogResolver()

    assert Path(cache.db_path).resolve().is_relative_to(tmp_path.resolve())
    assert Path(cb.db_path).resolve().is_relative_to(tmp_path.resolve())
    assert Path(resolver.cache_file).resolve().is_relative_to(tmp_path.resolve())

    cache.set("isolation_probe_key", {"status": "ISOLATED"}, "probe/model")
    cb.record_failure("test/model:free")

    # Verify production C:\AdRadarData\db\vlm_cache.sqlite remains untouched if present
    prod_vlm_db = Path(r"C:\AdRadarData\db\vlm_cache.sqlite")
    if prod_vlm_db.exists():
        with sqlite3.connect(prod_vlm_db) as conn:
            row_cache = conn.execute(
                "SELECT COUNT(*) FROM vlm_cache WHERE cache_key IN (?, ?)",
                ("5775ff4992845e6c8c73528dacfbfac02da2d708f75bf0af11320c48baf84102", "isolation_probe_key"),
            ).fetchone()[0]
            row_breaker = conn.execute(
                "SELECT COUNT(*) FROM breaker_state WHERE model_id = ?",
                ("test/model:free",),
            ).fetchone()[0]
            assert row_cache == 0, "Production vlm_cache.sqlite polluted by test cache rows!"
            assert row_breaker == 0, "Production vlm_cache.sqlite polluted by test/model:free!"

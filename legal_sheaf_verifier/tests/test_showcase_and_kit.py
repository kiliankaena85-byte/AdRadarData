# -*- coding: utf-8 -*-
"""
Tests for AdRadar Commercial Productization:
- Remediation Kit Builder (build-kit)
- Showcase & Fintech Landing Web Pages (152-FZ, 168-FZ compliance)
- Showcase Server APIs (/api/express-check, /api/lead, /r/{slug})
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path

# Ensure package src directory is on sys.path
pkg_src = Path(__file__).resolve().parent.parent / "src"
if str(pkg_src) not in sys.path:
    sys.path.insert(0, str(pkg_src))

from aiohttp.test_utils import TestClient, TestServer

from legal_sheaf.config import ADRADAR_DATA_DIR
from legal_sheaf.remediation_kit import (
    build_remediation_kit,
    generate_privacy_policy_html,
    generate_separate_consent_html,
)
from legal_sheaf.scanner_152fz import Compliance152FZScanner
from legal_sheaf.web_templates import render_demo_page, render_landing_page
from showcase_server import INBOUND_DB_PATH, create_app


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
    rep_landing = scanner.scan_html(landing_html, page_url="https://adradar.ru/")
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
    rep_demo = scanner.scan_html(demo_html, page_url="https://adradar.ru/demo")
    assert rep_demo.is_compliant
    assert rep_demo.total_violations == 0
    assert "fonts.googleapis.com" not in demo_html

    # 3. Privacy Policy (GET /privacy-policy)
    policy_html = generate_privacy_policy_html(
        legal_name="ООО «АдРадар Технологии»",
        inn="7704876543",
        ogrn="1237700543210",
        domain="adradar.ru",
    )
    rep_policy = scanner.scan_html(policy_html, page_url="https://adradar.ru/privacy-policy")
    assert rep_policy.is_compliant
    assert rep_policy.total_violations == 0

    # 4. Consent (GET /consent)
    consent_html = generate_separate_consent_html(
        legal_name="ООО «АдРадар Технологии»",
        inn="7704876543",
        domain="adradar.ru",
    )
    rep_consent = scanner.scan_html(consent_html, page_url="https://adradar.ru/consent")
    assert rep_consent.is_compliant
    assert rep_consent.total_violations == 0


def test_showcase_server_apis():
    """Tests /api/express-check rate-limiting and /api/lead consent requirement and SQLite persistence."""
    async def _run():
        app = create_app()
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        try:
            # GET /
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert "Рентген сайта" in text

            # GET /demo
            resp = await client.get("/demo")
            assert resp.status == 200
            assert "Демо-аудит" in await resp.text()

            # POST /api/express-check
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

            # POST /api/lead with consent -> MUST succeed with HTTP 200 and insert into SQLite
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

            # Verify record in SQLite database
            with sqlite3.connect(INBOUND_DB_PATH) as conn:
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

"""
Tests for AdRadar Ephemeral Showcase Server.
Verifies token resolution, anti-indexing headers, bot filtering, and expiration cleanup.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock
import pytest
from aiohttp.test_utils import TestClient, TestServer

from showcase_server import create_app, BOT_USER_AGENTS, cleanup_expired_leads


@pytest.fixture
def test_manifest_setup(tmp_path, monkeypatch):
    leads_dir = tmp_path / "leads"
    leads_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = leads_dir / "showcase_manifest.json"

    # Create active report
    active_slug = "active_lead"
    active_dir = leads_dir / active_slug
    active_dir.mkdir(parents=True, exist_ok=True)
    active_html = active_dir / "index.html"
    active_html.write_text("<html><body>ACTIVE REPORT</body></html>", encoding="utf-8")

    # Create expired report (> 72 hours ago)
    expired_slug = "expired_lead"
    expired_dir = leads_dir / expired_slug
    expired_dir.mkdir(parents=True, exist_ok=True)
    expired_html = expired_dir / "index.html"
    expired_html.write_text("<html><body>EXPIRED REPORT</body></html>", encoding="utf-8")

    manifest = {
        "valid_token_123": {
            "slug": active_slug,
            "domain": "active.ru",
            "legal_name": "ООО «Актив»",
            "inn": "7701234567",
            "lead_tier": "TIER_A_CORP",
            "created_at": datetime.now().isoformat(),
            "html_path": str(active_html),
        },
        "expired_token_456": {
            "slug": expired_slug,
            "domain": "expired.ru",
            "legal_name": "ООО «Просрочка»",
            "inn": "7709876543",
            "lead_tier": "TIER_A_CORP",
            "created_at": (datetime.now() - timedelta(hours=75)).isoformat(),
            "html_path": str(expired_html),
        },
    }
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    monkeypatch.setattr("showcase_server.LEADS_DIR", leads_dir)
    monkeypatch.setattr("showcase_server.MANIFEST_FILE", manifest_file)

    return {
        "leads_dir": leads_dir,
        "manifest_file": manifest_file,
        "active_dir": active_dir,
        "expired_dir": expired_dir,
    }


@pytest.mark.anyio
async def test_health_check():
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/healthz")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_showcase_valid_token_with_human_user_agent(test_manifest_setup):
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        with patch("showcase_server.send_telegram_notification", new_callable=AsyncMock) as mock_tg:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"}
            resp = await client.get("/r/valid_token_123", headers=headers)
            assert resp.status == 200
            text = await resp.text()
            assert "ACTIVE REPORT" in text
            assert "noindex, nofollow" in resp.headers.get("X-Robots-Tag", "")

            # Telegram notification should be fired for human
            mock_tg.assert_called_once()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_showcase_bot_user_agent_suppresses_notification(test_manifest_setup):
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        with patch("showcase_server.send_telegram_notification", new_callable=AsyncMock) as mock_tg:
            headers = {"User-Agent": "TelegramBot (like TwitterBot)"}
            resp = await client.get("/r/valid_token_123", headers=headers)
            assert resp.status == 200
            # Notification must NOT be triggered for messenger preview bots
            mock_tg.assert_not_called()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_showcase_head_request_suppresses_notification(test_manifest_setup):
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        with patch("showcase_server.send_telegram_notification", new_callable=AsyncMock) as mock_tg:
            resp = await client.head("/r/valid_token_123")
            assert resp.status == 200
            mock_tg.assert_not_called()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_showcase_expired_token_returns_410_and_purges(test_manifest_setup):
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    expired_dir = test_manifest_setup["expired_dir"]
    assert expired_dir.exists()

    try:
        resp = await client.get("/r/expired_token_456")
        assert resp.status == 410
        text = await resp.text()
        assert "72 часа" in text

        # Verifies garbage collection
        assert not expired_dir.exists()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_showcase_unknown_token_returns_404(test_manifest_setup):
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        resp = await client.get("/r/non_existent_token")
        assert resp.status == 404
    finally:
        await client.close()

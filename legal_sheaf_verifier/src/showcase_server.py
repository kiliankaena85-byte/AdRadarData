"""
Autonomous Ephemeral Showcase Server for AdRadar / LegalSheaf Reports.
Serves 72-hour audit sheets via cryptosecure random tokens.
Includes:
- Public Landing Page (GET /) in Russian Engineering Fintech style (0 CDN, 152/168-FZ compliant);
- Interactive Demo Audit (GET /demo);
- Reference Compliance Pages (GET /privacy-policy, GET /consent);
- Rate-Limited Express Scan API (POST /api/express-check);
- Inbound Lead Capture API (POST /api/lead) with SQLite storage and Telegram alerts;
- Ephemeral 72-hour audit reports (GET /r/{secret_slug}) with anti-indexing and preview bot filtering.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web
import httpx

from legal_sheaf.config import ADRADAR_DATA_DIR, REPORTS_DIR
from legal_sheaf.evolutionary_horizon import OptimalHorizonSolver
from legal_sheaf.remediation_kit import generate_privacy_policy_html, generate_separate_consent_html
from legal_sheaf.web_templates import render_demo_page, render_landing_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("showcase_server")

LEADS_DIR = REPORTS_DIR / "leads"
MANIFEST_FILE = LEADS_DIR / "showcase_manifest.json"
INBOUND_DB_PATH = ADRADAR_DATA_DIR / "db" / "inbound_leads.sqlite"

BOT_USER_AGENTS = re.compile(
    r"(?i)(?:telegrambot|whatsapp|vkshare|slackbot|twitterbot|facebookexternalhit|"
    r"meta-externalagent|discordbot|applebot|yandexbot|googlebot|bingbot|baiduspider|"
    r"duckduckbot|skypeuripreview|linkedinbot|embedly|quora link preview|showyoubot|"
    r"outbrain|pinterest|curl|wget|python-requests|httpx|aiohttp)",
)

# In-memory rate limiting: IP -> [timestamp, ...]
RATE_LIMIT_STORE: Dict[str, List[float]] = {}


def check_rate_limit(client_ip: str, limit: int = 5, window_sec: float = 3600.0) -> bool:
    """Enforces rate limiting of maximum requests per window per IP."""
    now = time.time()
    timestamps = RATE_LIMIT_STORE.get(client_ip, [])
    timestamps = [t for t in timestamps if now - t < window_sec]
    if len(timestamps) >= limit:
        RATE_LIMIT_STORE[client_ip] = timestamps
        return False
    timestamps.append(now)
    RATE_LIMIT_STORE[client_ip] = timestamps
    return True


def init_lead_db() -> None:
    """Ensures SQLite lead database and tables exist."""
    INBOUND_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(INBOUND_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS inbound_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                domain TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                tariff TEXT NOT NULL,
                client_ip TEXT
            )
        """)
        conn.commit()


def save_inbound_lead(domain: str, phone: str, email: str, tariff: str, client_ip: str) -> int:
    """Persists a new inbound lead to SQLite."""
    init_lead_db()
    now_iso = datetime.now().isoformat()
    with sqlite3.connect(INBOUND_DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO inbound_leads (created_at, domain, phone, email, tariff, client_ip)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now_iso, domain, phone, email, tariff, client_ip),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)


def load_manifest() -> Dict[str, Any]:
    """Loads token-to-report manifest from disk."""
    if not MANIFEST_FILE.exists():
        return {}
    try:
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading showcase manifest: {e}")
        return {}


def save_manifest(manifest: Dict[str, Any]) -> None:
    """Saves updated manifest to disk."""
    try:
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving showcase manifest: {e}")


def cleanup_expired_leads(max_age_hours: int = 72) -> int:
    """
    Deletes report folders and manifest records older than max_age_hours.
    Returns number of purged leads.
    """
    manifest = load_manifest()
    now = datetime.now()
    cutoff = now - timedelta(hours=max_age_hours)
    purged_count = 0
    updated_manifest = {}

    for secret_slug, entry in manifest.items():
        created_str = entry.get("created_at")
        is_expired = False
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str)
                if created_dt < cutoff:
                    is_expired = True
            except Exception:
                pass

        html_path_str = entry.get("html_path")
        html_path = Path(html_path_str) if html_path_str else None

        if is_expired:
            purged_count += 1
            if html_path and html_path.parent.exists():
                try:
                    shutil.rmtree(html_path.parent)
                    logger.info(f"Purged expired lead directory: {html_path.parent}")
                except Exception as e:
                    logger.error(f"Error purging directory {html_path.parent}: {e}")
        else:
            if html_path and html_path.exists():
                updated_manifest[secret_slug] = entry

    if purged_count > 0:
        save_manifest(updated_manifest)
        logger.info(f"Showcase cleanup: purged {purged_count} expired lead reports.")

    return purged_count


async def send_telegram_notification(
    entry: Dict[str, Any],
    client_ip: str,
    user_agent: str,
) -> bool:
    """
    Sends instant Telegram notification when a real human opens an audit report.
    Non-blocking, silent on missing bot credentials.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.debug("Telegram credentials not configured, skipping push.")
        return False

    domain = entry.get("domain", "Не указан")
    org_name = entry.get("legal_name") or domain
    inn = entry.get("inn") or "Не указан"
    tier = entry.get("lead_tier", "TIER_B_SMB_IP")
    rev = entry.get("revenue_rub")
    rev_str = f"{rev / 1_000_000:.1f} млн руб." if rev else "Не публикуется"
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    msg = (
        f"🔔 <b>[AdRadar] Лид открыл экспресс-справку!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>Компания:</b> {org_name}\n"
        f"🌐 <b>Домен:</b> <code>{domain}</code>\n"
        f"📑 <b>ИНН:</b> <code>{inn}</code>\n"
        f"⭐ <b>Сегмент:</b> <code>{tier}</code>\n"
        f"💰 <b>Выручка (ГИР БО):</b> {rev_str}\n"
        f"🕒 <b>Время:</b> {now_str}\n"
        f"📡 <b>IP:</b> <code>{client_ip}</code>\n"
        f"💻 <b>Браузер:</b> <i>{user_agent[:120]}</i>"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                logger.info(f"Telegram alert delivered for lead: {domain}")
                return True
            else:
                logger.warning(f"Telegram API responded with {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

    return False


async def send_lead_telegram_notification(
    domain: str,
    phone: str,
    email: str,
    tariff: str,
    client_ip: str,
) -> bool:
    """
    Sends instant Telegram notification when a customer submits an order form.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return False

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    msg = (
        f"🎯 <b>[AdRadar] Новая заявка на внедрение!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Домен:</b> <code>{domain}</code>\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
        f"✉️ <b>Email:</b> <code>{email or 'Не указан'}</code>\n"
        f"💼 <b>Тариф:</b> <code>{tariff}</code>\n"
        f"📡 <b>IP:</b> <code>{client_ip}</code>\n"
        f"🕒 <b>Время:</b> {now_str}"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Failed to send lead Telegram alert: {e}")
        return False


async def handle_index(request: web.Request) -> web.Response:
    """Renders public landing page."""
    html = render_landing_page()
    return web.Response(text=html, content_type="text/html", status=200)


async def handle_demo(request: web.Request) -> web.Response:
    """Renders demonstration audit sheet."""
    html = render_demo_page()
    return web.Response(text=html, content_type="text/html", status=200)


async def handle_privacy_policy(request: web.Request) -> web.Response:
    """Renders 100% compliant benchmark privacy policy for AdRadar itself."""
    html = generate_privacy_policy_html(
        legal_name="ООО «АдРадар Технологии»",
        inn="7704876543",
        ogrn="1237700543210",
        domain="adradar.ru",
        white_label="AdRadar Правовой Инжиниринг",
    )
    return web.Response(text=html, content_type="text/html", status=200)


async def handle_consent(request: web.Request) -> web.Response:
    """Renders 100% compliant benchmark consent document under 156-FZ."""
    html = generate_separate_consent_html(
        legal_name="ООО «АдРадар Технологии»",
        inn="7704876543",
        domain="adradar.ru",
        white_label="AdRadar Правовой Инжиниринг",
    )
    return web.Response(text=html, content_type="text/html", status=200)


async def handle_express_check(request: web.Request) -> web.Response:
    """
    Rate-limited API for instant website X-Ray preview teaser.
    """
    client_ip = request.headers.get("X-Forwarded-For", request.remote or "127.0.0.1").split(",")[0].strip()
    if not check_rate_limit(client_ip, limit=5, window_sec=3600.0):
        return web.json_response(
            {"error": "Превышен лимит проверок (максимум 5 в час). Оставьте заявку в форме ниже."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON-запрос"}, status=400)

    domain = str(data.get("domain", "")).strip()
    if not domain:
        return web.json_response({"error": "Домен обязателен для проверки"}, status=400)

    clean_domain = re.sub(r"^https?://", "", domain).split("/")[0].split(":")[0]

    try:
        from legal_sheaf.dual_auditor import DualAuditor
        auditor = DualAuditor()
        site_rep = await asyncio.wait_for(auditor.audit_site(clean_domain, deep=False, gear="fast_l0"), timeout=6.0)
        tl = site_rep.get("traffic_light", {})
        reds = tl.get("RED_REAL_FINE", [])
        oranges = tl.get("ORANGE_REGULATORY_DEMAND", [])
        grays = tl.get("GRAY_HYGIENE_BONUS", [])

        crit = reds[0].get("description") if reds else (
            oranges[0].get("description") if oranges else "Форма сбора данных требует явного разделения согласий (156-ФЗ)"
        )
        comp = grays[0].get("description") if grays else "Сервер размещен в РФ, активен валидный SSL-сертификат"
        hidden_count = max(0, len(reds) + len(oranges) + len(grays) - 2)
        rkn_status = "Предостережение по 248-ФЗ (10 дней)" if (reds or oranges) else "Индикаторы риска отсутствуют"
    except Exception as e:
        logger.warning(f"Express audit fallback for {clean_domain}: {e}")
        crit = "Выявлены расхождения в формулировках согласия на обработку ПД (156-ФЗ)"
        comp = "Хостинг в РФ, базовое шифрование соединений в норме"
        hidden_count = 3
        rkn_status = "Требуется уточнение реестровых записей"

    try:
        h_star = OptimalHorizonSolver.compute_optimal_horizon(uncertainty=0.25, base_fine=120000.0)
    except Exception:
        h_star = 29

    return web.json_response({
        "domain": clean_domain,
        "rkn_status": rkn_status,
        "critical_finding": crit,
        "compliance_fact": comp,
        "hidden_findings_count": hidden_count,
        "risk_horizon_weeks": h_star,
    })


async def handle_lead_submission(request: web.Request) -> web.Response:
    """
    Inbound lead capture API. Requires explicit consent_pd == True.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON-запрос"}, status=400)

    consent_pd = data.get("consent_pd")
    if consent_pd is not True:
        return web.json_response(
            {"error": "Для отправки заявки необходимо согласие на обработку персональных данных (152-ФЗ)"},
            status=400,
        )

    domain = str(data.get("domain", "")).strip()
    phone = str(data.get("phone", "")).strip()
    email = str(data.get("email", "")).strip()
    tariff = str(data.get("tariff", "Комплект под ключ (14 900 ₽)")).strip()

    if not phone and not email:
        return web.json_response({"error": "Укажите номер телефона или адрес электронной почты"}, status=400)

    client_ip = request.headers.get("X-Forwarded-For", request.remote or "127.0.0.1").split(",")[0].strip()

    try:
        lead_id = save_inbound_lead(domain=domain, phone=phone, email=email, tariff=tariff, client_ip=client_ip)
    except Exception as e:
        logger.error(f"Error persisting inbound lead: {e}")
        return web.json_response({"error": "Ошибка сохранения заявки в базу данных"}, status=500)

    asyncio.create_task(
        send_lead_telegram_notification(domain=domain, phone=phone, email=email, tariff=tariff, client_ip=client_ip)
    )

    return web.json_response({
        "status": "ok",
        "lead_id": lead_id,
        "message": "Заявка успешно принята! Наш специалист свяжется с вами в течение 30 минут.",
    })


async def handle_showcase(request: web.Request) -> web.Response:
    """
    Renders 72-hour report by secret token, filters preview bots, and triggers alert.
    """
    secret_slug = request.match_info.get("secret_slug", "")
    manifest = load_manifest()
    entry = manifest.get(secret_slug)

    headers = {
        "X-Robots-Tag": "noindex, nofollow, noarchive, nosnippet",
        "Cache-Control": "private, no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    if not entry:
        not_found_html = """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Справка не найдена</title>
<style>body{background:#0f172a;color:#f8fafc;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}</style>
</head><body><div style="text-align:center;"><h2>404 • Справка не найдена</h2><p style="color:#94a3b8;">Ссылка недействительна или была удалена.</p></div></body></html>"""
        return web.Response(text=not_found_html, content_type="text/html", status=404, headers=headers)

    created_str = entry.get("created_at")
    if created_str:
        try:
            created_dt = datetime.fromisoformat(created_str)
            if datetime.now() - created_dt > timedelta(hours=72):
                cleanup_expired_leads()
                expired_html = """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>Срок действия истек</title>
<style>body{background:#0f172a;color:#f8fafc;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}</style>
</head><body><div style="text-align:center;max-width:540px;padding:24px;">
<h2>Срок действия справки (72 часа) истек</h2>
<p style="color:#94a3b8;line-height:1.5;">В соответствии с регламентом конфиденциальности сервиса комплаенс-мониторинга AdRadar, временные отчеты безвозвратно удаляются по истечении 72 часов.</p>
</div></body></html>"""
                return web.Response(text=expired_html, content_type="text/html", status=410, headers=headers)
        except Exception:
            pass

    html_path_str = entry.get("html_path")
    if not html_path_str or not Path(html_path_str).exists():
        return web.Response(text="Отчет перемещен или удален.", status=404, headers=headers)

    with open(html_path_str, "r", encoding="utf-8") as f:
        html_content = f.read()

    user_agent = request.headers.get("User-Agent", "")
    is_bot = bool(BOT_USER_AGENTS.search(user_agent)) or request.method == "HEAD"

    if not is_bot:
        client_ip = request.headers.get("X-Forwarded-For", request.remote or "127.0.0.1").split(",")[0].strip()
        asyncio.create_task(send_telegram_notification(entry, client_ip, user_agent))

    return web.Response(text=html_content, content_type="text/html", status=200, headers=headers)


async def handle_health(request: web.Request) -> web.Response:
    """Lightweight health check for reverse proxy / systemd watchdog."""
    return web.json_response({"status": "ok", "service": "adradar-showcase-server"})


def create_app() -> web.Application:
    """Creates and configures the aiohttp web application."""
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/demo", handle_demo)
    app.router.add_get("/privacy-policy", handle_privacy_policy)
    app.router.add_get("/consent", handle_consent)
    app.router.add_post("/api/express-check", handle_express_check)
    app.router.add_post("/api/lead", handle_lead_submission)
    app.router.add_get("/r/{secret_slug}", handle_showcase)
    app.router.add_get("/healthz", handle_health)
    return app


def main():
    parser = argparse.ArgumentParser(description="AdRadar Ephemeral Showcase Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Listen port (default: 8080)")
    args = parser.parse_args()

    cleanup_expired_leads()

    logger.info(f"Starting AdRadar Showcase Server on {args.host}:{args.port}")
    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

"""
Autonomous Ephemeral Showcase Server for AdRadar / LegalSheaf Reports.
Serves 72-hour audit sheets via cryptosecure random tokens.
Includes:
- Public Landing Page (GET /) in Russian Engineering Fintech style (0 CDN, 152/168-FZ compliant);
- Interactive Demo Audit (GET /demo);
- Reference Compliance Pages (GET /privacy-policy, GET /consent);
- Rate-Limited Express Scan API (POST /api/express-check) with strict Anti-SSRF & Zero-Mock;
- Inbound Lead Capture API (POST /api/lead) with SQLite storage, rate limiting, and escaped Telegram alerts;
- Ephemeral 72-hour audit reports (GET /r/{secret_slug}) with anti-indexing and preview bot filtering.
"""

from __future__ import annotations

import argparse
import asyncio
import html as html_lib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiohttp import web
import httpx

from legal_sheaf.config import ADRADAR_DATA_DIR, REPORTS_DIR
from legal_sheaf.dual_auditor import DualAuditor
from legal_sheaf.evolutionary_horizon import OptimalHorizonSolver
from legal_sheaf.net_safety import SSRFBlockedError, is_safe_url
from legal_sheaf.remediation_kit import generate_privacy_policy_html, generate_separate_consent_html
from legal_sheaf.web_templates import render_demo_page, render_landing_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("showcase_server")

LEADS_DIR = REPORTS_DIR / "leads"
MANIFEST_FILE = LEADS_DIR / "showcase_manifest.json"
INBOUND_DB_PATH = ADRADAR_DATA_DIR / "db" / "inbound_leads.sqlite"

DEFAULT_PLACEHOLDER = "[РЕКВИЗИТЫ_ОПЕРАТОРА_ИЗ_ENV]"
MAX_RATE_LIMIT_KEYS = 10_000

ENV_KEYS_OF_INTEREST = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OPERATOR_LEGAL_NAME",
    "OPERATOR_INN",
    "OPERATOR_OGRN",
    "OPERATOR_EMAIL",
    "ADRADAR_BASE_URL",
    "SHOWCASE_BASE_URL",
    "TRUST_PROXY",
    "CORS_ALLOW_ORIGIN",
}

BOT_USER_AGENTS = re.compile(
    r"(?i)(?:telegrambot|whatsapp|vkshare|slackbot|twitterbot|facebookexternalhit|"
    r"meta-externalagent|discordbot|applebot|yandexbot|googlebot|bingbot|baiduspider|"
    r"duckduckbot|skypeuripreview|linkedinbot|embedly|quora link preview|showyoubot|"
    r"outbrain|pinterest|curl|wget|python-requests|httpx|aiohttp)",
)

# Bounded in-memory rate limiting: bucket_key -> [timestamp, ...]
RATE_LIMIT_STORE: Dict[str, List[float]] = {}

# Lazy DualAuditor singleton
_AUDITOR_SINGLETON: Optional[DualAuditor] = None
AUDITOR_APP_KEY = web.AppKey("auditor", DualAuditor)

STRICT_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


def load_runtime_env() -> None:
    """
    Loads runtime configuration from ADRADAR_DATA_DIR/.env, repository root .env, and CWD/.env
    without overwriting non-empty variables already present in os.environ.
    """
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        ADRADAR_DATA_DIR / ".env",
        repo_root / ".env",
        Path.cwd() / ".env",
    ]
    seen: Set[Path] = set()
    for env_path in candidates:
        try:
            resolved = env_path.resolve()
        except Exception:
            resolved = env_path
        if resolved in seen or not env_path.exists():
            continue
        seen.add(resolved)
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in ENV_KEYS_OF_INTEREST and val and not os.environ.get(key, "").strip():
                    os.environ[key] = val
        except Exception as exc:
            logger.warning(f"Failed to parse .env file {env_path}: {exc}")


def get_operator_config() -> Dict[str, str]:
    """Reads operator requisites and public URL strictly from environment configuration."""
    load_runtime_env()
    base_url = (
        os.environ.get("ADRADAR_BASE_URL", "").strip()
        or os.environ.get("SHOWCASE_BASE_URL", "").strip()
    )
    parsed_host = urllib.parse.urlparse(base_url).hostname if base_url else None
    return {
        "legal_name": os.environ.get("OPERATOR_LEGAL_NAME", "").strip() or DEFAULT_PLACEHOLDER,
        "inn": os.environ.get("OPERATOR_INN", "").strip() or DEFAULT_PLACEHOLDER,
        "ogrn": os.environ.get("OPERATOR_OGRN", "").strip() or DEFAULT_PLACEHOLDER,
        "email": os.environ.get("OPERATOR_EMAIL", "").strip() or DEFAULT_PLACEHOLDER,
        "base_url": base_url or "http://127.0.0.1:8080",
        "domain": parsed_host or DEFAULT_PLACEHOLDER,
    }


def get_auditor() -> DualAuditor:
    """Returns process-wide DualAuditor singleton to avoid reloading ONNX/KB per request."""
    global _AUDITOR_SINGLETON
    if _AUDITOR_SINGLETON is None:
        _AUDITOR_SINGLETON = DualAuditor()
    return _AUDITOR_SINGLETON


def get_client_ip(request: web.Request) -> str:
    """
    Resolves client IP from socket peer (request.remote).
    Honors X-Forwarded-For ONLY when TRUST_PROXY=1 is explicitly configured.
    """
    if os.environ.get("TRUST_PROXY", "").strip() == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            first_ip = xff.split(",")[0].strip()
            if first_ip:
                return first_ip
    return (request.remote or "127.0.0.1").strip()


def _prune_rate_limit_store(now: float, window_sec: float) -> None:
    """Removes expired entries and caps RATE_LIMIT_STORE to prevent unbounded memory growth."""
    expired_keys = [
        k for k, ts_list in RATE_LIMIT_STORE.items()
        if not any(now - t < window_sec for t in ts_list)
    ]
    for k in expired_keys:
        RATE_LIMIT_STORE.pop(k, None)

    if len(RATE_LIMIT_STORE) >= MAX_RATE_LIMIT_KEYS:
        # Evict oldest keys deterministically until under budget
        sorted_keys = sorted(
            RATE_LIMIT_STORE.items(),
            key=lambda kv: max(kv[1]) if kv[1] else 0.0,
        )
        excess = len(RATE_LIMIT_STORE) - MAX_RATE_LIMIT_KEYS + 1
        for k, _ in sorted_keys[:excess]:
            RATE_LIMIT_STORE.pop(k, None)


def check_rate_limit(client_ip: str, limit: int = 5, window_sec: float = 3600.0) -> bool:
    """Enforces bounded per-key rate limiting of maximum requests per window."""
    now = time.time()
    timestamps = [t for t in RATE_LIMIT_STORE.get(client_ip, []) if now - t < window_sec]
    if len(timestamps) >= limit:
        RATE_LIMIT_STORE[client_ip] = timestamps
        return False

    if client_ip not in RATE_LIMIT_STORE and len(RATE_LIMIT_STORE) >= MAX_RATE_LIMIT_KEYS:
        _prune_rate_limit_store(now, window_sec)

    timestamps.append(now)
    RATE_LIMIT_STORE[client_ip] = timestamps
    return True


def _allowed_cors_origins() -> Set[str]:
    origins: Set[str] = set()
    for var_name in ("CORS_ALLOW_ORIGIN", "ADRADAR_BASE_URL", "SHOWCASE_BASE_URL"):
        raw = os.environ.get(var_name, "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            val = item.strip().rstrip("/")
            if val and val != "*":
                origins.add(val)
    return origins


@web.middleware
async def security_and_cors_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Attaches strict security headers and handles explicit CORS preflight requests."""
    origin = request.headers.get("Origin", "").strip().rstrip("/")
    allowed_origins = _allowed_cors_origins()
    origin_allowed = bool(origin and origin in allowed_origins)

    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        resp = await handler(request)

    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Content-Security-Policy"] = STRICT_CSP_HEADER

    if origin_allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    elif request.method == "OPTIONS":
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"

    return resp


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
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"Error loading showcase manifest: {e}")
        return {}


def save_manifest(manifest: Dict[str, Any]) -> None:
    """Saves updated manifest to disk."""
    try:
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving showcase manifest: {e}")


def cleanup_expired_leads(max_age_hours: int = 72) -> int:
    """
    Deletes report folders and manifest records older than max_age_hours.
    Strictly enforces path containment within LEADS_DIR before deleting.
    """
    manifest = load_manifest()
    now = datetime.now()
    cutoff = now - timedelta(hours=max_age_hours)
    leads_root = LEADS_DIR.resolve()
    purged_count = 0
    updated_manifest = {}

    for secret_slug, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
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
            if html_path:
                try:
                    target_dir = html_path.parent.resolve()
                    if target_dir != leads_root and target_dir.is_relative_to(leads_root):
                        if target_dir.exists():
                            shutil.rmtree(target_dir)
                            logger.info(f"Purged expired lead directory: {target_dir}")
                    else:
                        logger.warning(f"Refused unsafe directory purge outside LEADS_DIR: {target_dir}")
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
    Non-blocking, silent on missing bot credentials, strictly HTML-escaped.
    """
    load_runtime_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        logger.debug("Telegram credentials not configured, skipping push.")
        return False

    domain = html_lib.escape(str(entry.get("domain", "Не указан")), quote=True)
    org_name = html_lib.escape(str(entry.get("legal_name") or entry.get("domain") or "Не указан"), quote=True)
    inn = html_lib.escape(str(entry.get("inn") or "Не указан"), quote=True)
    tier = html_lib.escape(str(entry.get("lead_tier", "TIER_B_SMB_IP")), quote=True)
    rev = entry.get("revenue_rub")
    rev_str = f"{rev / 1_000_000:.1f} млн руб." if rev else "Не публикуется"
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    safe_ip = html_lib.escape(str(client_ip), quote=True)
    safe_ua = html_lib.escape(str(user_agent[:120]), quote=True)

    msg = (
        f"🔔 <b>[AdRadar] Лид открыл экспресс-справку!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏢 <b>Компания:</b> {org_name}\n"
        f"🌐 <b>Домен:</b> <code>{domain}</code>\n"
        f"📑 <b>ИНН:</b> <code>{inn}</code>\n"
        f"⭐ <b>Сегмент:</b> <code>{tier}</code>\n"
        f"💰 <b>Выручка (ГИР БО):</b> {html_lib.escape(rev_str, quote=True)}\n"
        f"🕒 <b>Время:</b> {now_str}\n"
        f"📡 <b>IP:</b> <code>{safe_ip}</code>\n"
        f"💻 <b>Браузер:</b> <i>{safe_ua}</i>"
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
            logger.warning(f"Telegram API responded with {resp.status_code}: {resp.text[:200]}")
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
    Strictly escapes all user-controlled values for Telegram HTML parse_mode.
    """
    load_runtime_env()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return False

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    msg = (
        f"🎯 <b>[AdRadar] Новая заявка на внедрение!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Домен:</b> <code>{html_lib.escape(str(domain), quote=True)}</code>\n"
        f"📞 <b>Телефон:</b> <code>{html_lib.escape(str(phone), quote=True)}</code>\n"
        f"✉️ <b>Email:</b> <code>{html_lib.escape(str(email or 'Не указан'), quote=True)}</code>\n"
        f"💼 <b>Тариф:</b> <code>{html_lib.escape(str(tariff), quote=True)}</code>\n"
        f"📡 <b>IP:</b> <code>{html_lib.escape(str(client_ip), quote=True)}</code>\n"
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
    cfg = get_operator_config()
    page_html = render_landing_page(
        operator_email=cfg["email"],
        operator_legal_name=cfg["legal_name"] if cfg["legal_name"] != DEFAULT_PLACEHOLDER else None,
    )
    return web.Response(text=page_html, content_type="text/html", status=200)


async def handle_demo(request: web.Request) -> web.Response:
    """Renders demonstration audit sheet."""
    page_html = render_demo_page()
    return web.Response(text=page_html, content_type="text/html", status=200)


async def handle_privacy_policy(request: web.Request) -> web.Response:
    """Renders benchmark privacy policy populated strictly from environment requisites."""
    cfg = get_operator_config()
    page_html = generate_privacy_policy_html(
        legal_name=cfg["legal_name"],
        inn=cfg["inn"],
        ogrn=cfg["ogrn"],
        domain=cfg["domain"],
        white_label="AdRadar Правовой Инжиниринг",
        contact_email=cfg["email"],
    )
    return web.Response(text=page_html, content_type="text/html", status=200)


async def handle_consent(request: web.Request) -> web.Response:
    """Renders benchmark consent document under 156-FZ from environment requisites."""
    cfg = get_operator_config()
    page_html = generate_separate_consent_html(
        legal_name=cfg["legal_name"],
        inn=cfg["inn"],
        domain=cfg["domain"],
        white_label="AdRadar Правовой Инжиниринг",
        contact_email=cfg["email"],
        ogrn=cfg["ogrn"],
    )
    return web.Response(text=page_html, content_type="text/html", status=200)


async def handle_express_check(request: web.Request) -> web.Response:
    """
    Rate-limited API for instant website X-Ray preview teaser.
    Enforces JSON object validation, strict Anti-SSRF URL validation, and Zero-Mock error reporting.
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(f"express:{client_ip}", limit=5, window_sec=3600.0):
        return web.json_response(
            {"error": "Превышен лимит проверок (максимум 5 в час). Оставьте заявку в форме ниже."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON-запрос"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "Expected JSON object"}, status=400)

    raw_domain = str(data.get("domain", "")).strip()
    if not raw_domain or len(raw_domain) > 255:
        return web.json_response({"error": "Домен обязателен для проверки"}, status=400)

    target_url = raw_domain if "://" in raw_domain else f"https://{raw_domain}"
    safe_ok, safe_reason = is_safe_url(target_url)
    if not safe_ok:
        logger.warning(f"Express check blocked unsafe/SSRF target {raw_domain!r}: {safe_reason}")
        return web.json_response(
            {"error": "Forbidden private or local target", "detail": safe_reason},
            status=400,
        )

    parsed_target = urllib.parse.urlparse(target_url)
    clean_domain = (parsed_target.hostname or "").lower()
    if clean_domain.startswith("www."):
        clean_domain = clean_domain[4:]
    if not clean_domain:
        return web.json_response({"error": "Некорректное доменное имя"}, status=400)

    auditor = request.app.get(AUDITOR_APP_KEY) or get_auditor()
    try:
        site_rep = await asyncio.wait_for(
            auditor.audit_site(clean_domain, deep=False, gear="fast_l0"),
            timeout=12.0,
        )
    except SSRFBlockedError as ssrf_exc:
        return web.json_response(
            {"error": "Forbidden private or local target", "detail": str(ssrf_exc)},
            status=400,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Express audit timeout for {clean_domain}")
        return web.json_response(
            {
                "status": "UNVERIFIED_TIMEOUT",
                "domain": clean_domain,
                "error": "Превышено время ожидания ответа от целевого сайта (UNVERIFIED_TIMEOUT).",
            },
            status=504,
        )
    except Exception as exc:
        logger.warning(f"Express audit error for {clean_domain}: {exc}")
        return web.json_response(
            {
                "status": "UNVERIFIED_TIMEOUT",
                "domain": clean_domain,
                "error": "Целевой сайт недоступен или отклонил соединение (UNVERIFIED_TIMEOUT).",
            },
            status=502,
        )

    site_status = site_rep.get("status", "")
    if site_status in ("CONNECTION_FAILED", "NON_HTML_RESPONSE"):
        return web.json_response(
            {
                "status": "UNVERIFIED_TIMEOUT",
                "domain": clean_domain,
                "error": f"Не удалось просканировать целевой сайт ({site_status}).",
            },
            status=502,
        )

    tl = site_rep.get("traffic_light", {})
    reds = tl.get("RED_REAL_FINE", [])
    oranges = tl.get("ORANGE_REGULATORY_DEMAND", [])
    grays = tl.get("GRAY_HYGIENE_BONUS", [])

    crit = (
        reds[0].get("description")
        if reds
        else (
            oranges[0].get("description")
            if oranges
            else "Критических нарушений на поверхности (L0) не выявлено"
        )
    )
    comp = (
        grays[0].get("description")
        if grays
        else "Ответ целевого HTML-ресурса получен корректно"
    )
    hidden_count = max(0, len(reds) + len(oranges) + len(grays) - 2)
    rkn_status = (
        "Выявлены индикаторы риска (требуется проверка по ст. 49 248-ФЗ)"
        if (reds or oranges)
        else "Индикаторы риска на уровне L0 не обнаружены"
    )

    solver = OptimalHorizonSolver()
    h_star = solver.solve(uncertainty=0.25 if (reds or oranges) else 0.15)

    return web.json_response({
        "status": "OK",
        "domain": clean_domain,
        "rkn_status": rkn_status,
        "critical_finding": crit,
        "compliance_fact": comp,
        "hidden_findings_count": hidden_count,
        "risk_horizon_weeks": h_star,
    })


async def handle_lead_submission(request: web.Request) -> web.Response:
    """
    Inbound lead capture API. Enforces rate limit, JSON dict check, and explicit consent_pd == True.
    """
    client_ip = get_client_ip(request)
    if not check_rate_limit(f"lead:{client_ip}", limit=10, window_sec=3600.0):
        return web.json_response(
            {"error": "Превышен лимит отправки заявок. Повторите попытку позже."},
            status=429,
        )

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Некорректный JSON-запрос"}, status=400)

    if not isinstance(data, dict):
        return web.json_response({"error": "Expected JSON object"}, status=400)

    consent_pd = data.get("consent_pd")
    if consent_pd is not True:
        return web.json_response(
            {"error": "Для отправки заявки необходимо согласие на обработку персональных данных (152-ФЗ)"},
            status=400,
        )

    domain = str(data.get("domain", "")).strip()[:255]
    phone = str(data.get("phone", "")).strip()[:64]
    email = str(data.get("email", "")).strip()[:128]
    tariff = str(data.get("tariff", "Комплект под ключ (14 900 ₽)")).strip()[:128]

    if not phone and not email:
        return web.json_response({"error": "Укажите номер телефона или адрес электронной почты"}, status=400)

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

    if not entry or not isinstance(entry, dict):
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
    if not html_path_str:
        return web.Response(text="Отчет перемещен или удален.", status=404, headers=headers)

    try:
        resolved_html = Path(html_path_str).resolve()
        leads_root = LEADS_DIR.resolve()
        if not resolved_html.is_relative_to(leads_root) or not resolved_html.exists():
            return web.Response(text="Отчет перемещен или удален.", status=404, headers=headers)
    except Exception:
        return web.Response(text="Отчет перемещен или удален.", status=404, headers=headers)

    with open(resolved_html, "r", encoding="utf-8") as f:
        html_content = f.read()

    user_agent = request.headers.get("User-Agent", "")
    is_bot = bool(BOT_USER_AGENTS.search(user_agent)) or request.method == "HEAD"

    if not is_bot:
        client_ip = get_client_ip(request)
        asyncio.create_task(send_telegram_notification(entry, client_ip, user_agent))

    return web.Response(text=html_content, content_type="text/html", status=200, headers=headers)


async def handle_health(request: web.Request) -> web.Response:
    """Lightweight health check for reverse proxy / systemd watchdog."""
    return web.json_response({"status": "ok", "service": "adradar-showcase-server"})


def create_app() -> web.Application:
    """Creates and configures the aiohttp web application."""
    load_runtime_env()
    app = web.Application(middlewares=[security_and_cors_middleware])
    app[AUDITOR_APP_KEY] = get_auditor()
    app.router.add_get("/", handle_index)
    app.router.add_get("/demo", handle_demo)
    app.router.add_get("/privacy-policy", handle_privacy_policy)
    app.router.add_get("/consent", handle_consent)
    app.router.add_post("/api/express-check", handle_express_check)
    app.router.add_post("/api/lead", handle_lead_submission)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", handle_health)
    app.router.add_get("/r/{secret_slug}", handle_showcase)
    app.router.add_get("/healthz", handle_health)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="AdRadar Ephemeral Showcase Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Listen port (default: 8080)")
    args = parser.parse_args()

    load_runtime_env()
    cleanup_expired_leads()

    logger.info(f"Starting AdRadar Showcase Server on {args.host}:{args.port}")
    app = create_app()
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

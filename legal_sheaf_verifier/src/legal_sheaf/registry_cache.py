"""
AdRadar State Registries SQLite Cache & Per-Host Rate Limiter.
Strict compliance with AGENTS.md, Zero-Poisoning Invariant, and Government Registry IP Protection.

Storage:
- Single WAL SQLite database: C:\\AdRadarData\\db\\registry_cache.sqlite
TTL:
- ГИР БО ФНС (bo.nalog.gov.ru / GIR_BO / FNS): 30 days (2,592,000s).
- Реестр операторов РКН (pd.rkn.gov.ru / RKN): 7 days (604,800s).
Zero-Poisoning Invariant:
- Caches strictly verified HTTP 200 responses (TriState != UNVERIFIED).
- Network errors, timeouts, and unverified data are NEVER cached.
Per-Host Rate Limiter:
- Enforces >= 1.5s interval between outbound requests to pd.rkn.gov.ru and bo.nalog.gov.ru.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from legal_sheaf.config import DB_DIR

logger = logging.getLogger("legal_sheaf.registry_cache")

REGISTRY_DB_PATH = DB_DIR / "registry_cache.sqlite"

# TTL in seconds
TTL_FNS_SECONDS = 30 * 86400  # 30 days
TTL_RKN_SECONDS = 7 * 86400   # 7 days
TTL_DEFAULT_SECONDS = 7 * 86400

# Minimum spacing between outbound requests per host
HOST_RATE_LIMIT_INTERVALS: Dict[str, float] = {
    "pd.rkn.gov.ru": 1.5,
    "bo.nalog.gov.ru": 1.5,
}

# Per-host timestamps and locks
_last_host_query_times: Dict[str, float] = {}
_sync_lock = threading.Lock()
_async_locks: Dict[str, asyncio.Lock] = {}


def _get_async_lock(host: str) -> asyncio.Lock:
    if host not in _async_locks:
        _async_locks[host] = asyncio.Lock()
    return _async_locks[host]


def _init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target_path = db_path or REGISTRY_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS registries_cache (
            registry_type TEXT NOT NULL,
            inn TEXT NOT NULL,
            data_json TEXT NOT NULL,
            cached_at REAL NOT NULL,
            PRIMARY KEY (registry_type, inn)
        );
        """
    )
    conn.commit()
    return conn


class RegistryCacheStore:
    """Object-oriented wrapper around state registry cache."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or REGISTRY_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _init_db(self.db_path) as conn:
            pass

    def get(self, registry_type: str, inn: str) -> Optional[Dict[str, Any]]:
        return get_cached_registry(registry_type, inn, db_path=self.db_path)

    def set(self, registry_type: str, inn: str, data: Dict[str, Any]) -> bool:
        return save_cached_registry(registry_type, inn, data, db_path=self.db_path)

    def get_fns(self, inn: str) -> Optional[Dict[str, Any]]:
        return self.get("FNS", inn)

    def set_fns(self, inn: str, data: Dict[str, Any]) -> bool:
        return self.set("FNS", inn, data)

    def get_rkn(self, inn: str) -> Optional[Dict[str, Any]]:
        return self.get("RKN", inn)

    def set_rkn(self, inn: str, data: Dict[str, Any]) -> bool:
        return self.set("RKN", inn, data)


def _get_ttl_for_registry(registry_type: str) -> float:
    reg_upper = registry_type.strip().upper()
    if reg_upper in ("GIR_BO", "FNS", "BO_NALOG"):
        return TTL_FNS_SECONDS
    elif reg_upper in ("RKN", "PD_RKN"):
        return TTL_RKN_SECONDS
    return TTL_DEFAULT_SECONDS


def get_cached_registry(
    registry_type: str,
    inn: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """
    Retrieves cached entry if present and fresher than TTL:
    - FNS: 30 days
    - RKN: 7 days
    """
    if not inn:
        return None
    inn_clean = inn.strip()
    ttl = _get_ttl_for_registry(registry_type)

    try:
        with _init_db(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT data_json, cached_at FROM registries_cache WHERE registry_type = ? AND inn = ?",
                (registry_type, inn_clean),
            )
            row = cur.fetchone()
            if not row:
                return None
            data_json, cached_at = row
            if (time.time() - cached_at) > ttl:
                return None
            parsed = json.loads(data_json)
            if isinstance(parsed, dict):
                parsed["from_cache"] = True
            return parsed
    except Exception as e:
        logger.warning(f"Error reading registry cache for {registry_type}:{inn}: {e}")
        return None


def save_cached_registry(
    registry_type: str,
    inn: str,
    data: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> bool:
    """
    Saves confirmed registry response into SQLite cache.
    Zero-Poisoning Invariant:
    - Never caches UNVERIFIED, RKN_CHECK_UNAVAILABLE, or ERROR statuses.
    Returns True if successfully stored, False if rejected or failed.
    """
    if not inn or not data:
        return False
    inn_clean = inn.strip()

    # Zero-Poisoning Guard: Reject failed, timeout, or unverified results
    status = str(data.get("status", "")).upper()
    if status in ("RKN_CHECK_UNAVAILABLE", "UNVERIFIED", "ERROR", "TIMEOUT", "FAILED"):
        return False

    # Check for error payload
    if data.get("error_details") or data.get("error"):
        return False

    try:
        with _init_db(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO registries_cache (registry_type, inn, data_json, cached_at)
                VALUES (?, ?, ?, ?)
                """,
                (registry_type, inn_clean, json.dumps(data, ensure_ascii=False), time.time()),
            )
            conn.commit()
            return True
    except Exception as e:
        logger.warning(f"Error writing registry cache for {registry_type}:{inn}: {e}")
        return False


def clear_registry_cache(registry_type: Optional[str] = None, inn: Optional[str] = None) -> None:
    """Clears cached entries optionally filtered by type or INN."""
    try:
        with _init_db() as conn:
            if registry_type and inn:
                conn.execute("DELETE FROM registries_cache WHERE registry_type = ? AND inn = ?", (registry_type, inn))
            elif registry_type:
                conn.execute("DELETE FROM registries_cache WHERE registry_type = ?", (registry_type,))
            elif inn:
                conn.execute("DELETE FROM registries_cache WHERE inn = ?", (inn,))
            else:
                conn.execute("DELETE FROM registries_cache")
            conn.commit()
    except Exception as e:
        logger.warning(f"Error clearing registry cache: {e}")


# =============================================================================
# PER-HOST RATE LIMITERS (1.5s interval to prevent IP bans)
# =============================================================================
def enforce_host_rate_limit_sync(host: str, min_interval: Optional[float] = None) -> None:
    """Synchronously enforces min interval for the target registry host."""
    interval = min_interval if min_interval is not None else HOST_RATE_LIMIT_INTERVALS.get(host, 1.5)
    with _sync_lock:
        now = time.time()
        last = _last_host_query_times.get(host, 0.0)
        elapsed = now - last
        if elapsed < interval:
            time.sleep(interval - elapsed)
        _last_host_query_times[host] = time.time()


async def enforce_host_rate_limit_async(host: str, min_interval: Optional[float] = None) -> None:
    """Asynchronously enforces min interval for the target registry host."""
    interval = min_interval if min_interval is not None else HOST_RATE_LIMIT_INTERVALS.get(host, 1.5)
    lock = _get_async_lock(host)
    async with lock:
        now = time.time()
        last = _last_host_query_times.get(host, 0.0)
        elapsed = now - last
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _last_host_query_times[host] = time.time()


# Dedicated helper aliases for pd.rkn.gov.ru
def enforce_rkn_rate_limit_sync(min_interval: Optional[float] = None) -> None:
    enforce_host_rate_limit_sync("pd.rkn.gov.ru", min_interval=min_interval)


async def enforce_rkn_rate_limit_async(min_interval: Optional[float] = None) -> None:
    await enforce_host_rate_limit_async("pd.rkn.gov.ru", min_interval=min_interval)


# Dedicated helper aliases for bo.nalog.gov.ru
def enforce_fns_rate_limit_sync(min_interval: Optional[float] = None) -> None:
    enforce_host_rate_limit_sync("bo.nalog.gov.ru", min_interval=min_interval)


async def enforce_fns_rate_limit_async(min_interval: Optional[float] = None) -> None:
    await enforce_host_rate_limit_async("bo.nalog.gov.ru", min_interval=min_interval)

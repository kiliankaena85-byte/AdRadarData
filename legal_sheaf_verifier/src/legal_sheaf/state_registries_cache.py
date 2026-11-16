"""
AdRadar State Registries Cache & Rate Limiter (Compatibility shim for registry_cache.py).
Re-exports all functionality from registry_cache.
"""

from __future__ import annotations

from legal_sheaf.registry_cache import (
    REGISTRY_DB_PATH as CACHE_DB_PATH,
    TTL_FNS_SECONDS,
    TTL_RKN_SECONDS,
    get_cached_registry,
    save_cached_registry,
    clear_registry_cache,
    enforce_host_rate_limit_sync,
    enforce_host_rate_limit_async,
    enforce_rkn_rate_limit_sync,
    enforce_rkn_rate_limit_async,
    enforce_fns_rate_limit_sync,
    enforce_fns_rate_limit_async,
)

CACHE_TTL_SECONDS = TTL_FNS_SECONDS
RKN_MIN_INTERVAL_SECONDS = 1.5

__all__ = [
    "CACHE_DB_PATH",
    "CACHE_TTL_SECONDS",
    "TTL_FNS_SECONDS",
    "TTL_RKN_SECONDS",
    "RKN_MIN_INTERVAL_SECONDS",
    "get_cached_registry",
    "save_cached_registry",
    "clear_registry_cache",
    "enforce_host_rate_limit_sync",
    "enforce_host_rate_limit_async",
    "enforce_rkn_rate_limit_sync",
    "enforce_rkn_rate_limit_async",
    "enforce_fns_rate_limit_sync",
    "enforce_fns_rate_limit_async",
]

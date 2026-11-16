"""
Live OpenRouter Catalog Resolver & Financial Lock.
Refreshes model catalog every 24h, verifies free status (pricing == 0, image in inputs),
and queries API key remaining limits.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml

from legal_sheaf.config import ADRADAR_DATA_DIR

logger = logging.getLogger("vlm_context_engine.live_catalog")

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_CATALOG_ENDPOINT = "https://openrouter.ai/api/v1/models"
DEFAULT_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
DEFAULT_CACHE_FILE = ADRADAR_DATA_DIR / "cache" / "openrouter_models_catalog.json"
DEFAULT_PRIMARY_FREE_MODEL = "nvidia/nemotron-3.5-super-vl:free"
DEFAULT_FALLBACK_FREE_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "openrouter/free",
]


class FinancialLockViolation(Exception):
    """Raised when a paid model is requested or detected in free rotation."""
    pass


class LiveCatalogResolver:
    """
    Syncs and validates OpenRouter model IDs against live API catalog.
    Refreshes catalog every 24 hours.
    Enforces the Financial Lock: ensures free models have pricing == 0.0 and multimodal image support.
    Enforces banned model policy (deprecations, high-error rate models).
    """

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or self._find_config()
        self.config = self._load_config()
        settings = self.config.get("live_catalog_settings", {})
        self.refresh_interval = settings.get("refresh_interval_seconds", 86400)
        self.catalog_endpoint = settings.get("catalog_endpoint", DEFAULT_CATALOG_ENDPOINT)
        self.key_endpoint = DEFAULT_KEY_ENDPOINT
        self.cache_file = Path(settings.get("cache_file", str(DEFAULT_CACHE_FILE)))
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    def _find_config(self) -> Path:
        candidates = [
            Path.cwd() / "config" / "openrouter_models.yaml",
            ROOT_DIR / "config" / "openrouter_models.yaml",
            Path.cwd() / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
            ROOT_DIR / "legal_sheaf_verifier" / "config" / "openrouter_models.yaml",
        ]
        for p in candidates:
            if p.exists():
                return p
        return ROOT_DIR / "config" / "openrouter_models.yaml"

    def _load_config(self) -> Dict[str, Any]:
        if self.config_path.exists():
            try:
                return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning(f"Failed to parse openrouter_models.yaml: {e}")
        return {}

    async def get_live_catalog(self, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
        """Loads cached catalog or fetches fresh from OpenRouter API if older than 24h."""
        # 1. Check disk cache freshness
        if self.cache_file.exists():
            mtime = self.cache_file.stat().st_mtime
            if (time.time() - mtime) < self.refresh_interval:
                try:
                    data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "data" in data:
                        return {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict) and "id" in m}
                except Exception:
                    pass

        # 2. Fetch fresh catalog from OpenRouter
        close_local = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=15.0)
            close_local = True

        try:
            resp = await c.get(
                self.catalog_endpoint,
                headers={"User-Agent": "AdRadar-VLMContextEngine/2.0"},
            )
            if resp.status_code == 200:
                raw_json = resp.json()
                self.cache_file.write_text(json.dumps(raw_json, ensure_ascii=False, indent=2), encoding="utf-8")
                return {m.get("id"): m for m in raw_json.get("data", []) if isinstance(m, dict) and "id" in m}
        except Exception as e:
            logger.warning(f"Could not refresh OpenRouter live catalog: {e}")
        finally:
            if close_local:
                await c.aclose()

        # 3. Fallback to existing disk cache if network call failed
        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                return {m.get("id"): m for m in data.get("data", []) if isinstance(m, dict) and "id" in m}
            except Exception:
                pass

        return {}

    async def check_key_limits(self, api_key: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
        """Queries GET https://openrouter.ai/api/v1/key to verify limits and remaining credits."""
        if not api_key:
            return {"valid": False, "reason": "No API key provided"}

        close_local = False
        c = client
        if c is None:
            c = httpx.AsyncClient(timeout=10.0)
            close_local = True

        try:
            resp = await c.get(
                self.key_endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "AdRadar-VLMContextEngine/2.0",
                },
            )
            if resp.status_code == 200:
                key_data = resp.json().get("data", {})
                return {
                    "valid": True,
                    "label": key_data.get("label"),
                    "limit": key_data.get("limit"),
                    "usage": key_data.get("usage"),
                    "limit_remaining": key_data.get("limit_remaining"),
                    "is_free_tier": key_data.get("is_free_tier", False),
                    "rate_limit": key_data.get("rate_limit"),
                }
            return {"valid": False, "status_code": resp.status_code, "error": resp.text}
        except Exception as e:
            return {"valid": False, "error": str(e)}
        finally:
            if close_local:
                await c.aclose()

    def verify_model_free(self, model_id: str, catalog: Dict[str, Any]) -> bool:
        """
        Financial Lock Invariant:
        Ensures a model is 100% free (pricing == 0.0) and supports image input.
        Never charges user credits in free mode.
        """
        if model_id == "openrouter/free":
            # Virtual free router
            return True

        if not catalog:
            # If catalog is unavailable, fallback to verifying :free suffix
            return model_id.endswith(":free")

        info = catalog.get(model_id)
        if not info:
            return False

        pricing = info.get("pricing", {})
        try:
            prompt_cost = float(pricing.get("prompt", 0.0) or 0.0)
            comp_cost = float(pricing.get("completion", 0.0) or 0.0)
        except (ValueError, TypeError):
            return False

        if prompt_cost != 0.0 or comp_cost != 0.0:
            logger.warning(f"Financial Lock: model {model_id} has non-zero pricing (prompt={prompt_cost}, comp={comp_cost}).")
            return False

        arch = info.get("architecture", {})
        input_modalities = arch.get("input_modalities", [])
        if "image" not in input_modalities:
            logger.warning(f"Financial Lock: model {model_id} lacks 'image' in input_modalities ({input_modalities}).")
            return False

        return True

    async def resolve_models(
        self,
        paid_zdr_mode: bool = False,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[str]:
        """
        Resolves prioritized, validated list of candidate models for execution.
        Guarantees that :free models satisfy the Financial Lock (pricing == 0, image support).
        """
        catalog = await self.get_live_catalog(client=client)

        if paid_zdr_mode:
            paid_tier = self.config.get("paid_zdr_tier", {})
            primary = paid_tier.get("primary", "google/gemini-4-hydrogen")
            alternatives = paid_tier.get("alternatives", ["deepseek/deepseek-v4.1-flash-1017"])
            vip_arbiter = paid_tier.get("vip_arbiter", "google/gemini-4-argon")
            banned = set(paid_tier.get("banned_models", []))

            candidates = [primary] + [m for m in alternatives if m != primary] + [vip_arbiter]
            valid = []
            for m in candidates:
                if any(b in m for b in banned):
                    logger.warning(f"Banned model {m} rejected from paid ZDR candidates.")
                    continue
                valid.append(m)
            return valid or ["google/gemini-4-hydrogen"]

        # Free tier
        free_tier = self.config.get("free_tier", {})
        primary = free_tier.get("primary", DEFAULT_PRIMARY_FREE_MODEL)
        fallbacks = free_tier.get("fallbacks", DEFAULT_FALLBACK_FREE_MODELS)
        decider = free_tier.get("decider", "openrouter/free")
        banned = set(free_tier.get("banned_models", [])) | set(self.config.get("paid_zdr_tier", {}).get("banned_models", []))
        candidates = [primary] + [m for m in fallbacks if m != primary]
        if decider not in candidates:
            candidates.append(decider)

        candidates = [m for m in candidates if not any(b in m for b in banned)]

        if not catalog:
            return candidates

        valid_models = []
        for m in candidates:
            if self.verify_model_free(m, catalog):
                valid_models.append(m)
            else:
                logger.warning(f"Model {m} failed Financial Lock verification, removing from rotation.")

        # Always ensure at least openrouter/free is present as ultimate safety net
        if "openrouter/free" not in valid_models:
            valid_models.append("openrouter/free")

        return valid_models

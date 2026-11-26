"""
Pytest configuration and session-level test fixture caching for LegalSheafNet.
Accelerates pytest execution by caching heavy neural models and precedent indexes across test modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest

# Ensure non-interactive mode and TF disabled during all test runs
os.environ["ADRADAR_NON_INTERACTIVE"] = "1"
os.environ["USE_TF"] = "0"

from legal_sheaf import config as legal_config
from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.config import MODELS_CACHE_DIR, ARTIFACTS_DIR


@pytest.fixture(autouse=True)
def isolate_adradar_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Strictly isolates every pytest test from production C:\\AdRadarData\\.
    Redirects ADRADAR_DATA_DIR and all module-level DB/report/review_desk/cache paths to tmp_path.
    """
    data_dir = (tmp_path / "adradar_data").resolve()
    db_dir = data_dir / "db"
    reports_dir = data_dir / "reports"
    leads_dir = reports_dir / "leads"
    models_dir = data_dir / "models"
    review_desk_dir = data_dir / "review_desk"
    cache_dir = data_dir / "cache"
    kits_dir = data_dir / "kits"
    for d in (data_dir, db_dir, reports_dir, leads_dir, models_dir, review_desk_dir, cache_dir, kits_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ADRADAR_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(legal_config, "ADRADAR_DATA_DIR", data_dir)
    monkeypatch.setattr(legal_config, "DB_DIR", db_dir)
    monkeypatch.setattr(legal_config, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(legal_config, "MODELS_CACHE_DIR", models_dir)
    monkeypatch.setattr(legal_config, "SQLITE_DB_PATH", db_dir / "adradar_crawler.db")

    module_patches = {
        "legal_sheaf.batch_checkpoints": {"CHECKPOINTS_DB_PATH": db_dir / "batch_checkpoints.sqlite"},
        "legal_sheaf.outreach_ledger": {"LEDGER_DB_PATH": db_dir / "outreach_ledger.sqlite"},
        "legal_sheaf.registry_cache": {"REGISTRY_DB_PATH": db_dir / "registry_cache.sqlite"},
        "legal_sheaf.state_registries_cache": {"CACHE_DB_PATH": db_dir / "registry_cache.sqlite"},
        "legal_sheaf.lead_packager": {
            "REPORTS_DIR": reports_dir,
            "LEADS_OUTPUT_DIR": leads_dir,
            "MANIFEST_FILE": leads_dir / "showcase_manifest.json",
        },
        "legal_sheaf.remediation_kit": {"ADRADAR_DATA_DIR": data_dir},
        "legal_sheaf.lawyer_dossier_exporter": {"ADRADAR_DATA_DIR": data_dir},
        "legal_sheaf.dual_auditor": {"ADRADAR_DATA_DIR": data_dir, "MODELS_CACHE_DIR": models_dir},
        "legal_sheaf.triad_decision_router": {"DB_DIR": db_dir, "MODELS_CACHE_DIR": models_dir},
        "legal_sheaf.vendor_chain": {"DB_DIR": db_dir},
        "legal_sheaf.cli": {"ADRADAR_DATA_DIR": data_dir, "MODELS_CACHE_DIR": models_dir, "SQLITE_DB_PATH": db_dir / "adradar_crawler.db"},
        "showcase_server": {
            "ADRADAR_DATA_DIR": data_dir,
            "REPORTS_DIR": reports_dir,
            "LEADS_DIR": leads_dir,
            "MANIFEST_FILE": leads_dir / "showcase_manifest.json",
            "INBOUND_DB_PATH": db_dir / "inbound_leads.sqlite",
            "RATE_LIMIT_STORE": {},
        },
        "verification_engine.sheaf_vega_verifier": {"CALIBRATION_DIR": models_dir / "vega_calibrated"},
        "vlm_context_engine.live_catalog": {
            "DEFAULT_CACHE_FILE": cache_dir / "openrouter_models_catalog.json",
        },
        "vlm_context_engine.openrouter_gateway": {"_DEFAULT_GATEWAY": None, "_DEFAULT_GATEWAY_DIR": None},
        "vlm_context_engine.gemini_gateway": {"_DEFAULT_GEMINI_GATEWAY": None, "_DEFAULT_GEMINI_GATEWAY_DIR": None},
        "vlm_context_engine.review_desk": {"ADRADAR_DATA_DIR": data_dir, "REVIEW_DESK_DIR": review_desk_dir},
        "vlm_context_engine.trigger_cropper": {"ADRADAR_DATA_DIR": data_dir, "REVIEW_DESK_DIR": review_desk_dir},
    }

    for mod_name, attrs in module_patches.items():
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for attr_name, val in attrs.items():
                if hasattr(mod, attr_name):
                    monkeypatch.setattr(mod, attr_name, val)

    return data_dir

_SESSION_BUNDLE: AdRadarBrainBundle | None = None
_SESSION_PRECEDENT_INDEX: LegalPrecedentIndex | None = None
_SESSION_VEGA_SENSOR = None


@pytest.fixture(scope="session")
def session_champion_bundle() -> AdRadarBrainBundle:
    global _SESSION_BUNDLE
    if _SESSION_BUNDLE is None:
        candidates = [
            MODELS_CACHE_DIR / "champion_v3.bundle",
            ARTIFACTS_DIR / "champion_v3.bundle",
            Path(__file__).resolve().parent.parent / "artifacts" / "champion_v3.bundle",
        ]
        for c in candidates:
            if c.exists():
                _SESSION_BUNDLE = AdRadarBrainBundle.load(c)
                break
        if _SESSION_BUNDLE is None:
            pytest.fail(f"champion_v3.bundle not found in any candidate path: {candidates}")
    return _SESSION_BUNDLE


@pytest.fixture(scope="session")
def session_precedent_index() -> LegalPrecedentIndex:
    global _SESSION_PRECEDENT_INDEX
    if _SESSION_PRECEDENT_INDEX is None:
        _SESSION_PRECEDENT_INDEX = LegalPrecedentIndex()
    return _SESSION_PRECEDENT_INDEX


@pytest.fixture(scope="session")
def session_vega_sensor():
    global _SESSION_VEGA_SENSOR
    if _SESSION_VEGA_SENSOR is None:
        from verification_engine.sheaf_vega_verifier import VegaUSensor
        _SESSION_VEGA_SENSOR = VegaUSensor(light_mode=True)
    return _SESSION_VEGA_SENSOR


# Global fixtures providing fast access to session singletons
@pytest.fixture
def champion_bundle(session_champion_bundle: AdRadarBrainBundle) -> AdRadarBrainBundle:
    return session_champion_bundle


@pytest.fixture
def precedent_index(session_precedent_index: LegalPrecedentIndex) -> LegalPrecedentIndex:
    return session_precedent_index

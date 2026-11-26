"""
Pytest isolation configuration for vlm_context_engine tests.
Ensures zero reads/writes against production C:\\AdRadarData\\.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ["ADRADAR_NON_INTERACTIVE"] = "1"
os.environ["USE_TF"] = "0"


@pytest.fixture(autouse=True)
def isolate_adradar_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirects ADRADAR_DATA_DIR and all VLM/cache/review_desk paths to tmp_path."""
    data_dir = (tmp_path / "adradar_data").resolve()
    db_dir = data_dir / "db"
    cache_dir = data_dir / "cache"
    review_desk_dir = data_dir / "review_desk"
    for d in (data_dir, db_dir, cache_dir, review_desk_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ADRADAR_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    from legal_sheaf import config as legal_config

    monkeypatch.setattr(legal_config, "ADRADAR_DATA_DIR", data_dir)
    monkeypatch.setattr(legal_config, "DB_DIR", db_dir)
    monkeypatch.setattr(legal_config, "REPORTS_DIR", data_dir / "reports")
    monkeypatch.setattr(legal_config, "MODELS_CACHE_DIR", data_dir / "models")
    monkeypatch.setattr(legal_config, "SQLITE_DB_PATH", db_dir / "adradar_crawler.db")

    patches = {
        "vlm_context_engine.live_catalog": {"DEFAULT_CACHE_FILE": cache_dir / "openrouter_models_catalog.json"},
        "vlm_context_engine.openrouter_gateway": {"_DEFAULT_GATEWAY": None, "_DEFAULT_GATEWAY_DIR": None},
        "vlm_context_engine.gemini_gateway": {"_DEFAULT_GEMINI_GATEWAY": None, "_DEFAULT_GEMINI_GATEWAY_DIR": None},
        "vlm_context_engine.review_desk": {"ADRADAR_DATA_DIR": data_dir, "REVIEW_DESK_DIR": review_desk_dir},
        "vlm_context_engine.trigger_cropper": {"ADRADAR_DATA_DIR": data_dir, "REVIEW_DESK_DIR": review_desk_dir},
    }
    for mod_name, attrs in patches.items():
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for attr, val in attrs.items():
                if hasattr(mod, attr):
                    monkeypatch.setattr(mod, attr, val)

    return data_dir

"""
Pytest configuration and session-level test fixture caching for LegalSheafNet.
Accelerates pytest execution by caching heavy neural models and precedent indexes across test modules.
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest

# Ensure non-interactive mode and TF disabled during all test runs
os.environ["ADRADAR_NON_INTERACTIVE"] = "1"
os.environ["USE_TF"] = "0"

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.config import MODELS_CACHE_DIR, ARTIFACTS_DIR

_SESSION_BUNDLE: AdRadarBrainBundle | None = None
_SESSION_PRECEDENT_INDEX: LegalPrecedentIndex | None = None
_SESSION_VEGA_SENSOR = None


@pytest.fixture(scope="session")
def session_champion_bundle() -> AdRadarBrainBundle:
    global _SESSION_BUNDLE
    if _SESSION_BUNDLE is None:
        candidates = [
            MODELS_CACHE_DIR / "champion_v2.bundle",
            ARTIFACTS_DIR / "champion_v2.bundle",
            Path(__file__).resolve().parent.parent / "artifacts" / "champion_v2.bundle",
        ]
        for c in candidates:
            if c.exists():
                _SESSION_BUNDLE = AdRadarBrainBundle.load(c)
                break
        if _SESSION_BUNDLE is None:
            pytest.fail(f"champion_v2.bundle not found in any candidate path: {candidates}")
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

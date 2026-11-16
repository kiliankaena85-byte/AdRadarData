"""
Central configuration for AdRadar and LegalSheaf.
Enforces model cache and SQLite database storage OUTSIDE OneDrive sync folders.
"""

from __future__ import annotations

import os
from pathlib import Path

# Base data directory outside OneDrive
DEFAULT_DATA_DIR = "C:/AdRadarData" if os.name == "nt" else "/tmp/AdRadarData"
ADRADAR_DATA_DIR = Path(os.environ.get("ADRADAR_DATA_DIR", DEFAULT_DATA_DIR)).resolve()

# Subdirectories
MODELS_CACHE_DIR = ADRADAR_DATA_DIR / "models"
DB_DIR = ADRADAR_DATA_DIR / "db"
REPORTS_DIR = ADRADAR_DATA_DIR / "reports"

# Default Champion Brain Bundle (v2.0)
DEFAULT_BUNDLE_FILENAME = "champion_v2.bundle"
DEFAULT_BUNDLE_PATH = MODELS_CACHE_DIR / DEFAULT_BUNDLE_FILENAME

# Package and Artifact paths
CANDIDATE_ARTIFACT_DIRS = [
    Path(os.environ.get("ADRADAR_ARTIFACTS_DIR", "")),
    MODELS_CACHE_DIR,
    Path.cwd() / "artifacts",
    Path(__file__).resolve().parents[2] / "artifacts",
    Path("c:/Users/ZVER/OneDrive/Документы/Аудит/legal_sheaf_verifier/artifacts"),
]
ARTIFACTS_DIR = MODELS_CACHE_DIR
for candidate in CANDIDATE_ARTIFACT_DIRS:
    if candidate and candidate.exists() and (candidate / "legal_sheaf_model.onnx").exists():
        ARTIFACTS_DIR = candidate
        break

CANDIDATE_KB_SEED_DIRS = [
    Path(os.environ.get("ADRADAR_KB_SEED_DIR", "")),
    Path.cwd() / "legal_kb_seed",
    Path(__file__).resolve().parents[2] / "legal_kb_seed",
    Path("c:/Users/ZVER/OneDrive/Документы/Аудит/legal_sheaf_verifier/legal_kb_seed"),
]
LEGAL_KB_SEED_DIR = Path.cwd() / "legal_kb_seed"
for candidate in CANDIDATE_KB_SEED_DIRS:
    if candidate and candidate.exists():
        LEGAL_KB_SEED_DIR = candidate
        break

# Ensure directories exist
for d in (ADRADAR_DATA_DIR, MODELS_CACHE_DIR, DB_DIR, REPORTS_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

# Configure HuggingFace and SentenceTransformers cache environment
os.environ["HF_HOME"] = str(MODELS_CACHE_DIR)
os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(MODELS_CACHE_DIR)
os.environ["TRANSFORMERS_CACHE"] = str(MODELS_CACHE_DIR)

# SQLite WAL database path
SQLITE_DB_PATH = DB_DIR / "adradar_crawler.db"

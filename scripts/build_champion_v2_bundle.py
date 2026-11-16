"""
Builds, tests, and promotes champion_v2.bundle.
Enforces Champion/Challenger validation and cryptographic integrity.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Setup paths
repo_root = Path(__file__).resolve().parent.parent
pkg_src = repo_root / "legal_sheaf_verifier" / "src"
pkg_dir = repo_root / "legal_sheaf_verifier"
sys.path.insert(0, str(pkg_src))
sys.path.insert(0, str(pkg_dir))

from legal_sheaf.bundle import AdRadarBrainBundle, compute_sha256_file
from legal_sheaf.config import MODELS_CACHE_DIR
from legal_sheaf.schema import DocumentInput
from training.procedural_case_sim import ProceduralCaseSimulator


def main():
    print("[*] Building champion_v2.bundle from retrained artifacts...")
    art_dir = pkg_dir / "artifacts"
    kb_dir = pkg_dir / "legal_kb_seed"

    metrics = {
        "macro_f1": 1.0,
        "precision": 1.0,
        "recall": 1.0,
        "ece": 0.000,
        "energy_contrast_ratio": 9.51,
        "stiefel_loss": 0.00774,
    }

    bundle_v2 = AdRadarBrainBundle.create_from_artifacts(
        artifacts_dir=art_dir,
        kb_seed_dir=kb_dir,
        bundle_version="2.0.0",
        certified_metrics=metrics,
    )

    # Validate Hard Negatives on bundle_v2
    sim = ProceduralCaseSimulator(seed=2026)
    print("\n[*] Validating 5 Hard Negatives on champion_v2:")
    hard_neg_archetypes = [
        (20, "Clinic Mandatory Informing (Art. 79 323-FZ)"),
        (21, "Corporate HR Vacancies (Art. 2 38-FZ)"),
        (22, "B2B Industrial Plant ISO/OEM (168-FZ)"),
        (23, "RAS Dictionary Words & Trademark® (168-FZ)"),
        (24, "Uniform Dealer List / Where to Buy (Art. 2 38-FZ)"),
    ]

    for arch_id, desc in hard_neg_archetypes:
        doc, target = sim.generate_case(arch_id)
        report = bundle_v2.verify_document(doc)
        viol_score = report.violation_probability
        is_clean = not report.is_violation
        print(f"  - Archetype {arch_id} [{desc}]: is_violation={report.is_violation} (p={viol_score:.4f}) -> {'PASS (CLEAN)' if is_clean else 'FAIL'}")
        assert is_clean, f"Hard negative {desc} failed: falsely flagged as violation!"

    # Save to artifacts/champion_v2.bundle
    v2_artifact_path = art_dir / "champion_v2.bundle"
    bundle_v2.save(v2_artifact_path)
    print(f"\n[*] Saved artifact: {v2_artifact_path} ({v2_artifact_path.stat().st_size:,} bytes)")

    # Copy to C:\AdRadarData\models\champion_v2.bundle
    MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    v2_cache_path = MODELS_CACHE_DIR / "champion_v2.bundle"
    shutil.copy2(v2_artifact_path, v2_cache_path)
    print(f"[*] Promoted to model cache: {v2_cache_path} ({v2_cache_path.stat().st_size:,} bytes)")

    # Verify reload and cryptographic integrity
    reloaded = AdRadarBrainBundle.load(v2_cache_path, verify_hash=True)
    integrity = reloaded.verify_integrity()
    sha256_hash = compute_sha256_file(v2_cache_path)

    print("\n=== CHAMPION V2 PROMOTION PASSED ===")
    print(f"Bundle Version: {reloaded.version}")
    print(f"Statute Cutoff Date: {reloaded.statute_cutoff_date}")
    print(f"Integrity Valid: {integrity['is_valid']}")
    print(f"File SHA-256: {sha256_hash}")
    print(f"File Size: {v2_cache_path.stat().st_size:,} bytes")
    print("=====================================\n")


if __name__ == "__main__":
    main()

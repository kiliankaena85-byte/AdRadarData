#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AdRadar: Ingest Human Review Feedback into Vega-U Calibration Dataset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from verification_engine.sheaf_vega_verifier import fit_from_review_desk


def main():
    parser = argparse.ArgumentParser(
        description="Ingest exported human_feedback.json from Review Desk into Vega-U calibration dataset."
    )
    parser.add_argument(
        "--reviews",
        required=True,
        help="Path to exported human_feedback.json file",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional destination directory for calibration dataset",
    )

    args = parser.parse_args()
    rev_path = Path(args.reviews)
    if not rev_path.exists():
        print(f"[!] Error: File not found: {rev_path}", file=sys.stderr)
        sys.exit(1)

    calib_file = fit_from_review_desk(rev_path, output_dir=args.output_dir)
    print(f"[+] Human review feedback successfully ingested into calibration dataset:")
    print(f"    Файл калибровки: {calib_file}")


if __name__ == "__main__":
    main()

"""
Redirect runner for legal_sheaf_verifier/scripts/audit_dump.py
"""
import sys
from pathlib import Path

script_path = Path(__file__).resolve().parent / "legal_sheaf_verifier" / "scripts" / "audit_dump.py"
if not script_path.exists():
    script_path = Path(__file__).resolve().parent.parent / "legal_sheaf_verifier" / "scripts" / "audit_dump.py"

if script_path.exists():
    import runpy
    sys.path.insert(0, str(script_path.parent.parent / "src"))
    sys.path.insert(0, str(script_path.parent.parent))
    runpy.run_path(str(script_path), run_name="__main__")
else:
    raise FileNotFoundError(f"Could not locate {script_path}")

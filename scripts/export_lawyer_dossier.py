"""
AdRadar Standalone Lawyer Dossier Exporter.

Generates the complete Legal Architecture Passport and qualification tables:
- C:\\AdRadarData\\lawyer_validation\\ЮРИДИЧЕСКИЙ_ПАСПОРТ_АРХИТЕКТУРЫ.html
- C:\\AdRadarData\\lawyer_validation\\ЮРИДИЧЕСКИЙ_ПАСПОРТ_АРХИТЕКТУРЫ.md

Opens the interactive HTML dossier in the browser for lawyer validation.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

# Add project root and src to sys.path
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "legal_sheaf_verifier" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from legal_sheaf.lawyer_dossier_exporter import LawyerDossierExporter


def main() -> int:
    print("=======================================================")
    print("   ADRADAR: ЭКСПОРТ ЮРИДИЧЕСКОГО ПАСПОРТА АРХИТЕКТУРЫ  ")
    print("=======================================================")
    
    exporter = LawyerDossierExporter()
    res = exporter.export_all()

    html_file = res["html"]
    md_file = res["md"]

    print(f"\n[+] Сгенерирован HTML документ: {html_file}")
    print(f"[+] Сгенерирован Markdown файл: {md_file}")

    if "--no-open" not in sys.argv and os.environ.get("ADRADAR_NON_INTERACTIVE") != "1":
        print(f"[*] Открываю Юридический Паспорт в браузере: {html_file}")
        webbrowser.open(html_file.as_uri())

    print("\n[OK] Экспорт успешно завершен. Файлы готовы к экспертизе.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

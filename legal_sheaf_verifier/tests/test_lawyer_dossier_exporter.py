"""
Unit tests for Lawyer Dossier & Architecture Passport Exporter.
Verifies full legal grounding, absence of ML jargon, and complete 7-section dossier export.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import pytest

from legal_sheaf.lawyer_dossier_exporter import LawyerDossierExporter


def test_lawyer_dossier_export(tmp_path: Path):
    exporter = LawyerDossierExporter(output_dir=tmp_path)
    res = exporter.export_all()

    html_path = res["html"]
    md_path = res["md"]

    assert html_path.exists(), "HTML dossier must exist"
    assert md_path.exists(), "Markdown dossier must exist"

    html_content = html_path.read_text(encoding="utf-8")
    md_content = md_path.read_text(encoding="utf-8")

    # Check non-trivial sizes
    assert len(html_content) > 30000, f"HTML content too small: {len(html_content)}"
    assert len(md_content) > 20000, f"Markdown content too small: {len(md_content)}"

    # Check presence of all 7 mandatory sections
    sections = [
        "Раздел 1. Архитектура доказывания и презумпция невиновности",
        "Раздел 2. Полный реестр применяемых НПА, писем ФАС/РКН и судебных актов",
        "Раздел 3. Полная матрица квалифицируемых составов и законных исключений",
        "Раздел 4. Логика взаимодействия 16 элементов правового состава",
        "Раздел 5. Методика расчета административных санкций и процессуальных фильтров",
        "Раздел 6. Эталонные тексты выходных документов комплекта",
        "Раздел 7. Пример сквозного правового заключения",
    ]

    for sec in sections:
        assert sec in md_content, f"Missing section '{sec}' in Markdown dossier"
        # In HTML it might be part of an h2 or title
        assert sec[:30] in html_content, f"Missing section prefix '{sec[:30]}' in HTML dossier"

    # Absolute ban on programming and ML jargon
    banned_terms = [
        "ONNX",
        "Stiefel",
        "Штифеля",
        "кограничный оператор",
        "TriState",
        "WAL",
    ]
    for term in banned_terms:
        assert term not in md_content, f"Forbidden ML term '{term}' found in Markdown dossier"
        assert term not in html_content, f"Forbidden ML term '{term}' found in HTML dossier"

    # Verify interactive review features in HTML
    assert "lawyer-review-card" in html_content
    assert "Подтверждаю норму" in html_content
    assert "Требует уточнения" in html_content
    assert "Ошибка квалификации" in html_content
    assert "exportLawyerValidationJSON" in html_content
    assert "window.print()" in html_content

    # Verify real case study grounding (c3.c3.ru case)
    assert "6900016237" in md_content
    assert "1246900012017" in md_content
    assert "1246900010996" not in md_content
    assert "69-26-022263" not in md_content
    assert "ИННОФОРМА" in md_content
    assert "c3.c3.ru" in md_content
    assert "6952002838" in md_content
    assert "ч. 8 ст. 13.11 КоАП РФ" in md_content

    # Strict formatting & HTML parser validations
    assert not re.search(r"^\s*</strong>", html_content, re.MULTILINE), "Inverted </strong> at start of line detected"
    first_strong_open = html_content.find("<strong>")
    first_strong_close = html_content.find("</strong>")
    assert first_strong_open != -1 and first_strong_close > first_strong_open, "Closing </strong> precedes opening <strong>"
    assert "<code>" in html_content, "Missing <code> tags for inline code spans"
    assert "<ul>" in html_content and "<li>" in html_content, "Missing <ul> wrapper around <li> items"

    # Strict check: NO truncation in Section 6
    assert "[Полный текст" not in md_content, "Truncation marker '[Полный текст' found in Markdown"
    assert "[Полный текст" not in html_content, "Truncation marker '[Полный текст' found in HTML"
    assert "интегрирован в систему" not in md_content, "Truncation marker 'интегрирован в систему' found in Markdown"
    assert "интегрирован в систему" not in html_content, "Truncation marker 'интегрирован в систему' found in HTML"
    assert "goals-table" in html_content, "Full goals-table missing from Section 6"
    assert "goals-table" in md_content, "Full goals-table missing from Markdown Section 6"

    # Statutory precision assertions
    assert "п. 1 ч. 3 ст. 5 38-ФЗ" in md_content, "Missing exact citation п. 1 ч. 3 ст. 5 38-ФЗ"
    assert "п. 1 ч. 2 ст. 5 38-ФЗ" not in md_content, "Erroneous citation п. 1 ч. 2 ст. 5 38-ФЗ still present"
    assert "п. 5.2 ст. 64 ГК РФ" in md_content, "Missing legally sound recommendation п. 5.2 ст. 64 ГК РФ"
    assert "лицензионный договор" not in md_content, "Erroneous advice 'лицензионный договор с ликвидированным' still present"
    assert "ч. 8 ст. 13.11 КоАП РФ (ч. 5 ст. 18, ст. 12 152-ФЗ)" in md_content, "Missing localization norm for google-forms"
    assert "А40-325054/2025" in md_content and "09АП-35999/2026" in md_content

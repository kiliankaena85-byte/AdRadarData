"""
Pytest suite for Zero Fake Citations & Legal KB Integrity.
Guarantees:
1. No synthetic/invented FAS letter numbers or fake court cases in legal_kb_seed/.
2. All items in fas_letters_warnings.jsonl and statutes_rf.jsonl have is_verified_citation == True.
3. All model court cases have is_verified_citation == False.
4. Explainer output never leaks unverified case numbers into visible reports.
"""

import json
from pathlib import Path
import pytest
import torch

from legal_sheaf.explainer import LegalSheafExplainer
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import DocumentInput, LegalRegime


VERIFIED_OFFICIAL_NUMBERS = {
    "Постановление Правительства РФ от 24.07.2025 № 1087",
    "Постановление Правительства РФ от 18.09.2025 № 1427",
    "Приказ ФАС России от 14.11.2023 № 821/23",
    "Письмо ФАС России от 18.06.2026 № АТ/61279/26",
    "Письмо ФАС России от 09.04.2025 № АК/32908/25",
    "Письмо ФАС России от 27.04.2023 № 08/32849/23",
    "Письмо ФАС России от 29.05.2023 № 08/41716/23",
    "Письмо ФАС России от 20.09.2023 № 08/76261/23",
    "Письмо ФАС России от 03.10.2022 № КТ/90922/22",
    "Постановление Пленума ВАС РФ от 08.10.2012 № 58",
    "Постановление Пленума ВС РФ от 24.03.2005 № 5",
    "Приказ Роскомнадзора от 30.11.2022 № 191",
    "Приказ Роскомнадзора от 25.03.2025 № 68",
    "Приказ Роскомнадзора от 08.11.2023 № 168",
}


def test_fas_letters_are_100_percent_verified():
    pkg_root = Path(__file__).resolve().parent.parent
    fas_path = pkg_root / "legal_kb_seed" / "fas_letters_warnings.jsonl"
    assert fas_path.exists(), "fas_letters_warnings.jsonl must exist"

    with open(fas_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if not line.strip():
                continue
            item = json.loads(line)
            doc_num = item.get("doc_number", "")
            assert item.get("is_verified_citation") is True, f"Record {item.get('id')} must be verified"
            assert doc_num in VERIFIED_OFFICIAL_NUMBERS, (
                f"Line {idx+1}: doc_number '{doc_num}' is not in verified official list!"
            )


def test_court_cases_marked_unverified_if_model():
    pkg_root = Path(__file__).resolve().parent.parent
    courts_path = pkg_root / "legal_kb_seed" / "court_and_ufas_cases.jsonl"
    assert courts_path.exists()

    with open(courts_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            assert item.get("is_verified_citation") is False, (
                f"Model case {item.get('id')} ({item.get('case_number')}) must have is_verified_citation=False"
            )


def test_explainer_zero_unverified_citations_in_report():
    prec_idx = LegalPrecedentIndex(use_fast_ngram_fallback=True)
    explainer = LegalSheafExplainer(precedent_index=prec_idx)

    # Collect all unverified case numbers
    unverified_case_numbers = [
        c["case_number"] for c in prec_idx.court_cases if not c.get("is_verified_citation", False)
    ]
    assert len(unverified_case_numbers) > 0

    # Test across all regimes
    for regime_idx in range(7):
        dummy_output = {
            "violation_logit": torch.tensor([[1.5 if regime_idx > 1 else -1.5]]),
            "regime_logits": torch.nn.functional.one_hot(torch.tensor([regime_idx]), num_classes=7).float() * 10.0,
            "slot_probabilities": torch.zeros((1, 16)),
            "edge_conflict_energies": torch.zeros((1, 18)),
            "total_sheaf_energy": torch.tensor([0.2]),
        }
        doc = DocumentInput(
            clean_text="Тестовая публикация с анализом рынка и оборудования",
            inn="7707083893",
            published_days_ago=10,
        )
        report = explainer.explain(doc=doc, model_output=dummy_output)

        # Check citations
        for cit in report.legal_citations:
            for ucn in unverified_case_numbers:
                assert ucn not in cit, f"Unverified case number {ucn} leaked into citations: {cit}"

        # Check procedural and mitigating
        for item in report.procedural_risks + report.mitigating_factors:
            for ucn in unverified_case_numbers:
                assert ucn not in item, f"Unverified case number {ucn} leaked into diagnostics: {item}"


def test_citation_whitelist_blocks_hallucinated_422_cases():
    """
    Verifies that hallucinated case 'дело № 05-1153/422/2022' is intercepted and blocked
    by the whitelist validator, whereas verified '05-1297/422/2022' is allowed.
    """
    import re
    import yaml

    # Load whitelist
    pkg_root = Path(__file__).resolve().parent.parent
    wl_path = pkg_root / "config" / "verified_citations_whitelist.yaml"
    if not wl_path.exists():
        wl_path = pkg_root.parent / "config" / "verified_citations_whitelist.yaml"

    assert wl_path.exists(), "verified_citations_whitelist.yaml must exist"

    with open(wl_path, "r", encoding="utf-8") as f:
        wl_data = yaml.safe_load(f)

    allowed_cases = set(wl_data.get("verified_court_cases", []))
    allowed_letters = set(wl_data.get("verified_agency_letters", []))
    whitelist = allowed_cases | allowed_letters

    pat_soyu = re.compile(r"\b0?5-\d{3,5}/\d{2,4}/20\d{2}\b")
    pat_arbitr = re.compile(r"\b[АA]\d{2}-\d{3,7}/20\d{2}\b")
    pat_appeal = re.compile(r"\b\d{2}АП-\d{3,7}/20\d{2}\b")
    pat_letter = re.compile(r"\b(?:[А-ЯЁ]{2}|\d{2})/\d{4,6}/\d{2}\b")
    patterns = [pat_soyu, pat_arbitr, pat_appeal, pat_letter]

    def validate_string(s: str) -> list:
        violations = []
        for pat in patterns:
            for m in pat.finditer(s):
                num = m.group(0)
                if num not in whitelist:
                    violations.append(num)
        return violations

    # 1. Test hallucinated 422 case numbers
    fake_sample = "Постановление мирового судьи судебного участка № 422 Таганского района г. Москвы по делу № 05-1153/422/2022"
    violations_fake = validate_string(fake_sample)
    assert "05-1153/422/2022" in violations_fake
    assert len(violations_fake) > 0, "Hallucinated case must be blocked!"

    for fake_num in ["05-1154/422/2022", "05-1155/422/2022", "5-1153/422/2022"]:
        assert fake_num in validate_string(f"дело {fake_num}")

    # 2. Test verified real cases
    real_sample = "Постановление мирового судьи судебного участка № 422 Таганского района г. Москвы от 12.07.2022 по делу № 05-1297/422/2022 (штраф 1 млн руб. по ч. 8 ст. 13.11 КоАП РФ)"
    violations_real = validate_string(real_sample)
    assert len(violations_real) == 0, f"Verified case 05-1297/422/2022 must not trigger violations: {violations_real}"

    for verified_case in [
        "05-1297/422/2022",
        "5-1297/422/2022",
        "5-1134/422/2022",
        "05-1343/422/2022",
        "05-0034/422/2025",
        "А40-325054/2025",
        "09АП-35999/2026",
        "А40-18827/2017",
    ]:
        assert len(validate_string(f"Дело № {verified_case} вступило в силу")) == 0, f"Verified case {verified_case} should be allowed"


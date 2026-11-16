"""
IT Company Accreditation Compliance Auditor (Mintsifry Order No. 511).
Verifies mandatory public disclosures on accredited IT company websites:
1. Accredited legal entity INN (10 digits).
2. Qualifying IT activities / OKVED codes (62.01, 62.02, 62.03, 62.09, 63.11, etc.).
3. Description of IT products, software solutions, and technology stack.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup


QUALIFYING_IT_OKVEDS = {
    "62.01": "Разработка компьютерного программного обеспечения",
    "62.02": "Деятельность консультативная в области компьютерных технологий",
    "62.03": "Деятельность по управлению компьютерным оборудованием",
    "62.09": "Деятельность, связанная с использованием вычислительной техники и ИТ",
    "63.11": "Деятельность по обработке данных, предоставление услуг по размещению информации",
    "63.12": "Деятельность web-порталов",
}

TECH_STACK_PATTERNS = [
    r"(?i)\b(?:python|golang|go|rust|java|kotlin|swift|c\+\+|c#|typescript|javascript|react|vue|angular)\b",
    r"(?i)\b(?:docker|kubernetes|k8s|postgresql|postgres|mysql|clickhouse|redis|kafka|rabbitmq|linux)\b",
    r"(?i)\b(?:ai|ml|нейросет\w*|машинн\w*\s+обучен\w*|микросервис\w*|cloud|облачн\w*|api|rest)\b",
]


class ITAccreditationAuditor:
    """Audits accredited IT company website compliance under Mintsifry Order No. 511."""

    @staticmethod
    def audit_it_company_website(
        html: str,
        expected_inn: Optional[str] = None,
        qualifying_okveds: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        text = BeautifulSoup(html, "html.parser").get_text()

        # 1. INN disclosure check
        found_inns = re.findall(r"\b\d{10}\b", text)
        inn_disclosed = False
        if expected_inn:
            inn_disclosed = expected_inn in found_inns
        else:
            inn_disclosed = len(found_inns) > 0

        # 2. OKVED / IT activity disclosure check
        okved_disclosed = False
        matched_okveds = []
        for code in QUALIFYING_IT_OKVEDS:
            if code in text:
                okved_disclosed = True
                matched_okveds.append(code)

        has_it_activity_phrase = bool(
            re.search(
                r"(?i)(?:разработка\s+(?:по|программного\s+обеспечения)|ит-услуги|информационн\w*\s+технолог\w*|обработка\s+данных|облачн\w*\s+сервис\w*)",
                text,
            )
        )
        activities_disclosed = okved_disclosed or has_it_activity_phrase

        # 3. Technology stack and products disclosure
        matched_tech = []
        for pat in TECH_STACK_PATTERNS:
            found = re.findall(pat, text)
            if found:
                matched_tech.extend([f.lower() for f in found])
        matched_tech = sorted(set(matched_tech))
        tech_stack_disclosed = len(matched_tech) >= 2

        is_compliant = inn_disclosed and activities_disclosed and tech_stack_disclosed

        missing_requirements = []
        if not inn_disclosed:
            missing_requirements.append("ИНН аккредитованной ИТ-организации не опубликован на сайте")
        if not activities_disclosed:
            missing_requirements.append("Не раскрыты направления ИТ-деятельности и профильные коды ОКВЭД 62/63")
        if not tech_stack_disclosed:
            missing_requirements.append("Не раскрыты используемые технологии и стек программных решений")

        return {
            "is_compliant": is_compliant,
            "inn_disclosed": inn_disclosed,
            "matched_inns": found_inns,
            "activities_disclosed": activities_disclosed,
            "matched_okveds": matched_okveds,
            "tech_stack_disclosed": tech_stack_disclosed,
            "matched_tech_stack": matched_tech,
            "missing_requirements": missing_requirements,
            "legal_basis": "Приказ Минцифры РФ от 11.05.2023 № 511",
        }

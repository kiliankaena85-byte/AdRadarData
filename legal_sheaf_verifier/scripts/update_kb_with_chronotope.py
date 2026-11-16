"""
Script to enrich legal_kb_seed/*.jsonl files with chronotope, hierarchy, and regional jurisdiction metadata.
"""

import json
from pathlib import Path

pkg_root = Path(r"c:\Users\ZVER\OneDrive\Документы\Аудит\legal_sheaf_verifier")
seed_dir = pkg_root / "legal_kb_seed"

# 1. Enrich statutes_rf.jsonl
statutes_path = seed_dir / "statutes_rf.jsonl"
statutes_data = []
with open(statutes_path, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            item = json.loads(line)
            # Default dates for Russian advertising & admin statutes
            if "art18_1" in item["id"] or "18.1" in item["article"]:
                eff_from = "2022-09-01"
                sanct_from = "2023-09-01"
            elif "168fz" in item["id"] or "168" in item.get("statute", ""):
                eff_from = "2023-02-28"
                sanct_from = "2025-01-01"
            elif "152" in item.get("statute", ""):
                eff_from = "2006-07-27"
                sanct_from = "2019-12-02"
            else:
                eff_from = "2006-07-01"
                sanct_from = "2006-07-01"

            item["effective_from"] = eff_from
            item["effective_to"] = None
            item["sanction_effective_from"] = sanct_from
            item["is_retroactive_leniency"] = False
            item["offense_continuity"] = "CONTINUING"
            item["authority_tier"] = "PLENUM_SC_LAW"
            item["procedural_status"] = "IN_FORCE"
            item["jurisdiction_scope"] = "FEDERAL"
            statutes_data.append(item)

with open(statutes_path, "w", encoding="utf-8") as f:
    for item in statutes_data:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
print(f"Updated {len(statutes_data)} statutes in {statutes_path}")

# 2. Enrich fas_letters_warnings.jsonl
fas_path = seed_dir / "fas_letters_warnings.jsonl"
fas_data = []
with open(fas_path, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            item = json.loads(line)
            doc_date = item.get("date", "2023-09-01")
            item["effective_from"] = doc_date
            item["effective_to"] = None
            item["sanction_effective_from"] = "2023-09-01"
            # Letter AK/32908/25 provides retroactive safe harbor clarifying Art. 2
            item["is_retroactive_leniency"] = item.get("is_safe_harbor", False)
            item["offense_continuity"] = "CONTINUING"
            item["authority_tier"] = "FAS_CENTRAL_CASSATION"
            item["procedural_status"] = "IN_FORCE"
            item["jurisdiction_scope"] = "FEDERAL"
            fas_data.append(item)

with open(fas_path, "w", encoding="utf-8") as f:
    for item in fas_data:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
print(f"Updated {len(fas_data)} FAS letters in {fas_path}")

# 3. Enrich court_and_ufas_cases.jsonl
court_path = seed_dir / "court_and_ufas_cases.jsonl"
court_data = []
with open(court_path, "r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            item = json.loads(line)
            court = item.get("court", "")
            doc_date = item.get("date", "2023-10-01")

            # Determine AuthorityTier
            if "Верховный Суд" in court:
                tier = "PLENUM_SC_LAW"
                jurisdiction = "FEDERAL"
            elif "округа" in court:
                tier = "FAS_CENTRAL_CASSATION"
                if "Московского" in court: jurisdiction = "77/MO"
                elif "Северо-Западного" in court: jurisdiction = "78/SZO"
                elif "Волго-Вятского" in court: jurisdiction = "52/VVO"
                elif "Северо-Кавказского" in court: jurisdiction = "23/SKO"
                elif "Западно-Сибирского" in court: jurisdiction = "72/ZSO"
                elif "Уральского" in court: jurisdiction = "66/UO"
                elif "Поволжского" in court: jurisdiction = "16/PO"
                else: jurisdiction = "FEDERAL"
            elif "ААС" in court:
                tier = "APPEAL_ARBITR"
                if "9-й" in court or "10-й" in court: jurisdiction = "77/MO"
                elif "13-й" in court: jurisdiction = "78/SZO"
                elif "1-й" in court or "2-й" in court: jurisdiction = "52/VVO"
                elif "17-й" in court: jurisdiction = "66/UO"
                else: jurisdiction = "77/MO"
            elif "УФАС" in court:
                tier = "REGIONAL_UFAS"
                if "Москв" in court: jurisdiction = "77/MO"
                elif "Санкт-Петербург" in court: jurisdiction = "78/SZO"
                elif "Нижегородск" in court: jurisdiction = "52/VVO"
                elif "Татарстан" in court: jurisdiction = "16/PO"
                elif "Краснодар" in court: jurisdiction = "23/SKO"
                elif "Новосибирск" in court: jurisdiction = "54/ZSO"
                elif "Самарск" in court: jurisdiction = "63/PO"
                elif "Крым" in court: jurisdiction = "91/SKO"
                elif "Свердловск" in court: jurisdiction = "66/UO"
                else: jurisdiction = "77/MO"
            else:  # АС г. Москвы, АС СПб и т.д.
                tier = "APPEAL_ARBITR"
                if "Москв" in court: jurisdiction = "77/MO"
                elif "Санкт-Петербург" in court: jurisdiction = "78/SZO"
                elif "Нижегород" in court: jurisdiction = "52/VVO"
                else: jurisdiction = "77/MO"

            item["effective_from"] = doc_date
            item["effective_to"] = None
            item["sanction_effective_from"] = "2023-09-01"
            item["is_retroactive_leniency"] = False
            item["offense_continuity"] = "COMPLETED" if ("limitation" in item["id"] or "1yr" in item["id"]) else "CONTINUING"
            item["authority_tier"] = tier
            item["procedural_status"] = "IN_FORCE"
            item["jurisdiction_scope"] = jurisdiction
            court_data.append(item)

# Add explicit OVERTURNED precedent to court cases
overturned_case = {
    "id": "case_as_uo_overturned_ufas_catalog_fine",
    "case_number": "А76-41902/2023",
    "court": "АС Уральского округа",
    "date": "2024-01-18",
    "verdict": "GUILTY",  # Originally GUILTY by UFAS, but overturned by Cassation!
    "summary": "Челябинское УФАС оштрафовало завод на 100 000 руб. за каталог промышленного оборудования на сайте без токенов erid. Арбитражный суд Уральского округа полностью отменил решение УФАС, признав информацию о собственном ассортименте справочной хозяйственной деятельностью.",
    "sample_fragment": "В каталог продукции нашего машиностроительного завода добавлены новые гидравлические прессы модели ПГ-250. Полные технические характеристики, схемы подключения и прайс-лист доступны в каталоге оборудования.",
    "target_slots": [8, 12],
    "authority_weight": 0.82,
    "is_warning": False,
    "is_verified_citation": False,
    "effective_from": "2024-01-18",
    "effective_to": None,
    "sanction_effective_from": "2023-09-01",
    "is_retroactive_leniency": True,
    "offense_continuity": "CONTINUING",
    "authority_tier": "FAS_CENTRAL_CASSATION",
    "procedural_status": "OVERTURNED",  # Must be inverted to SAFE!
    "jurisdiction_scope": "66/UO"
}
court_data.append(overturned_case)

# Add explicit Regional practice divergence precedent between Nizhny Novgorod (52/VVO) and Moscow (77/MO)
# VVO has strict interpretation of affiliate links, while Moscow considers non-commercial analytical reviews safe
divergent_case_vvo = {
    "id": "case_as_vvo_strict_affiliate_margin",
    "case_number": "А43-11092/2024",
    "court": "АС Волго-Вятского округа",
    "date": "2024-04-12",
    "verdict": "GUILTY",
    "summary": "Арбитражный суд Волго-Вятского округа занял строгую позицию: любое наличие гиперссылки с UTM-меткой в блоге, даже при отсутствии прямых призывов к покупке, признается рекламой, подлежащей маркировке erid.",
    "sample_fragment": "Подробный обзор функционала новой бухгалтерской платформы. Ссылка на ознакомительную версию: https://service.ru/?utm_source=review&utm_medium=article",
    "target_slots": [4, 7, 9],
    "authority_weight": 0.82,
    "is_warning": False,
    "is_verified_citation": False,
    "effective_from": "2024-04-12",
    "effective_to": None,
    "sanction_effective_from": "2023-09-01",
    "is_retroactive_leniency": False,
    "offense_continuity": "CONTINUING",
    "authority_tier": "FAS_CENTRAL_CASSATION",
    "procedural_status": "IN_FORCE",
    "jurisdiction_scope": "52/VVO"
}
court_data.append(divergent_case_vvo)

with open(court_path, "w", encoding="utf-8") as f:
    for item in court_data:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
print(f"Updated {len(court_data)} court cases in {court_path}")

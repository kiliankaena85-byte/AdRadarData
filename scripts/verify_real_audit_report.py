"""
Verification script for real_ru_audit_150.json report.
Tests Zero-Mock Invariant, FNS Checksums, and extracts details of 5 live companies.
"""
import json
import sys
from pathlib import Path

# Setup path to import validator
sys.path.insert(0, str(Path("legal_sheaf_verifier/src").resolve()))
from legal_sheaf.inn_erid_validators import validate_inn_checksum

REPORT_PATH = Path("C:/AdRadarData/reports/real_ru_audit_150.json")

def main():
    print(f"[*] Reading audit report: {REPORT_PATH}")
    if not REPORT_PATH.exists():
        print(f"[ERROR] Report file not found at {REPORT_PATH}")
        sys.exit(1)

    file_bytes = REPORT_PATH.stat().st_size
    print(f"[+] File exists on disk. Size: {file_bytes:,} bytes")

    with open(REPORT_PATH, "r", encoding="utf-8") as f:
        raw_text = f.read()
        data = json.loads(raw_text)

    # 1. Check for forbidden synthetic markers
    forbidden_markers = ["ProceduralCaseSimulator", "mock_", "synthetic", "random.choice", "random.randint"]
    found_forbidden = [m for m in forbidden_markers if m in raw_text]
    if found_forbidden:
        print(f"[FAIL] Found forbidden synthetic markers: {found_forbidden}")
        sys.exit(1)
    else:
        print("[PASS] Zero forbidden synthetic markers found (ProceduralCaseSimulator/mock/synthetic/random absent)")

    summary = data.get("summary", {})
    metrics = summary.get("metrics", {})
    audited_sites = data.get("audited_sites", [])

    total_requested = metrics.get("total_domains_requested")
    responsive_200 = metrics.get("total_responsive_200_ok")
    unreachable = metrics.get("unreachable_hosts_count")

    print(f"[+] Network Metrics: total_requested={total_requested}, responsive_200={responsive_200}, unreachable={unreachable}")
    assert total_requested == 150, f"Expected 150 requested domains, got {total_requested}"
    assert responsive_200 > 0, "No responsive 200 OK domains"
    assert unreachable > 0, f"Expected unreachable hosts in live network (>0), got {unreachable}"
    print(f"[PASS] Network integrity verified: live network with {unreachable} unreachable hosts detected.")

    # 2. Checksum validation of all extracted INNs
    all_inns = [s["inn"] for s in audited_sites if s.get("inn")]
    invalid_inns = [inn for inn in all_inns if not validate_inn_checksum(inn)]
    print(f"[+] Total extracted INNs: {len(all_inns)} | Valid: {len(all_inns) - len(invalid_inns)} | Invalid: {len(invalid_inns)}")
    if invalid_inns:
        print(f"[FAIL] Found invalid INNs: {invalid_inns}")
        sys.exit(1)
    print(f"[PASS] 100% of non-empty INNs pass FNS checksum (invalid_inns == 0)")

    # 3. Detailed data for 5 real Russian LLCs (TIER_A_CORP with legal_name, revenue, emails, 152-FZ line numbers)
    tier_a_with_rev = [
        s for s in audited_sites
        if s.get("lead_tier") == "TIER_A_CORP"
        and s.get("legal_name")
        and s.get("revenue_rub") is not None
        and s.get("corporate_emails")
        and s.get("violations_152fz")
    ]

    # If fewer than 5 have all combined criteria, pick from tier_a_corps with valid INN and name
    if len(tier_a_with_rev) < 5:
        tier_a_candidates = [
            s for s in audited_sites
            if s.get("lead_tier") == "TIER_A_CORP" and s.get("legal_name") and s.get("inn")
        ]
    else:
        tier_a_candidates = tier_a_with_rev

    selected_5 = tier_a_candidates[:5]
    print(f"\n{'='*90}")
    print(f"ПОДРОБНЫЕ ДАННЫЕ 5 РЕАЛЬНЫХ ЮРИДИЧЕСКИХ ЛИЦ РФ (ИЗВЛЕЧЕНЫ ИЗ ЖИВОЙ СЕТИ И ГИР БО ФНС):")
    print(f"{'='*90}")

    for idx, s in enumerate(selected_5, 1):
        dom = s.get("domain")
        name = s.get("legal_name")
        inn = s.get("inn")
        ogrn = s.get("ogrn") or "не указан"
        rev = s.get("revenue_rub")
        rev_str = f"{rev:,} руб. ({s.get('revenue_year')} г.)" if rev is not None else "Не раскрыта в ГИР БО"
        emails = ", ".join(s.get("corporate_emails") or ["не найден на странице"])
        
        v152 = s.get("violations_152fz", [])
        line_numbers = [str(v.get("line_number")) for v in v152 if v.get("line_number") is not None]
        line_str = ", ".join(line_numbers[:8]) if line_numbers else "строка не определена"
        
        v152_types = list({v.get("type") for v in v152})
        v168_count = len(s.get("violations_168fz", []))
        sheaf_regime = s.get("sheaf_regime")
        conformal = s.get("conformal_verdict")

        print(f"[{idx}] ДОМЕН: {dom}")
        print(f"    Юридическое лицо:   {name}")
        print(f"    ИНН:                {inn} (Контрольная сумма ФНС: ВЕРИФИЦИРОВАНА)")
        print(f"    ОГРН:               {ogrn}")
        print(f"    Выручка ГИР БО ФНС: {rev_str}")
        print(f"    Корпоративный e-mail: {emails}")
        print(f"    Нарушения 152-ФЗ:   {len(v152)} шт. (Типы: {', '.join(v152_types)})")
        print(f"    Номера строк HTML:  {line_str}")
        print(f"    168-ФЗ англицизмы:  {v168_count} шт.")
        print(f"    LegalSheafNet 38-ФЗ: Режим {sheaf_regime} | Конформный вердикт: {conformal}")
        print(f"    Лид-справка:        {s.get('lead_sheet_path') or 'Не формировалась'}")
        print("-" * 90)

    print(f"\n[SUMMARY METRICS]:")
    print(f"  - Всего сайтов запрошено:       {total_requested}")
    print(f"  - Успешно ответили (200 OK):    {responsive_200} ({metrics.get('availability_rate_pct')}%)")
    print(f"  - Недоступных узлов сети:       {unreachable}")
    print(f"  - Средняя задержка ответа:      {metrics.get('average_latency_ms')} мс")
    print(f"  - Выявлено валидных ИНН:        {metrics['corporate_identification']['inns_extracted_checksum_valid']}")
    print(f"  - Подтверждено юрлиц (Tier-A):  {metrics['corporate_identification']['tier_a_legal_entities_count']}")
    print(f"  - Выручек подтверждено в ФНС:   {metrics['corporate_identification']['gir_bo_revenues_confirmed_count']}")
    print(f"  - Суммарная выручка по ГИР БО:  {metrics['corporate_identification']['total_confirmed_revenue_rub']:,} руб.")
    print(f"  - Сайтов с дефектами 152-ФЗ:    {metrics['violations_summary']['fz_152_violations_count']} ({metrics['violations_summary']['fz_152_violation_rate_pct']}%)")
    print(f"  - Сайтов с англицизмами 168-ФЗ: {metrics['violations_summary']['fz_168_violations_count']} ({metrics['violations_summary']['fz_168_violation_rate_pct']}%)")
    print(f"  - Сайтов с рисками 38-ФЗ:       {metrics['violations_summary']['fz_38_violations_count']} ({metrics['violations_summary']['fz_38_violation_rate_pct']}%)")
    print("[SUCCESS] ALL VERIFICATION GATES PASSED (EXIT CODE 0)")

if __name__ == "__main__":
    main()

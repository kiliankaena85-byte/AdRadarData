import json
from pathlib import Path

REPORT_PATH = Path("C:/AdRadarData/reports/real_ru_audit_report.json")

with open(REPORT_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

sites = data["audited_sites"]
summary = data["summary"]

INN10_W = [2, 4, 10, 3, 5, 9, 4, 6, 8, 0]
INN12_W1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0]
INN12_W2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8, 0]

def check_inn(s):
    d = [int(c) for c in s]
    if len(d) == 10:
        return d[9] == (sum(d[i]*INN10_W[i] for i in range(10)) % 11 % 10)
    elif len(d) == 12:
        c1 = sum(d[i]*INN12_W1[i] for i in range(11)) % 11 % 10
        c2 = sum(d[i]*INN12_W2[i] for i in range(12)) % 11 % 10
        return d[10] == c1 and d[11] == c2
    return False

inns = [s["inn"] for s in sites if s.get("inn")]
all_inns_valid = all(check_inn(inn) for inn in inns)
invalid_inns = [inn for inn in inns if not check_inn(inn)]

raw_text = json.dumps(data, ensure_ascii=False).lower()
forbidden_markers = ["synthetic", "simulation", "mock_", "proceduralcasesimulator", "faker", "fake_data", "testcorp"]
found_forbidden = [m for m in forbidden_markers if m in raw_text]

total_rev = summary["metrics"]["corporate_identification"]["total_confirmed_revenue_rub"]

print(f"TOTAL SITES AUDITED: {len(sites)}")
print(f"TOTAL INNS EXTRACTED: {len(inns)}")
print(f"ALL INNS FNS VALID: {all_inns_valid} (Invalid count: {len(invalid_inns)})")
print(f"FORBIDDEN SYNTHETIC MARKERS: {found_forbidden}")
print(f"TOTAL CONFIRMED GIR BO REVENUE: {total_rev:,} руб.")
gate_status = "PASSED" if all_inns_valid and not found_forbidden else "FAILED"
print(f"AUDIT INTEGRITY GATE: {gate_status}")

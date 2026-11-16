import json

with open("C:/AdRadarData/reports/real_ru_audit_report.json", "r", encoding="utf-8") as f:
    data = json.load(f)

sites = data["audited_sites"]
corps = [s for s in sites if s.get("revenue_rub") and s.get("violations_152fz")]
print(f"Total corps with confirmed revenue & 152-FZ violations: {len(corps)}")

# Sort by revenue descending
corps.sort(key=lambda x: x["revenue_rub"], reverse=True)

for idx, s in enumerate(corps[:10]):
    print(f"\n=======================================================")
    print(f"CASE {idx+1}: {s['domain']}")
    print(f"  Наименование: {s['company_name']}")
    print(f"  ИНН: {s['inn']} | ОГРН: {s.get('ogrn')}")
    print(f"  Выручка ГИР БО ФНС: {s['revenue_rub']:,} руб. ({s.get('revenue_year')} г.)")
    print(f"  Статус: HTTP {s['status_code']} | URL: {s['final_url']} | Задержка: {s['latency_ms']} мс")
    print(f"  Страниц исследовано: {len(s['pages_inspected'])}")
    print(f"  Нарушения 152-ФЗ ({len(s['violations_152fz'])} шт.):")
    for v in s["violations_152fz"][:4]:
        sig = v.get("signature") or f"Форма {v.get('inputs')}"
        print(f"    - [{v.get('type')}] Строка {v.get('line_number')}: {sig} ({v.get('fine_rub')})")
        print(f"      Сниппет: {v.get('snippet')[:140]}")
    if s.get("violations_168fz"):
        print(f"  Нарушения 168-ФЗ (англицизмы): {s.get('violations_168fz')}")
    print(f"  38-ФЗ Режим: {s.get('sheaf_regime')} | Энергия пучка: {s.get('total_sheaf_energy')} | Вердикт: {s.get('conformal_verdict')}")
    if s.get("lead_sheet_path"):
        print(f"  Карточка лида: {s.get('lead_sheet_path')}")

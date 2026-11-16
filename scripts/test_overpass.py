import httpx

query = """[out:json][timeout:10];
(
  node["contact:website"~"\\.ru$"](55.5,37.3,55.9,37.8);
  node["website"~"\\.ru$"](55.5,37.3,55.9,37.8);
);
out tags 50;
"""

mirrors = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]

headers = {
    "User-Agent": "LegalSheafResearchAudit/1.0 (contact: info@legal-audit.ru) Mozilla/5.0",
    "Accept": "application/json",
}

for m in mirrors:
    try:
        print(f"Testing {m}...")
        r = httpx.post(m, data={"data": query}, timeout=8.0, headers=headers)
        print(m, "status:", r.status_code)
        if r.status_code == 200:
            data = r.json()
            elems = data.get("elements", [])
            print("Elements returned:", len(elems))
            for el in elems[:5]:
                print("  Site:", el.get("tags", {}).get("contact:website") or el.get("tags", {}).get("website"))
            break
    except Exception as e:
        print(m, "error:", type(e).__name__, e)

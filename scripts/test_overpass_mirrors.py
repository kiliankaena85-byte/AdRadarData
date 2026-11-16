import httpx

query = """[out:json][timeout:15];
(
  node["contact:website"~"\\.ru$"](55.7,37.5,55.8,37.7);
  node["website"~"\\.ru$"](55.7,37.5,55.8,37.7);
);
out tags 25;
"""

mirrors = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/cgi/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Accept": "application/json",
}

for m in mirrors:
    try:
        print(f"Checking {m}...")
        r = httpx.post(m, data={"data": query}, timeout=8.0, headers=headers, verify=False)
        print(f"  {m} status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            elems = data.get("elements", [])
            print(f"  SUCCESS! elements: {len(elems)}")
            for el in elems[:5]:
                print("   ", el.get("tags", {}).get("contact:website") or el.get("tags", {}).get("website"))
    except Exception as e:
        print(f"  {m} error: {type(e).__name__} {e}")

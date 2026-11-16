import httpx

query = """
[out:json][timeout:30];
(
  node["website"~"\\.ru$"]["office"="company"](55.5,37.3,55.9,37.8);
  way["website"~"\\.ru$"]["office"="company"](55.5,37.3,55.9,37.8);
  node["website"~"\\.ru$"]["man_made"="works"](55.0,37.0,56.5,38.5);
  way["website"~"\\.ru$"]["man_made"="works"](55.0,37.0,56.5,38.5);
  node["contact:website"~"\\.ru$"]["office"="company"](55.5,37.3,55.9,37.8);
);
out tags 50;
"""

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded"
}

endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

for ep in endpoints:
    try:
        r = httpx.post(ep, data={"data": query}, headers=headers, timeout=20.0)
        print(f"Endpoint {ep}: Status {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            els = data.get("elements", [])
            print(f"  Got {len(els)} elements!")
            for el in els[:5]:
                print("   ", el.get("tags", {}).get("name"), "->", el.get("tags", {}).get("website") or el.get("tags", {}).get("contact:website"))
            break
        else:
            print("  Body:", r.text[:200])
    except Exception as e:
        print(f"Endpoint {ep} failed: {e}")

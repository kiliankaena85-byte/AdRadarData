import httpx

urls = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

q = """
[out:json][timeout:15];
(
  node["office"="company"]["website"](55.7,37.5,55.8,37.7);
);
out tags 10;
"""

headers = {
    "User-Agent": "LegalResearchAuditBot/1.0 (https://github.com/adradar; legal-bot@adradar.ru)",
    "Accept": "application/json"
}

for u in urls:
    try:
        r = httpx.post(u, data={"data": q}, headers=headers, timeout=15.0)
        print(f"{u} -> Status {r.status_code}")
        if r.status_code == 200:
            print("Success! Got:", len(r.json().get("elements", [])))
            for el in r.json().get("elements", [])[:3]:
                print("   ", el.get("tags", {}).get("name"), el.get("tags", {}).get("website"))
            break
        else:
            print("Response:", r.text[:200])
    except Exception as e:
        print(f"{u} -> Error: {e}")

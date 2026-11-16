import httpx

query = """[out:json][timeout:15];
node["office"="company"]["website"](55.6,37.4,55.8,37.8);
out tags 10;"""

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "text/plain; charset=utf-8"
}

endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter"
]

for ep in endpoints:
    try:
        r = httpx.post(ep, content=query.encode("utf-8"), headers=headers, timeout=15.0, verify=False)
        print(ep, "-> Status:", r.status_code)
        if r.status_code == 200:
            print("SUCCESS from", ep)
            data = r.json()
            for el in data.get("elements", [])[:5]:
                print(" ", el.get("tags", {}).get("name"), "->", el.get("tags", {}).get("website"))
            break
        else:
            print(" ", r.text[:100])
    except Exception as e:
        print(ep, "-> Error:", e)

import httpx
import urllib.parse

# Москва bbox
query = """[out:json][timeout:20];
(
  node["office"="company"]["website"](55.6,37.4,55.8,37.8);
  node["man_made"="works"]["website"](55.6,37.4,55.8,37.8);
);
out tags 25;"""

# Standard POST with urlencoded data
data = "data=" + urllib.parse.quote(query)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Accept": "*/*"
}

endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

for ep in endpoints:
    try:
        r = httpx.post(ep, content=data.encode("utf-8"), headers=headers, timeout=20.0, verify=False)
        print(ep, "-> Status:", r.status_code)
        if r.status_code == 200:
            res = r.json()
            els = res.get("elements", [])
            print(f"Success! {len(els)} elements found in {ep}")
            for el in els[:5]:
                print(" ", el.get("tags", {}).get("name"), "->", el.get("tags", {}).get("website"))
            break
        else:
            print("  Body:", r.text[:150])
    except Exception as e:
        print(ep, "-> Error:", e)

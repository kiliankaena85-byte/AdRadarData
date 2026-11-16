import urllib.request
import json
import urllib.parse
import time

query = """[out:json][timeout:25];
node["office"="company"]["website"](55.7,37.5,55.8,37.7);
out tags 20;"""

endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

for ep in endpoints:
    print(f"Trying {ep}...")
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        ep,
        data=data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Origin": "https://overpass-turbo.eu",
            "Referer": "https://overpass-turbo.eu/",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("Status:", resp.status)
            res = json.loads(resp.read().decode("utf-8"))
            els = res.get("elements", [])
            print(f"SUCCESS! {len(els)} elements found in {ep}")
            for el in els[:5]:
                print(" ", el.get("tags", {}).get("name"), "->", el.get("tags", {}).get("website"))
            break
    except Exception as e:
        print(f"Failed {ep}: {e}")
        time.sleep(1)

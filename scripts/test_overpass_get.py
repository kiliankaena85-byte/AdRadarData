import httpx
import urllib.parse

query = """[out:json][timeout:40];
(
  nwr["amenity"~"clinic|dentist"]["website"](55.55,37.35,55.92,37.85);
);
out tags 10;"""

for ep in ["https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"]:
    url = ep + "?data=" + urllib.parse.quote(query)
    try:
        print("Testing GET on", ep)
        r = httpx.get(url, timeout=15.0, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        print(f"Status from {ep}: {r.status_code}, len: {len(r.text)}")
    except Exception as e:
        print(f"Error from {ep}: {type(e).__name__} {e}")

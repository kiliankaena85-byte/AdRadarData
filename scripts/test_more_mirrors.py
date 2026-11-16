import httpx

query = """[out:json][timeout:20];
node["amenity"="dentist"](55.7,37.5,55.8,37.7);
out tags 5;"""

mirrors = [
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
    "https://overpass.openstreetmap.ru/cgi/interpreter",
]

for m in mirrors:
    try:
        r = httpx.post(m, data={"data": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0, verify=False)
        print(m, r.status_code, len(r.text))
        if r.status_code == 200:
            print("SUCCESS from", m)
    except Exception as e:
        print(m, type(e).__name__, str(e)[:60])

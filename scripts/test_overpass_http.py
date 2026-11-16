import httpx

query = """[out:json][timeout:15];
node["amenity"="dentist"](55.7,37.5,55.8,37.7);
out tags 5;"""

for proto in ["http", "https"]:
    for host in ["overpass-api.de", "lz4.overpass-api.de", "z.overpass-api.de", "maps.mail.ru/osm/tools/overpass"]:
        url = f"{proto}://{host}/api/interpreter"
        try:
            r = httpx.post(url, data={"data": query}, timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
            print(url, r.status_code, len(r.text))
            if r.status_code == 200:
                print("SUCCESS!", url)
                break
        except Exception as e:
            print(url, type(e).__name__, str(e)[:60])

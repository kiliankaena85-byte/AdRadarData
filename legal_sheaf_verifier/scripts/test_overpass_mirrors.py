import httpx

urls = [
    "http://overpass-api.de/api/interpreter",
    "http://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

q = '[out:json][timeout:10];node["office"="company"]["website"](55.7,37.5,55.8,37.7);out tags 5;'

for u in urls:
    try:
        r = httpx.post(u, data={"data": q}, timeout=10.0, verify=False)
        print(f"{u} -> Status {r.status_code}")
        if r.status_code == 200:
            print("Success! Got:", len(r.json().get("elements", [])))
            break
    except Exception as e:
        print(f"{u} -> Error: {e}")

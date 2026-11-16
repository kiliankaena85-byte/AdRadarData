import httpx

query = """[out:json][timeout:40];
(
  nwr["amenity"~"clinic|dentist"]["website"](55.55,37.35,55.92,37.85);
  nwr["office"="company"]["website"](55.55,37.35,55.92,37.85);
  nwr["man_made"="works"]["website"](55.50,37.20,56.00,38.00);
  nwr["amenity"~"clinic|dentist"]["website"](59.80,30.10,60.10,30.55);
  nwr["office"="company"]["website"](59.80,30.10,60.10,30.55);
  nwr["amenity"~"clinic|dentist"]["website"](56.75,60.45,56.95,60.75);
  nwr["office"="company"]["website"](54.90,82.75,55.12,83.10);
  nwr["office"="company"]["website"](55.70,48.95,55.88,49.25);
);
out tags 600;"""

endpoints = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

for ep in endpoints:
    print(f"Trying {ep} with timeout=45...")
    try:
        r = httpx.post(ep, data={"data": query}, headers=headers, timeout=45.0, verify=False)
        print(f"Status from {ep}: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            elems = data.get("elements", [])
            print(f"Success! Elements count: {len(elems)}")
            break
        else:
            print(f"Response: {r.text[:200]}")
    except Exception as e:
        print(f"Error from {ep}: {type(e).__name__} {e}")

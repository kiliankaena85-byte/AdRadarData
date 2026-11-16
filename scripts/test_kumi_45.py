import httpx
import time

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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

t0 = time.time()
url = "https://overpass.kumi.systems/api/interpreter"
print(f"Connecting to {url} with 45s timeout...")
try:
    r = httpx.post(url, data={"data": query}, headers=headers, timeout=45.0, verify=False)
    print(f"Elapsed: {time.time()-t0:.1f}s | Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        elems = data.get("elements", [])
        print(f"Elements: {len(elems)}")
except Exception as e:
    print(f"Elapsed: {time.time()-t0:.1f}s | Error: {type(e).__name__} {e}")

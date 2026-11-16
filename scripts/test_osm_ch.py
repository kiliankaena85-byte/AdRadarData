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

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)",
    "Accept": "application/json",
}

url = "https://overpass.osm.ch/api/interpreter"
print("Sending query to", url)
r = httpx.post(url, data={"data": query}, headers=headers, timeout=40.0, verify=False)
print("status:", r.status_code)
if r.status_code == 200:
    data = r.json()
    elems = data.get("elements", [])
    print("elements:", len(elems))
    for el in elems[:10]:
        t = el.get("tags", {})
        print(" ", t.get("name"), "->", t.get("website") or t.get("contact:website"))

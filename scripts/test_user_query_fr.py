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

h = {"User-Agent": "LegalSheafAuditor/1.0 (https://adradar.ru; contact@adradar.ru)"}
url = "https://overpass.openstreetmap.fr/api/interpreter"

print("Sending exact user query to", url)
r = httpx.post(url, data={"data": query}, headers=h, timeout=45.0, verify=False)
print("Status:", r.status_code)
if r.status_code == 200:
    data = r.json()
    elems = data.get("elements", [])
    print("TOTAL ELEMENTS RETURNED:", len(elems))
    sites = set()
    for el in elems:
        tags = el.get("tags", {})
        s = tags.get("website") or tags.get("contact:website")
        if s:
            sites.add(s)
    print("TOTAL EXTRACTED WEBSITES:", len(sites))
    print("SAMPLE 10:", list(sites)[:10])

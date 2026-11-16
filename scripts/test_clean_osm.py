import httpx
import urllib.parse

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
r = httpx.post("https://overpass.openstreetmap.fr/api/interpreter", data={"data": query}, headers=h, timeout=45.0, verify=False)
data = r.json()

EXCLUDED = {
    "vk.com", "t.me", "ok.ru", "yandex.ru", "2gis.ru", "prodoctorov.ru", "zoon.ru", "gosuslugi.ru",
    "google.com", "facebook.com", "instagram.com", "youtube.com"
}

clean_domains = []
for el in data.get("elements", []):
    tags = el.get("tags", {})
    raw_url = tags.get("website") or tags.get("contact:website")
    if not raw_url:
        continue
    raw_url = raw_url.strip()
    if not raw_url.startswith("http"):
        raw_url = "http://" + raw_url
    try:
        p = urllib.parse.urlparse(raw_url)
        netloc = p.netloc.lower().split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in EXCLUDED:
            if not any(netloc.endswith("." + ex) for ex in EXCLUDED):
                clean_domains.append(netloc)
    except Exception:
        pass

unique_domains = list(dict.fromkeys(clean_domains))
print(f"Total unique clean RU domains from OSM: {len(unique_domains)}")
print(f"First 25: {unique_domains[:25]}")

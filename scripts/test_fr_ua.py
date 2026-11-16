import httpx

query = """[out:json][timeout:20];
node["amenity"="dentist"](55.7,37.5,55.8,37.7);
out tags 5;"""

headers_variants = [
    {"User-Agent": "LegalSheafAuditor/1.0 (https://adradar.ru; contact@adradar.ru)", "Accept": "application/json"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Accept": "*/*"},
    {"User-Agent": "Qgis/3.28.0", "Accept": "*/*"},
]

url = "https://overpass.openstreetmap.fr/api/interpreter"
for h in headers_variants:
    try:
        r = httpx.post(url, data={"data": query}, headers=h, timeout=10.0, verify=False)
        print("fr status:", r.status_code, "len:", len(r.text))
        if r.status_code == 200:
            print("SUCCESS from osm.fr!")
            break
    except Exception as e:
        print("fr error:", type(e).__name__, e)

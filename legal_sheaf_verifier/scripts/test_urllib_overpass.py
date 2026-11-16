import urllib.request
import json
import urllib.parse

query = """[out:json][timeout:15];
(
  node["office"="company"]["website"](55.6,37.4,55.8,37.8);
);
out tags 10;"""

url = "https://overpass-api.de/api/interpreter"
data = urllib.parse.urlencode({"data": query}).encode("utf-8")

req = urllib.request.Request(
    url,
    data=data,
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://overpass-turbo.eu",
        "Referer": "https://overpass-turbo.eu/",
    }
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("urllib status:", resp.status)
        data = json.loads(resp.read().decode("utf-8"))
        print("Success! Got elements:", len(data.get("elements", [])))
        for el in data.get("elements", [])[:5]:
            print("  ", el.get("tags", {}).get("name"), "->", el.get("tags", {}).get("website"))
except Exception as e:
    print("urllib error:", e)

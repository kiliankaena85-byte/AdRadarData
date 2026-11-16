import httpx
import urllib.parse

# Test 1: Overpass via GET
q = '[out:json][timeout:15];node["office"="company"]["website"](55.7,37.5,55.8,37.7);out tags 25;'
url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(q)

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

try:
    r = httpx.get(url, headers=headers, timeout=15.0)
    print("Overpass GET status:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print("Overpass GET elements:", len(data.get("elements", [])))
        for el in data.get("elements", [])[:5]:
            print("  ", el.get("tags", {}).get("name"), el.get("tags", {}).get("website"))
    else:
        print("Overpass GET response:", r.text[:200])
except Exception as e:
    print("Overpass GET error:", e)

# Test 2: VC.ru timeline pagination
try:
    r = httpx.get("https://api.vc.ru/v2.8/timeline?limit=20", timeout=10.0)
    print("VC.ru status:", r.status_code)
    items = r.json().get("result", {}).get("items", [])
    print("VC.ru items count:", len(items))
    links = []
    for it in items:
        blocks = it.get("blocks", [])
        for b in blocks:
            data = b.get("data", {}) or {}
            # link
            l = data.get("link", {}).get("url") or data.get("url")
            if l: links.append(l)
    print("Sample links from 1 page of VC:", links[:5])
except Exception as e:
    print("VC.ru error:", e)

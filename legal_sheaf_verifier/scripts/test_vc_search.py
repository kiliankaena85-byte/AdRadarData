import httpx
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

queries = ["завод", "производство", "опт", "поставки", "клиника", "оборудование"]
for q in queries:
    url = f"https://api.vc.ru/v2.1/search?query={q}&limit=20"
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        print(f"Query '{q}': status {r.status_code}")
        if r.status_code == 200:
            res = r.json().get("result", {})
            items = res.get("items", [])
            print(f"  Found {len(items)} items for '{q}'")
    except Exception as e:
        print(f"Query '{q}' error: {e}")

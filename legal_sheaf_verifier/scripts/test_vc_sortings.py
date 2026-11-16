import httpx

sortings = ["date", "popular", "week", "month", "year"]
headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

for s in sortings:
    url = f"https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting={s}&limit=20"
    try:
        r = httpx.get(url, headers=headers, timeout=10.0)
        print(f"Sorting '{s}': status {r.status_code}, items: {len(r.json().get('result', {}).get('items', []))}")
    except Exception as e:
        print(f"Sorting '{s}' error: {e}")

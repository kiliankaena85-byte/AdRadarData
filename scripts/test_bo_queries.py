import httpx

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://bo.nalog.gov.ru/",
    "Accept": "application/json, text/plain, */*",
}

for q in ["Газпром", "Лукойл", "7702038150", "7708004767", "7707083893"]:
    try:
        r = httpx.get(f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={q}", headers=headers, verify=False, timeout=8)
        data = r.json()
        print(f"{q} -> status: {r.status_code}, total: {data.get('totalElements')}, content len: {len(data.get('content', []))}")
        if data.get("content"):
            c0 = data["content"][0]
            print(f"  found: {c0.get('shortName')} / INN: {c0.get('inn')} / bfo: {c0.get('bfo')}")
    except Exception as e:
        print(f"{q} -> error: {e}")

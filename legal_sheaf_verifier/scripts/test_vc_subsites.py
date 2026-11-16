import httpx

headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
try:
    r = httpx.get("https://api.vc.ru/v2.1/subsites_list", headers=headers, timeout=10.0)
    print("subsites_list status:", r.status_code)
    if r.status_code == 200:
        data = r.json().get("result", [])
        print("Subsites count:", len(data))
        for s in data[:10]:
            print(" ", s.get("id"), s.get("name"), s.get("url"))
except Exception as e:
    print("Error:", e)

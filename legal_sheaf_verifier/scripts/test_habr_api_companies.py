import httpx

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

try:
    r = httpx.get("https://habr.com/kek/v2/companies?fl=ru&hl=ru&page=1", headers=headers, timeout=10.0)
    print("Status:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print("Data keys:", data.keys())
        comp_ids = data.get("companyIds", [])
        print("Companies count:", len(comp_ids))
        companies = data.get("companyRefs", {})
        for cid in comp_ids[:5]:
            c = companies.get(cid, {})
            print("Company:", c.get("titleHtml"), "Site:", c.get("siteUrl"))
except Exception as e:
    print("Error:", e)

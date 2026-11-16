import httpx

test_inns = ["7702070139", "7801262791", "7724229333", "6671234567"]
endpoints = [
    "https://bo.nalog.gov.ru/nws/search?query={inn}",
    "https://bo.nalog.gov.ru/advanced-search/organizations/search?query={inn}",
    "https://bo.nalog.gov.ru/api/search?query={inn}",
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://bo.nalog.gov.ru/",
}

for inn in test_inns:
    for ep_template in endpoints:
        ep = ep_template.format(inn=inn)
        try:
            r = httpx.get(ep, headers=headers, timeout=6.0, verify=False)
            if r.status_code == 200:
                data = r.json()
                content = data.get("content", [])
                if content:
                    print(f"FOUND for INN {inn} via {ep} -> {len(content)} orgs")
                    org = content[0]
                    print("  Name:", org.get("shortName"))
                    print("  BFO:", org.get("bfo"))
                    break
        except Exception as e:
            pass

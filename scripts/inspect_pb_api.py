import httpx

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://bo.nalog.gov.ru/",
}
r = httpx.get("https://bo.nalog.gov.ru/static/js/main.c0e3a91a.js", headers=headers, verify=False)
text = r.text

for target in ["pb-api", "organizations-card", "n-api"]:
    pos = 0
    while True:
        pos = text.find(target, pos)
        if pos == -1:
            break
        print(f"=== {target} ===")
        print(text[max(0, pos-100):min(len(text), pos+200)])
        pos += len(target)

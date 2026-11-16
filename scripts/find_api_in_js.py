import httpx
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://bo.nalog.gov.ru/",
}
r = httpx.get("https://bo.nalog.gov.ru/static/js/main.c0e3a91a.js", headers=headers, verify=False)
text = r.text

print("Searching for URLs in JS...")
urls = set(re.findall(r'["\'](/[a-zA-Z0-9_\-/]+)["\']', text))
for u in sorted(urls):
    if any(k in u for k in ["search", "org", "bfo", "api"]):
        print("  ", u)

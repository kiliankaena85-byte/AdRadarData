import httpx
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

r = httpx.get("https://bo.nalog.gov.ru/", headers=headers, verify=False)
print("status:", r.status_code)
scripts = re.findall(r'src=["\']([^"\']+\.js)["\']', r.text)
print("scripts:", scripts)

for s in scripts:
    js_url = "https://bo.nalog.gov.ru" + s if s.startswith("/") else f"https://bo.nalog.gov.ru/{s}"
    try:
        rj = httpx.get(js_url, headers=headers, verify=False, timeout=5)
        # Search for api paths
        apis = set(re.findall(r'/(?:api|n-api|search|advanced-search|organizations|bfo)[a-zA-Z0-9_\-/]*', rj.text))
        if apis:
            print(f"APIs in {s}:")
            for a in sorted(apis):
                if any(k in a for k in ["org", "search", "bfo", "financial"]):
                    print("  ", a)
    except Exception as e:
        print("error reading", s, e)

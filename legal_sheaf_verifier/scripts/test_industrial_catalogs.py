import httpx
from bs4 import BeautifulSoup
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

urls = [
    "https://fabricators.ru/zavody",
    "https://manufacturers.ru/kompanii",
    "https://productcenter.ru/proizvoditeli",
]

for u in urls:
    try:
        r = httpx.get(u, headers=headers, timeout=8.0, follow_redirects=True)
        print(u, "-> Status:", r.status_code)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            # Find links
            doms = set()
            for a in soup.find_all("a", href=True):
                h = a["href"]
                m = re.search(r"https?://([a-zA-Z0-9_\-\.]+)", h)
                if m:
                    d = m.group(1).lower().split(":")[0]
                    if d.startswith("www."): d = d[4:]
                    if (d.endswith(".ru") or d.endswith(".рф")) and "fabricat" not in d and "manufactur" not in d and "productcenter" not in d:
                        doms.add(d)
            print("  Found RU domains:", len(doms), list(doms)[:5])
    except Exception as e:
        print(u, "-> Error:", e)

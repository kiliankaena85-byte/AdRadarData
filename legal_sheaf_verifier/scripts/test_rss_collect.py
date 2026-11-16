import httpx
from bs4 import BeautifulSoup
import urllib.parse
import re

feeds = [
    "https://rb.ru/feeds/all/",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://habr.com/ru/rss/all/all/?fl=ru",
    "https://habr.com/ru/rss/best/daily/?fl=ru",
    "https://habr.com/ru/rss/best/weekly/?fl=ru",
    "https://habr.com/ru/rss/best/monthly/?fl=ru",
]

domains = set()
headers = {"User-Agent": "Mozilla/5.0"}

for f in feeds:
    try:
        r = httpx.get(f, headers=headers, timeout=8.0, follow_redirects=True)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "xml")
            items = soup.find_all("item")
            print(f"{f}: found {len(items)} items")
            for it in items:
                desc = it.find("description")
                if desc and desc.text:
                    for a in BeautifulSoup(desc.text, "html.parser").find_all("a", href=True):
                        h = a["href"]
                        m = re.search(r"https?://([a-zA-Z0-9_\-\.]+)", h)
                        if m:
                            d = m.group(1).lower().split(":")[0]
                            if d.startswith("www."): d = d[4:]
                            if d.endswith(".ru") or d.endswith(".рф"):
                                domains.add(d)
    except Exception as e:
        print(f"{f}: error {e}")

print("Total new domains from RSS:", len(domains))
print("Sample:", list(domains)[:10])

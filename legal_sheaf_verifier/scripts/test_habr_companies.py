import httpx
from bs4 import BeautifulSoup
import urllib.parse
import re

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

try:
    r = httpx.get("https://habr.com/ru/companies/page1/", headers=headers, timeout=10.0)
    print("Habr companies status:", r.status_code)
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")
        # Find company profile links or website links
        links = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if "/company/" in h:
                links.append(h)
        print("Found company links:", len(set(links)), list(set(links))[:5])
except Exception as e:
    print("Error:", e)

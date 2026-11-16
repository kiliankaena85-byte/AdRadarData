import httpx
import re
from bs4 import BeautifulSoup

# 1. Test VC.ru articles and extract external links
try:
    r = httpx.get("https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date", timeout=10.0)
    print("VC.ru v2.1 status:", r.status_code)
    if r.status_code == 200:
        data = r.json().get("result", {})
        items = data.get("items", [])
        print("VC.ru items count:", len(items))
        domains = set()
        for it in items:
            blocks = it.get("data", {}).get("blocks", [])
            for b in blocks:
                # search text for href
                txt = b.get("data", {}).get("text", "")
                if "<a " in txt:
                    soup = BeautifulSoup(txt, "html.parser")
                    for a in soup.find_all("a", href=True):
                        h = a["href"]
                        m = re.search(r"https?://([a-zA-Z0-9_\-\.]+)", h)
                        if m:
                            dom = m.group(1).lower()
                            if dom.endswith(".ru") or dom.endswith(".рф"):
                                domains.add(dom)
        print("Real RU domains from VC:", len(domains), list(domains)[:10])
except Exception as e:
    print("VC error:", e)

# 2. Test Habr API
try:
    r = httpx.get("https://habr.com/kek/v2/articles/?fl=ru&hl=ru&page=1", timeout=10.0)
    print("Habr API status:", r.status_code)
    if r.status_code == 200:
        habr_data = r.json()
        article_refs = habr_data.get("articleRefs", {})
        print("Habr articles:", len(article_refs))
        habr_domains = set()
        for aid, art in article_refs.items():
            author = art.get("author", {})
            h_txt = art.get("textHtml", "")
            for m in re.finditer(r"https?://([a-zA-Z0-9_\-\.]+)", h_txt):
                dom = m.group(1).lower()
                if dom.endswith(".ru") or dom.endswith(".рф"):
                    habr_domains.add(dom)
        print("Real RU domains from Habr:", len(habr_domains), list(habr_domains)[:10])
except Exception as e:
    print("Habr error:", e)

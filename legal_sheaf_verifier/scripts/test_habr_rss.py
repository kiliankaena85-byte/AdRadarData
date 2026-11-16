import httpx
import re
import urllib.parse
from bs4 import BeautifulSoup

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru",
    "github.com", "wikipedia.org", "google.com", "yandex.ru", "ya.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "medium.com", "tbank.ru", "sberbank.ru"
}

def resolve_vc_redirect(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        if "vc.ru" in parsed.netloc and "redirect" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("to", [None])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return url

# Test Habr RSS
try:
    r = httpx.get("https://habr.com/ru/rss/all/all/?fl=ru", timeout=10.0, follow_redirects=True)
    print("Habr RSS Status:", r.status_code)
    if r.status_code == 200:
        soup = BeautifulSoup(r.text, "xml")
        items = soup.find_all("item")
        print("Habr RSS items:", len(items))
        habr_links = set()
        for it in items:
            desc = it.find("description")
            if desc:
                txt = desc.text
                for a in BeautifulSoup(txt, "html.parser").find_all("a", href=True):
                    h = a["href"]
                    p = urllib.parse.urlparse(h)
                    dom = p.netloc.lower().split(":")[0]
                    if dom.startswith("www."): dom = dom[4:]
                    if (dom.endswith(".ru") or dom.endswith(".рф")) and dom not in COMMON_PLATFORMS:
                        habr_links.add(dom)
        print("Habr unique RU domains:", len(habr_links), list(habr_links)[:5])
except Exception as e:
    print("Habr RSS error:", e)

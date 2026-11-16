import asyncio
import httpx
import urllib.parse
from bs4 import BeautifulSoup
import re

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "vk.com", "vk.ru", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru",
    "github.com", "wikipedia.org", "google.com", "google.ru", "yandex.ru", "ya.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "medium.com", "tbank.ru", "sberbank.ru"
}

def resolve_vc_redirect(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        if ("vc.ru" in parsed.netloc or "dtf.ru" in parsed.netloc) and "redirect" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("to", [None])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return url

def clean_domain(raw_url_or_domain: str) -> str | None:
    if not raw_url_or_domain: return None
    url = raw_url_or_domain.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    try:
        p = urllib.parse.urlparse(url)
        netloc = p.netloc.lower().split(":")[0]
        if netloc.startswith("www."): netloc = netloc[4:]
        if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
            if "." in netloc and len(netloc) >= 4 and not netloc.endswith(".yandex.ru") and not netloc.endswith(".vk.ru") and not netloc.endswith(".rospotrebnadzor.ru"):
                return netloc
    except Exception:
        pass
    return None

async def test_subsites():
    headers = {"User-Agent": "Mozilla/5.0"}
    domains = set()
    async with httpx.AsyncClient(headers=headers) as client:
        for sid in [238, 140, 150, 100, 300]:
            url = f"https://api.vc.ru/v2.1/timeline?subsiteIds={sid}&sorting=date&limit=40"
            try:
                r = await client.get(url, timeout=6.0)
                if r.status_code == 200:
                    items = r.json().get("result", {}).get("items", [])
                    print(f"Subsite {sid}: items {len(items)}")
                    for it in items:
                        blocks = it.get("data", {}).get("blocks", [])
                        for b in blocks:
                            b_data = b.get("data", {}) or {}
                            txt = b_data.get("text", "")
                            if "<a " in txt:
                                soup = BeautifulSoup(txt, "html.parser")
                                for a in soup.find_all("a", href=True):
                                    dom = clean_domain(resolve_vc_redirect(a["href"]))
                                    if dom: domains.add(dom)
            except Exception as e:
                print(sid, "err", e)
    print("Additional unique domains from subsites:", len(domains))

asyncio.run(test_subsites())

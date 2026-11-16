import asyncio
import httpx
import re
import urllib.parse
from bs4 import BeautifulSoup
from typing import Set, List

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "vk.com", "vk.ru", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru",
    "github.com", "wikipedia.org", "google.com", "google.ru", "yandex.ru", "ya.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "medium.com", "tbank.ru", "sberbank.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com", "dtf.ru",
    "cbr.ru", "nalog.gov.ru", "gosuslugi.ru", "mos.ru", "consultant.ru", "garant.ru",
    "2gis.ru", "2gis.com", "ok.ru", "mail.ru", "avito.ru", "hh.ru", "superjob.ru",
    "wildberries.ru", "ozon.ru", "aliexpress.ru", "market.yandex.ru", "sber.ru",
    "t.ru", "dzeninfra.ru", "kinopoisk.ru", "music.yandex.ru", "sentry.io",
    "lenta.ru", "ria.ru", "tass.ru", "fontanka.ru", "gazeta.ru"
}

def resolve_redirect(url: str) -> str:
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

def clean_domain(raw_url: str) -> str | None:
    if not raw_url:
        return None
    url = raw_url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    try:
        p = urllib.parse.urlparse(url)
        netloc = p.netloc.lower().split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
            if "." in netloc and len(netloc) >= 4 and not netloc.endswith(".yandex.ru") and not netloc.endswith(".vk.ru") and not netloc.endswith(".mail.ru"):
                return netloc
    except Exception:
        pass
    return None

async def harvest_osnova_stream(client: httpx.AsyncClient, base_url: str, subsite_id: int, sorting: str, max_pages: int = 25) -> Set[str]:
    found = set()
    last_id = None
    last_val = None
    for p in range(max_pages):
        url = f"{base_url}?subsiteIds={subsite_id}&sorting={sorting}"
        if last_id and last_val:
            url += f"&lastId={last_id}&lastSortingValue={last_val}"
        try:
            r = await client.get(url, timeout=7.0)
            if r.status_code != 200:
                break
            res = r.json().get("result", {})
            items = res.get("items", [])
            if not items:
                break
            last_id = res.get("lastId")
            last_val = res.get("lastSortingValue")

            for it in items:
                d = it.get("data", {})
                for b in d.get("blocks", []):
                    b_data = b.get("data", {}) or {}
                    txt = b_data.get("text", "")
                    if "<a " in txt:
                        soup = BeautifulSoup(txt, "html.parser")
                        for a in soup.find_all("a", href=True):
                            dom = clean_domain(resolve_redirect(a["href"]))
                            if dom: found.add(dom)
                    l = b_data.get("link", {}).get("url") or b_data.get("url")
                    if l and isinstance(l, str):
                        dom = clean_domain(resolve_redirect(l))
                        if dom: found.add(dom)
        except Exception:
            break
    return found

async def harvest_habr_companies(client: httpx.AsyncClient, max_pages: int = 15) -> Set[str]:
    found = set()
    for page in range(1, max_pages + 1):
        try:
            r = await client.get(f"https://habr.com/ru/companies/page{page}/", timeout=6.0)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # In Habr company cards, some link directly to company site or profile
                dom = clean_domain(href)
                if dom: found.add(dom)
        except Exception:
            break
    return found

async def main():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json, text/html"}
    all_domains = set()

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        streams = []
        # VC main sortings
        for sort in ["date", "popular", "month", "year"]:
            streams.append(harvest_osnova_stream(client, "https://api.vc.ru/v2.1/timeline", 0, sort, max_pages=30))
        # VC subsites
        for sub_id in [68172, 68173, 68180, 68174, 68178, 68183, 68179, 68181]:
            streams.append(harvest_osnova_stream(client, "https://api.vc.ru/v2.1/timeline", sub_id, "date", max_pages=20))
            streams.append(harvest_osnova_stream(client, "https://api.vc.ru/v2.1/timeline", sub_id, "popular", max_pages=20))
        # DTF sortings
        for sort in ["date", "popular", "month"]:
            streams.append(harvest_osnova_stream(client, "https://api.dtf.ru/v2.1/timeline", 0, sort, max_pages=20))
        # Habr
        streams.append(harvest_habr_companies(client, max_pages=15))

        print(f"[*] Running {len(streams)} parallel harvester streams...")
        results = await asyncio.gather(*streams)
        for r in results:
            all_domains.update(r)

    print(f"[+] Total harvested domains: {len(all_domains)}")
    print(f"Sample 30: {sorted(list(all_domains))[:30]}")

if __name__ == "__main__":
    asyncio.run(main())

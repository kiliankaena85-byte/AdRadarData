import asyncio
import httpx
import urllib.parse
from bs4 import BeautifulSoup
import re
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
    "t.ru", "ya.ru", "dzeninfra.ru", "kinopoisk.ru", "music.yandex.ru"
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
    if not raw_url_or_domain:
        return None
    url = raw_url_or_domain.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    try:
        p = urllib.parse.urlparse(url)
        netloc = p.netloc.lower().split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
            if "." in netloc and len(netloc) >= 4 and not netloc.endswith(".yandex.ru") and not netloc.endswith(".vk.ru") and not netloc.endswith(".rospotrebnadzor.ru"):
                return netloc
    except Exception:
        pass
    return None

async def harvest_timeline(client: httpx.AsyncClient, base_api: str, sortings: list, max_pages: int = 40) -> Set[str]:
    found = set()
    for sorting in sortings:
        last_id = None
        last_sorting_value = None
        for page in range(max_pages):
            url = f"{base_api}?subsiteIds=0&sorting={sorting}"
            if last_id and last_sorting_value:
                url += f"&lastId={last_id}&lastSortingValue={last_sorting_value}"
            try:
                r = await client.get(url, timeout=7.0)
                if r.status_code != 200:
                    break
                res = r.json().get("result", {})
                items = res.get("items", [])
                if not items:
                    break
                last_id = res.get("lastId")
                last_sorting_value = res.get("lastSortingValue")

                for it in items:
                    d = it.get("data", {})
                    blocks = d.get("blocks", [])
                    for b in blocks:
                        b_data = b.get("data", {}) or {}
                        txt = b_data.get("text", "")
                        if "<a " in txt:
                            soup = BeautifulSoup(txt, "html.parser")
                            for a in soup.find_all("a", href=True):
                                h = resolve_vc_redirect(a["href"])
                                dom = clean_domain(h)
                                if dom: found.add(dom)
                        l = b_data.get("link", {}).get("url") or b_data.get("url")
                        if l and isinstance(l, str):
                            h = resolve_vc_redirect(l)
                            dom = clean_domain(h)
                            if dom: found.add(dom)
            except Exception:
                break
    return found

async def main():
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    all_domains = set()
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        print("[*] Harvesting VC.ru across 4 sortings (date, popular, month, year)...")
        vc_doms = await harvest_timeline(client, "https://api.vc.ru/v2.1/timeline", ["date", "popular", "month", "year"], max_pages=60)
        all_domains.update(vc_doms)
        print(f"VC.ru yielded: {len(vc_doms)} domains. Total: {len(all_domains)}")

        print("[*] Harvesting DTF.ru across 3 sortings...")
        dtf_doms = await harvest_timeline(client, "https://api.dtf.ru/v2.1/timeline", ["date", "popular", "month"], max_pages=50)
        all_domains.update(dtf_doms)
        print(f"DTF.ru yielded: {len(dtf_doms)} domains. Total: {len(all_domains)}")

    print(f"GRAND TOTAL REAL RU DOMAINS: {len(all_domains)}")

asyncio.run(main())

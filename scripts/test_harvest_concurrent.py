import asyncio
import httpx
import re
import urllib.parse
from bs4 import BeautifulSoup

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
    "t.ru", "dzeninfra.ru", "kinopoisk.ru", "music.yandex.ru", "sentry.io"
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

async def fetch_timeline_page(client: httpx.AsyncClient, base_api: str, subsite_id: int, sorting: str, last_id=None, last_val=None):
    url = f"{base_api}?subsiteIds={subsite_id}&sorting={sorting}"
    if last_id and last_val:
        url += f"&lastId={last_id}&lastSortingValue={last_val}"
    try:
        r = await client.get(url, timeout=6.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def extract_domains_from_json(data: dict) -> tuple[set, str, str]:
    doms = set()
    res = data.get("result", {})
    items = res.get("items", [])
    last_id = res.get("lastId")
    last_val = res.get("lastSortingValue")
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
                    if dom: doms.add(dom)
            l = b_data.get("link", {}).get("url") or b_data.get("url")
            if l and isinstance(l, str):
                h = resolve_vc_redirect(l)
                dom = clean_domain(h)
                if dom: doms.add(dom)
    return doms, last_id, last_val

async def main():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
    all_domains = set()
    
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # Collect across multiple subsites and sortings
        # VC.ru subsites: 0 (all), 68172 (Marketing), 68173 (Business), 68180 (Services), 68174 (Finance), 68178 (Law), 68183 (SEO), 68179 (Tech), 68181 (Education)
        subsites = [0, 68172, 68173, 68180, 68174, 68178, 68183, 68179, 68181]
        
        async def crawl_subsite_stream(base_api: str, sub_id: int, sorting: str, depth=15):
            local_doms = set()
            cur_id = None
            cur_val = None
            for p in range(depth):
                data = await fetch_timeline_page(client, base_api, sub_id, sorting, cur_id, cur_val)
                if not data:
                    break
                doms, next_id, next_val = extract_domains_from_json(data)
                local_doms.update(doms)
                if not next_id or next_id == cur_id:
                    break
                cur_id = next_id
                cur_val = next_val
            return local_doms

        tasks = []
        for s in subsites:
            tasks.append(crawl_subsite_stream("https://api.vc.ru/v2.1/timeline", s, "date", depth=10))
            tasks.append(crawl_subsite_stream("https://api.vc.ru/v2.1/timeline", s, "popular", depth=10))
        # DTF subsites
        tasks.append(crawl_subsite_stream("https://api.dtf.ru/v2.1/timeline", 0, "date", depth=10))
        tasks.append(crawl_subsite_stream("https://api.dtf.ru/v2.1/timeline", 0, "popular", depth=10))

        print(f"[*] Running {len(tasks)} concurrent timeline crawls...")
        results = await asyncio.gather(*tasks)
        for r in results:
            all_domains.update(r)

    print(f"[+] Total unique Russian corporate domains harvested: {len(all_domains)}")
    sample = sorted(list(all_domains))[:20]
    print(f"Sample 20 domains: {sample}")

if __name__ == "__main__":
    asyncio.run(main())

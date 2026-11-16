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
    "wildberries.ru", "ozon.ru", "aliexpress.ru", "market.yandex.ru"
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
            # Check domain has valid structure
            if "." in netloc and len(netloc) >= 4 and not netloc.endswith(".yandex.ru") and not netloc.endswith(".vk.ru"):
                return netloc
    except Exception:
        pass
    return None

async def query_overpass_regions(client: httpx.AsyncClient) -> Set[str]:
    """Queries Overpass API for companies/factories/clinics across 7 major Russian regions."""
    regions_bbox = [
        # (min_lat, min_lon, max_lat, max_lon)
        ("Москва", 55.55, 37.35, 55.90, 37.85),
        ("Санкт-Петербург", 59.80, 30.15, 60.05, 30.50),
        ("Новосибирск", 54.90, 82.80, 55.15, 83.10),
        ("Екатеринбург", 56.75, 60.45, 56.95, 60.75),
        ("Нижний Новгород", 56.20, 43.80, 56.40, 44.10),
        ("Казань", 55.70, 49.00, 55.90, 49.25),
        ("Краснодар", 45.00, 38.90, 45.10, 39.10),
    ]
    found = set()
    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    for name, s, w, n, e in regions_bbox:
        q = f"""[out:json][timeout:12];
(
  node["office"="company"]["website"]({s},{w},{n},{e});
  node["man_made"="works"]["website"]({s},{w},{n},{e});
  node["amenity"="clinic"]["website"]({s},{w},{n},{e});
  node["amenity"="dentist"]["website"]({s},{w},{n},{e});
  node["craft"]["website"]({s},{w},{n},{e});
);
out tags 30;"""
        for ep in endpoints:
            try:
                r = await client.post(ep, data={"data": q}, timeout=12.0)
                if r.status_code == 200:
                    for el in r.json().get("elements", []):
                        tags = el.get("tags", {})
                        site = tags.get("website") or tags.get("contact:website")
                        dom = clean_domain(site)
                        if dom:
                            found.add(dom)
                    print(f"[Overpass] {name} ({ep}): got {len(found)} domains so far")
                    break
            except Exception:
                continue

    return found

async def harvest_timeline(client: httpx.AsyncClient, base_api: str, max_pages: int = 50) -> Set[str]:
    """Paginates Osnova timelines (vc.ru, dtf.ru) extracting external commercial domains."""
    found = set()
    for sorting in ["date", "popular", "month"]:
        last_id = None
        last_sorting_value = None
        for page in range(max_pages):
            url = f"{base_api}?subsiteIds=0&sorting={sorting}"
            if last_id and last_sorting_value:
                url += f"&lastId={last_id}&lastSortingValue={last_sorting_value}"
            try:
                r = await client.get(url, timeout=8.0)
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

async def collect_real_ru_domains(target_count: int = 350) -> List[str]:
    all_domains = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/html, */*",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # Source 1: Overpass API
        print("[*] Sourcing from OpenStreetMap Overpass API...")
        try:
            overpass_doms = await query_overpass_regions(client)
            all_domains.update(overpass_doms)
            print(f"[+] Overpass yielded: {len(overpass_doms)} unique domains")
        except Exception as e:
            print(f"[!] Overpass failed: {e}")

        # Source 2: VC.ru Timeline Pagination
        print(f"[*] Sourcing from api.vc.ru timeline pagination...")
        vc_doms = await harvest_timeline(client, "https://api.vc.ru/v2.1/timeline", max_pages=60)
        all_domains.update(vc_doms)
        print(f"[+] VC.ru yielded: {len(vc_doms)} unique domains. Total now: {len(all_domains)}")

        # Source 3: DTF.ru Timeline Pagination (if needed to reach target)
        if len(all_domains) < target_count:
            print(f"[*] Sourcing from api.dtf.ru timeline pagination...")
            dtf_doms = await harvest_timeline(client, "https://api.dtf.ru/v2.1/timeline", max_pages=40)
            all_domains.update(dtf_doms)
            print(f"[+] DTF.ru yielded: {len(dtf_doms)} unique domains. Total now: {len(all_domains)}")

    return sorted(list(all_domains))

if __name__ == "__main__":
    doms = asyncio.run(collect_real_ru_domains(300))
    print(f"Collected Total: {len(doms)} real RU domains")
    print("First 25:", doms[:25])

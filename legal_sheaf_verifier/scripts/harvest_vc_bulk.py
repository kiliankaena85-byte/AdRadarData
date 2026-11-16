import asyncio
import httpx
import urllib.parse
from bs4 import BeautifulSoup
import re

COMMON_PLATFORMS = {
    "vc.ru", "t.me", "telegram.org", "vk.com", "youtube.com", "youtu.be",
    "dzen.ru", "habr.com", "rbc.ru", "kommersant.ru", "vedomosti.ru",
    "github.com", "wikipedia.org", "google.com", "yandex.ru", "ya.ru",
    "instagram.com", "facebook.com", "twitter.com", "x.com", "linkedin.com",
    "rutube.ru", "tenchat.ru", "pikabu.ru", "medium.com", "tbank.ru", "sberbank.ru",
    "apple.com", "android.com", "play.google.com", "apps.apple.com"
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

async def harvest_vc_domains(max_pages_per_sort: int = 40):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    domains = set()

    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        for sorting in ["date", "popular", "month"]:
            print(f"[*] Harvesting VC.ru with sorting='{sorting}'...")
            last_id = None
            last_sorting_value = None

            for page in range(max_pages_per_sort):
                url = f"https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting={sorting}"
                if last_id and last_sorting_value:
                    url += f"&lastId={last_id}&lastSortingValue={last_sorting_value}"
                
                try:
                    r = await client.get(url)
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
                            # text links
                            txt = b_data.get("text", "")
                            if "<a " in txt:
                                soup = BeautifulSoup(txt, "html.parser")
                                for a in soup.find_all("a", href=True):
                                    href = resolve_vc_redirect(a["href"])
                                    p = urllib.parse.urlparse(href)
                                    netloc = p.netloc.lower().split(":")[0]
                                    if netloc.startswith("www."): netloc = netloc[4:]
                                    if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
                                        domains.add(netloc)
                            # link block
                            l = b_data.get("link", {}).get("url") or b_data.get("url")
                            if l and isinstance(l, str):
                                href = resolve_vc_redirect(l)
                                p = urllib.parse.urlparse(href)
                                netloc = p.netloc.lower().split(":")[0]
                                if netloc.startswith("www."): netloc = netloc[4:]
                                if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
                                    domains.add(netloc)
                except Exception as e:
                    break

            print(f"  -> Total unique domains after {sorting}: {len(domains)}")

    return sorted(list(domains))

if __name__ == "__main__":
    doms = asyncio.run(harvest_vc_domains(30))
    print(f"Final Count of Real RU Domains: {len(doms)}")
    print("Sample 20:", doms[:20])

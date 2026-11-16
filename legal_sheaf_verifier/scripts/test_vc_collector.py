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

async def main():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    domains = set()
    last_id = None
    last_sorting_value = None

    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        for page in range(15):
            url = "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date"
            if last_id and last_sorting_value:
                url += f"&lastId={last_id}&lastSortingValue={last_sorting_value}"
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    print("Status:", r.status_code)
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
                        # Check text html
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
                        # Check link/embed url
                        link = b_data.get("link", {}).get("url") or b_data.get("url")
                        if link and isinstance(link, str):
                            href = resolve_vc_redirect(link)
                            p = urllib.parse.urlparse(href)
                            netloc = p.netloc.lower().split(":")[0]
                            if netloc.startswith("www."): netloc = netloc[4:]
                            if (netloc.endswith(".ru") or netloc.endswith(".рф")) and netloc not in COMMON_PLATFORMS:
                                domains.add(netloc)

                print(f"Page {page+1}: collected {len(domains)} unique RU domains so far...")
            except Exception as e:
                print(f"Page {page} error: {e}")
                break

    print(f"Total Unique RU Domains from VC: {len(domains)}")
    print("Sample:", list(domains)[:15])

import asyncio
asyncio.run(main())

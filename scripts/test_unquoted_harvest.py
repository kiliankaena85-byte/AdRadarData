import asyncio
import httpx
import re
import urllib.parse
from typing import Set

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

def clean_domain(dom: str) -> str | None:
    d = dom.lower().lstrip("www.").strip()
    if (d.endswith(".ru") or d.endswith(".рф")) and d not in COMMON_PLATFORMS:
        if "." in d and len(d) >= 4 and not d.endswith(".yandex.ru") and not d.endswith(".vk.ru") and not d.endswith(".mail.ru"):
            return d
    return None

async def harvest_stream(client: httpx.AsyncClient, base_url: str, subsite_id: int, sorting: str, depth=20) -> Set[str]:
    found = set()
    last_id = None
    last_val = None
    for _ in range(depth):
        url = f"{base_url}?subsiteIds={subsite_id}&sorting={sorting}"
        if last_id and last_val:
            url += f"&lastId={last_id}&lastSortingValue={last_val}"
        try:
            r = await client.get(url, timeout=6.0)
            if r.status_code != 200:
                break
            raw = urllib.parse.unquote(r.text.replace("\\/", "/"))
            matches = re.findall(r"https?://([a-zA-Z0-9.-]+\.(?:ru|рф))", raw)
            for m in matches:
                cd = clean_domain(m)
                if cd:
                    found.add(cd)

            res = r.json().get("result", {})
            last_id = res.get("lastId")
            last_val = res.get("lastSortingValue")
            if not last_id:
                break
        except Exception:
            break
    return found

async def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    domains = set()
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        streams = []
        for s in ["date", "popular", "month", "year"]:
            streams.append(harvest_stream(client, "https://api.vc.ru/v2.1/timeline", 0, s, depth=25))
        for sub in [68172, 68173, 68180, 68174, 68178, 68183, 68179, 68181]:
            streams.append(harvest_stream(client, "https://api.vc.ru/v2.1/timeline", sub, "date", depth=15))
            streams.append(harvest_stream(client, "https://api.vc.ru/v2.1/timeline", sub, "popular", depth=15))
        for s in ["date", "popular"]:
            streams.append(harvest_stream(client, "https://api.dtf.ru/v2.1/timeline", 0, s, depth=20))

        results = await asyncio.gather(*streams)
        for r in results:
            domains.update(r)

    print(f"Total unquoted harvest count: {len(domains)}")
    print("Sample 20:", sorted(list(domains))[:20])

if __name__ == "__main__":
    asyncio.run(main())

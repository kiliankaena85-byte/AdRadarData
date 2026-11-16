import httpx
import re

url = "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date"
r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"})
text = r.text
found = re.findall(r'https?://([a-zA-Z0-9.-]+\.(?:ru|рф))', text)
print("Bare regex on 1 timeline page found:", len(found), set(found))

"""
VLM Context Engine: Safe Playwright Trigger Zone Cropper and Canvas Collage Assembler.
Strict compliance with AGENTS.md, Zero-Vision-Library isolation (NO cv2, NO PIL).
All collage rendering is executed natively inside Playwright's headless Chromium HTML5 <canvas>.
Includes Anti-SSRF router, PII CSS blur (6px), 1440x900 incognito, and 2500-char policy snippet extractor.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page

from legal_sheaf.config import ADRADAR_DATA_DIR
from legal_sheaf.triad_decision_router import sanitize_before_external_decision

logger = logging.getLogger("vlm_context_engine.cropper")

REVIEW_DESK_DIR = ADRADAR_DATA_DIR / "review_desk"

PROHIBITED_EXTENSIONS = {
    ".exe", ".zip", ".bat", ".cmd", ".msi", ".sh", ".bin", ".iso", ".dmg",
    ".dll", ".scr", ".ps1", ".vbs", ".apk", ".deb", ".rpm"
}

PRIVATE_IP_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_ssrf_dangerous_url(target_url: str) -> bool:
    """
    Validates whether a URL attempts an SSRF against internal/private infrastructure
    or attempts to download executable binary/archive files.
    """
    try:
        parsed = urlparse(target_url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ("file", "ftp", "gopher", "tftp", "data"):
            return True

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False

        if hostname in ("localhost", "0.0.0.0", "::", "127.0.0.1"):
            return True

        # Check IP address against private subnets
        try:
            ip = ipaddress.ip_address(hostname)
            for net in PRIVATE_IP_NETWORKS:
                if ip in net:
                    return True
        except ValueError:
            pass

        # Check binary/executable download extensions
        path = (parsed.path or "").lower()
        if any(path.endswith(ext) for ext in PROHIBITED_EXTENSIONS):
            return True

        return False
    except Exception:
        return False


def _slugify(domain_or_url: str) -> str:
    """Converts URL or domain into a clean filesystem slug."""
    clean = domain_or_url.strip().lower()
    clean = re.sub(r"^https?://", "", clean).split("/")[0].split(":")[0]
    if clean.startswith("www."):
        clean = clean[4:]
    return re.sub(r"[^a-z0-9_-]", "_", clean)


def extract_policy_methods_snippet(
    policy_html_or_text: Optional[str],
    max_chars: int = 2500,
) -> str:
    """
    Extracts high-relevance paragraphs discussing processing methods, metrics, and third parties.
    Strips raw HTML tags and passes through 152-FZ PII Defense sanitizer (0 names, phones, emails).
    """
    if not policy_html_or_text:
        return "Текст Политики конфиденциальности не предоставлен для анализа методов."

    # Strip HTML tags
    clean_text = re.sub(r"<[^>]+>", " ", policy_html_or_text)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()

    triggers = [
        r"цел\w*\s+обработк",
        r"способ\w*\s+обработк",
        r"метод\w*\s+обработк",
        r"треть\w*\s+лиц",
        r"субобработчик",
        r"поручени\w*\s+на\s+обработку",
        r"cookie",
        r"куки",
        r"метрическ\w*",
        r"яндекс\.метрик",
        r"веб-аналитик",
    ]
    pattern = re.compile(r"(?i)(?:" + "|".join(triggers) + r")")

    paragraphs = clean_text.split(".")
    selected: List[str] = []
    current_len = 0

    for p in paragraphs:
        p_clean = p.strip()
        if not p_clean or len(p_clean) < 25:
            continue
        if pattern.search(p_clean):
            selected.append(p_clean + ".")
            current_len += len(p_clean)
            if current_len >= max_chars:
                break

    if not selected:
        selected_text = clean_text[:max_chars]
    else:
        selected_text = " ".join(selected)[:max_chars]

    # Run through 152-FZ PII sanitizer
    sanitized, _ = sanitize_before_external_decision(selected_text)
    return sanitized


async def _locate_text_anchor_rect(page: Page, patterns: List[str]) -> Optional[Dict[str, Any]]:
    """
    Scans DOM for text matching patterns to determine presence and exact sniper coordinates.
    Returns bounding box coordinates padded around the target text element.
    """
    js_code = """
    (patterns) => {
        const regexes = patterns.map(p => new RegExp(p, 'i'));
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const txt = node.nodeValue.trim();
            if (!txt || txt.length < 3) continue;
            for (const re of regexes) {
                if (re.test(txt)) {
                    let el = node.parentElement;
                    if (!el) continue;
                    // Ascend to block container if inline
                    while (el && el.parentElement && window.getComputedStyle(el).display === 'inline') {
                        el = el.parentElement;
                    }
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (rect.width > 10 && rect.height > 10 && style.visibility !== 'hidden' && style.display !== 'none') {
                        const absY = rect.top + window.scrollY;
                        const absX = rect.left + window.scrollX;
                        return {
                            found_text: txt.substring(0, 80),
                            x: Math.max(0, absX - 40),
                            y: Math.max(0, absY - 30),
                            width: Math.min(1440, Math.max(450, rect.width + 80)),
                            height: Math.min(650, Math.max(140, rect.height + 60)),
                            scrollY: Math.max(0, absY - 150),
                        };
                    }
                }
            }
        }
        return null;
    }
    """
    try:
        return await page.evaluate(js_code, patterns)
    except Exception as e:
        logger.warning(f"Error locating text anchor: {e}")
        return None


async def _capture_zone_screenshot(
    page: Page,
    zone_id: str,
    title: str,
    selectors: List[str],
    fallback_clip: Dict[str, float],
    text_patterns: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Two-stage sniper targeting:
    1. Text-anchored: uses text to verify presence and lock onto exact pixel coordinates.
    2. Fallback: captures container to record visual evidence of absence (if text not found).
    """
    element_found = False
    png_bytes: Optional[bytes] = None
    target_anchor: Optional[Dict[str, Any]] = None

    # Stage 1: Text-Anchored Sniper Targeting (Текст определяет наличие и место)
    if text_patterns:
        target_anchor = await _locate_text_anchor_rect(page, text_patterns)
        if target_anchor:
            try:
                # Scroll to element to ensure it's rendered in view
                await page.evaluate("(y) => window.scrollTo(0, y)", target_anchor["scrollY"])
                await page.wait_for_timeout(250)
                sniper_clip = {
                    "x": float(target_anchor["x"]),
                    "y": float(target_anchor["y"]),
                    "width": float(target_anchor["width"]),
                    "height": float(target_anchor["height"]),
                }
                png_bytes = await page.screenshot(clip=sniper_clip, full_page=True)
                if png_bytes:
                    element_found = True
                    title = f"{title} [Снайперский фокус: «{target_anchor['found_text'][:35]}...»]"
            except Exception as e:
                logger.warning(f"Sniper crop failed for {zone_id}: {e}")

    # Stage 2: Fallback to container selectors if text anchor was not targeted
    if not element_found or not png_bytes:
        for sel in selectors:
            try:
                elem = await page.query_selector(sel)
                if elem:
                    is_visible = await elem.is_visible()
                    box = await elem.bounding_box()
                    if is_visible and box and box["width"] > 50 and box["height"] > 20:
                        png_bytes = await elem.screenshot()
                        element_found = True
                        break
            except Exception:
                continue

    # Stage 3: Viewport coordinate clipping fallback (доказательство отсутствия)
    if not element_found or not png_bytes:
        try:
            png_bytes = await page.screenshot(clip=fallback_clip, full_page=True)
        except Exception:
            try:
                png_bytes = await page.screenshot()
            except Exception as e:
                logger.warning(f"Failed to capture fallback clip for {zone_id}: {e}")
                return None

    if not png_bytes:
        return None

    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "zone_id": zone_id,
        "title": title,
        "base64": b64,
        "png_bytes": png_bytes,
        "text_anchor_found": bool(target_anchor),
        "found_text": target_anchor.get("found_text") if target_anchor else "",
    }


async def _stitch_collage_in_browser(
    page: Page,
    zones: List[Dict[str, Any]],
) -> bytes:
    """
    Stitches captured zone crops into a unified PNG collage via HTML5 <canvas> inside Chromium.
    Eliminates dependency on PIL/Pillow or OpenCV.
    """
    payload = [
        {"title": z["title"], "dataUrl": f"data:image/png;base64,{z['base64']}"}
        for z in zones
    ]

    js_code = """
    async (items) => {
        // 1. Preload all images
        const loaded = await Promise.all(items.map(it => {
            return new Promise((resolve) => {
                const img = new Image();
                img.onload = () => resolve({ title: it.title, img: img });
                img.onerror = () => resolve(null);
                img.src = it.dataUrl;
            });
        }));

        const valid = loaded.filter(x => x !== null && x.img.width > 0);
        if (valid.length === 0) return "";

        const targetWidth = 1280;
        const bannerHeight = 44;
        const padding = 16;

        // Calculate total canvas height
        let totalHeight = padding;
        const layout = [];

        for (const it of valid) {
            const aspect = it.img.height / it.img.width;
            let drawW = targetWidth - (padding * 2);
            let drawH = Math.round(drawW * aspect);
            if (drawH > 650) {
                drawH = 650;
            }
            layout.push({
                title: it.title,
                img: it.img,
                x: padding,
                y: totalHeight + bannerHeight,
                w: drawW,
                h: drawH,
                bannerY: totalHeight,
            });
            totalHeight += bannerHeight + drawH + padding + 12;
        }

        if (totalHeight > 3600) {
            totalHeight = 3600;
        }

        const canvas = document.createElement('canvas');
        canvas.width = targetWidth;
        canvas.height = totalHeight;
        const ctx = canvas.getContext('2d');

        // Background
        ctx.fillStyle = '#0f172a';
        ctx.fillRect(0, 0, targetWidth, totalHeight);

        // Draw each block
        for (const b of layout) {
            if (b.y >= totalHeight) break;
            // Header banner
            ctx.fillStyle = '#1e293b';
            ctx.fillRect(b.x, b.bannerY, b.w, bannerHeight);

            // Red border tag indicator
            ctx.fillStyle = '#ef4444';
            ctx.fillRect(b.x, b.bannerY, 6, bannerHeight);

            // Title text
            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 16px system-ui, sans-serif';
            ctx.fillText(b.title, b.x + 16, b.bannerY + 28);

            // Draw image with red bounding box
            const maxDrawH = Math.min(b.h, totalHeight - b.y);
            if (maxDrawH > 0) {
                ctx.drawImage(b.img, b.x, b.y, b.w, maxDrawH);
                ctx.strokeStyle = '#ef4444';
                ctx.lineWidth = 3;
                ctx.strokeRect(b.x, b.y, b.w, maxDrawH);
            }
        }

        return canvas.toDataURL('image/jpeg', 0.85);
    }
    """

    data_url = await page.evaluate(js_code, payload)
    if not data_url or "," not in data_url:
        raise ValueError("Failed to render collage in browser canvas.")

    raw_b64 = data_url.split(",", 1)[1]
    return base64.b64decode(raw_b64)


async def capture_trigger_zones(
    url: str,
    audit_findings: Optional[List[Dict[str, Any]]] = None,
    output_dir: Optional[Path] = None,
    policy_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Navigates to URL, smoothly scrolls to ensure dynamic footer rendering,
    captures 3 critical trigger zones (Footer/Meta disclaimer, Form/Cookie, Hero/Claims),
    and stitches them into collage.png via headless Chromium canvas.
    """
    clean_url = url if url.startswith("http") else f"https://{url}"
    slug = _slugify(clean_url)

    target_dir = output_dir or (REVIEW_DESK_DIR / slug)
    target_dir.mkdir(parents=True, exist_ok=True)

    extracted_policy = extract_policy_methods_snippet(policy_text)

    captured_zones: List[Dict[str, Any]] = []
    meta_disclaimer_text: str = ""
    final_captured_url: str = clean_url
    captured_lang: str = "ru"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5"},
        )
        if is_ssrf_dangerous_url(clean_url):
            logger.warning(f"Aborting capture: Target URL {clean_url} is in prohibited SSRF range.")
            return {
                "domain": slug,
                "url": clean_url,
                "final_url": clean_url,
                "detected_lang": "unknown",
                "target_dir": str(target_dir),
                "collage_path": "",
                "collage_exists": False,
                "zones": [],
                "error": "SSRF_PROHIBITED_TARGET",
                "policy_methods_snippet": extracted_policy,
                "meta_disclaimer_text": "",
                "meta_disclaimer_found": False,
            }

        page = await context.new_page()

        # Install Playwright SSRF Route Interceptor
        async def route_handler(route):
            req_url = route.request.url
            if is_ssrf_dangerous_url(req_url):
                logger.warning(f"Blocked SSRF/prohibited route request to {req_url}")
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        try:
            logger.info(f"Opening {clean_url} in headless Chromium...")
            await page.goto(clean_url, wait_until="domcontentloaded", timeout=12000)

            # Auto-detect English landing page and switch to Russian version if available
            try:
                curr_url = page.url
                html_txt = await page.content()
                html_lang = await page.evaluate("() => (document.documentElement.lang || '').toLowerCase()")
                clean_t = re.sub(r"(?is)<(?:script|style|svg)[^>]*>.*?</(?:script|style|svg)>", " ", html_txt[:60000])
                clean_t = re.sub(r"<[^>]+>", " ", clean_t)
                cyr_c = len(re.findall(r"[\u0400-\u04FF]", clean_t))
                lat_c = len(re.findall(r"[A-Za-z]", clean_t))
                tot_c = cyr_c + lat_c
                cyr_ratio = (cyr_c / tot_c) if tot_c > 0 else 0.0

                is_russian = html_lang.startswith("ru") or "/ru" in curr_url or cyr_ratio >= 0.2
                if not is_russian and "/ru" not in curr_url:
                    ru_target = await page.evaluate("""
                    () => {
                        const altLink = document.querySelector('link[rel="alternate"][hreflang^="ru"]');
                        if (altLink && altLink.href) return altLink.href;
                        const aRu = document.querySelector('a[href*="/ru/"], a[href$="/ru"]');
                        if (aRu && aRu.href) return aRu.href;
                        return null;
                    }
                    """)
                    if ru_target:
                        logger.info(f"Auto-switching from English landing {curr_url} to Russian version: {ru_target}")
                        await page.goto(ru_target, wait_until="domcontentloaded", timeout=12000)
                    else:
                        try_ru_url = urllib.parse.urljoin(curr_url, "/ru/")
                        try:
                            resp_ru = await page.goto(try_ru_url, wait_until="domcontentloaded", timeout=8000)
                            if resp_ru and resp_ru.status >= 400:
                                await page.goto(curr_url, wait_until="domcontentloaded", timeout=8000)
                        except Exception:
                            pass

                final_captured_url = page.url
                post_html = await page.content()
                post_clean = re.sub(r"(?is)<(?:script|style|svg)[^>]*>.*?</(?:script|style|svg)>", " ", post_html[:60000])
                post_clean = re.sub(r"<[^>]+>", " ", post_clean)
                cyr_post = len(re.findall(r"[\u0400-\u04FF]", post_clean))
                lat_post = len(re.findall(r"[A-Za-z]", post_clean))
                post_lang = await page.evaluate("() => (document.documentElement.lang || '').toLowerCase()")
                captured_lang = "ru" if post_lang.startswith("ru") or "/ru" in final_captured_url or cyr_post >= 0.2 * (cyr_post + lat_post) else "en"
            except Exception as e_lang:
                logger.warning(f"Error during auto-language switch check: {e_lang}")
                final_captured_url = page.url

            # Smooth scroll to bottom to trigger lazy-loaded footers and disclaimers
            await page.evaluate("""
            async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    const distance = 350;
                    const timer = setInterval(() => {
                        const scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if (totalHeight >= scrollHeight) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, 80);
                });
            }
            """)
            await page.wait_for_timeout(2000)

            # Total page scroll height
            scroll_h = await page.evaluate("() => document.body.scrollHeight")
            scroll_h = max(900, scroll_h)

            # Detect Meta disclaimer in DOM (ensures detection even if styled small)
            meta_disclaimer_text = await page.evaluate("""
            () => {
                const footer = document.querySelector('footer, [role="contentinfo"], .footer, #footer');
                const text = (footer ? footer.innerText : '') + '\\n' + (document.body ? document.body.innerText : '');
                const lines = text.split('\\n');
                for (const line of lines) {
                    const l = line.trim();
                    if (/meta/i.test(l) && /(?:запрещен|экстремист|признан)/i.test(l)) {
                        return l;
                    }
                }
                return "";
            }
            """)

            # Inject CSS-blur for personal phone numbers and emails (152-FZ PII Defense) with 6px blur
            await page.evaluate("""
            () => {
                const style = document.createElement('style');
                style.id = 'adradar-pii-blur';
                style.textContent = `
                    .adradar-pii-masked {
                        filter: blur(6px) !important;
                        user-select: none !important;
                    }
                `;
                document.head.appendChild(style);

                const phoneRegex = /(?:\\+7|8)[\\s\\-(]?\\d{3}[\\s\\-)]?\\d{3}[\\s\\-]?\\d{2}[\\s\\-]?\\d{2}/;
                const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/;

                function walkTextNodes(node) {
                    if (node.nodeType === Node.TEXT_NODE) {
                        const val = node.nodeValue;
                        if (phoneRegex.test(val) || emailRegex.test(val)) {
                            if (node.parentElement && !node.parentElement.classList.contains('adradar-pii-masked')) {
                                node.parentElement.classList.add('adradar-pii-masked');
                            }
                        }
                    } else if (node.nodeType === Node.ELEMENT_NODE) {
                        if (node.tagName !== 'SCRIPT' && node.tagName !== 'STYLE') {
                            for (let child of node.childNodes) {
                                walkTextNodes(child);
                            }
                        }
                    }
                }
                if (document.body) {
                    walkTextNodes(document.body);
                }
            }
            """)

            # 1. ZONE #1: FOOTER (Requisites, Meta/socials disclaimers, asterisks *)
            # IMPORTANT: Hide fixed overlays (cookie banners, modals, chat widgets) so they don't cover footer!
            await page.evaluate("""
            () => {
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed') {
                        el.style.setProperty('display', 'none', 'important');
                    }
                }
            }
            """)

            footer_el = await page.query_selector("footer, [role='contentinfo'], .footer, #footer")
            if footer_el:
                try:
                    await footer_el.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                except Exception:
                    pass

            footer_selectors = [
                "footer",
                "[role='contentinfo']",
                ".footer",
                "#footer",
                ".site-footer",
                ".t-footer",
                ".b-footer",
            ]
            footer_clip = {
                "x": 0,
                "y": max(0, scroll_h - 700),
                "width": 1440,
                "height": min(700, scroll_h),
            }
            z1_title = "ZONE #1: FOOTER (Реквизиты, Соцсети, Сноски * Meta)"
            if meta_disclaimer_text:
                z1_title += f" [Сноска найдена: {meta_disclaimer_text[:40]}...]"

            z1 = await _capture_zone_screenshot(
                page=page,
                zone_id="ZONE_1_FOOTER",
                title=z1_title,
                selectors=footer_selectors,
                fallback_clip=footer_clip,
                text_patterns=[
                    r"meta.*(?:запрещен|экстремист|признан)",
                    r"(?:инн|огрн)\s*\d{10,13}",
                    r"политик\w*\s+конфиденциальн",
                ],
            )
            if z1:
                z1_path = target_dir / "zone_1_footer.png"
                z1_path.write_bytes(z1["png_bytes"])
                z1["file_name"] = "zone_1_footer.png"
                z1["file_path"] = str(z1_path)
                captured_zones.append(z1)

            # 2. ZONE #2: FORM_AND_COOKIE (Input fields, checkboxes, cookie-banner)
            form_selectors = [
                "form",
                ".cookie-banner",
                "[id*='cookie']",
                "[class*='cookie']",
                ".t-form",
                ".b-form",
                "[class*='modal'] form",
            ]
            form_clip = {
                "x": 0,
                "y": max(0, scroll_h // 2 - 350),
                "width": 1440,
                "height": 700,
            }
            z2 = await _capture_zone_screenshot(
                page=page,
                zone_id="ZONE_2_FORM",
                title="ZONE #2: FORM & COOKIE (Форма сбора ПДн, Чекбоксы, Плашка cookie)",
                selectors=form_selectors,
                fallback_clip=form_clip,
                text_patterns=[
                    r"согласи\w*\s+на\s+обработк",
                    r"персональн\w*\s+данн",
                    r"файлы\s+cookie",
                    r"cookie",
                ],
            )
            if z2:
                z2_path = target_dir / "zone_2_form.png"
                z2_path.write_bytes(z2["png_bytes"])
                z2["file_name"] = "zone_2_form.png"
                z2["file_path"] = str(z2_path)
                captured_zones.append(z2)

            # 3. ZONE #3: HERO_OR_PROMO (Top banners, slogans, 168-FZ words)
            hero_selectors = [
                ".hero",
                "header",
                "[class*='hero']",
                "[class*='banner']",
                "main > section:first-child",
                "#hero",
            ]
            hero_clip = {
                "x": 0,
                "y": 0,
                "width": 1440,
                "height": 650,
            }
            z3 = await _capture_zone_screenshot(
                page=page,
                zone_id="ZONE_3_HERO",
                title="ZONE #3: HERO & PROMO (Слоганы «№1 / Рассрочка», Англицизмы 168-ФЗ)",
                selectors=hero_selectors,
                fallback_clip=hero_clip,
                text_patterns=[
                    r"№\s*1\b",
                    r"лучш\w+\b",
                    r"лидер\b",
                    r"рассрочк\w*\s+0%",
                ],
            )
            if z3:
                z3_path = target_dir / "zone_3_hero.png"
                z3_path.write_bytes(z3["png_bytes"])
                z3["file_name"] = "zone_3_hero.png"
                z3["file_path"] = str(z3_path)
                captured_zones.append(z3)

            # Stitch collage inside the Chromium page context
            c_jpg = target_dir / "collage.jpg"
            c_png = target_dir / "collage.png"
            if captured_zones:
                collage_bytes = await _stitch_collage_in_browser(page, captured_zones)
                c_jpg.write_bytes(collage_bytes)
                c_png.write_bytes(collage_bytes)
                collage_path = c_jpg
            else:
                c_jpg.write_bytes(b"")
                c_png.write_bytes(b"")
                collage_path = c_jpg

        except Exception as e:
            logger.error(f"Error capturing trigger zones for {clean_url}: {e}")
            c_jpg = target_dir / "collage.jpg"
            c_png = target_dir / "collage.png"
            if not c_jpg.exists():
                c_jpg.write_bytes(b"")
            if not c_png.exists():
                c_png.write_bytes(b"")
            collage_path = c_jpg
        finally:
            await browser.close()

    # Clean payload for caller
    cleaned_zones = [
        {
            "zone_id": z["zone_id"],
            "title": z["title"],
            "file_name": z.get("file_name", ""),
            "file_path": z.get("file_path", ""),
        }
        for z in captured_zones
    ]

    return {
        "domain": slug,
        "url": final_captured_url or clean_url,
        "final_url": final_captured_url or clean_url,
        "detected_lang": captured_lang,
        "target_dir": str(target_dir),
        "collage_path": str(collage_path),
        "collage_exists": collage_path.exists() and collage_path.stat().st_size > 0,
        "zones": cleaned_zones,
        "policy_methods_snippet": extracted_policy,
        "meta_disclaimer_text": meta_disclaimer_text,
        "meta_disclaimer_found": bool(meta_disclaimer_text),
    }

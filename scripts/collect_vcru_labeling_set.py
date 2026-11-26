"""
Live VC.ru Article Collector & Objective Golden Set Builder.
Fetches real recent publications via public api.vc.ru v2.1/v2.8 endpoints:
1. Harvests a rich multi-rubric raw pool (120-150 live articles across
   marketing, services, ai, crypto, finance, dev, life) into:
   <ADRADAR_DATA_DIR>/data/vcru_pool_raw.jsonl
   strictly excluding all post_id and URLs present in golden_set_real_vcru.jsonl.
2. Saves anonymized real articles for fast human annotation to:
   <ADRADAR_DATA_DIR>/data/vcru_to_label.jsonl
3. Builds an objectively verifiable baseline set (>= 12 real VC.ru articles) at:
   legal_sheaf_verifier/tests/golden_set_real_vcru.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "legal_sheaf_verifier" / "src"
if str(PKG_SRC) not in sys.path:
    sys.path.insert(0, str(PKG_SRC))

from legal_sheaf.bundle import AdRadarBrainBundle
from legal_sheaf.config import ADRADAR_DATA_DIR, MODELS_CACHE_DIR
from legal_sheaf.dual_auditor import (
    PROMO_CODE_REGEX,
    is_affiliate_link,
    resolve_redirect_url,
)
from legal_sheaf.inn_erid_validators import (
    analyze_ad_label_status,
    extract_commercial_targets,
    extract_erid_tokens,
    find_valid_inns_in_text,
)
from legal_sheaf.schema import DocumentInput, LegalRegime

# Verified VC.ru rubric slugs and live fallback subsite IDs (verified via api.vc.ru/v2.8/subsite)
# Note: on VC.ru, Finance rubric URI is 'money' (id=199119).
DEFAULT_RUBRIC_SLUGS: Tuple[str, ...] = (
    "marketing",
    "services",
    "ai",
    "crypto",
    "money",
    "dev",
    "life",
)

VERIFIED_RUBRIC_FALLBACK_IDS: Dict[str, int] = {
    "marketing": 199113,
    "services": 200396,
    "ai": 332941,
    "crypto": 199126,
    "money": 199119,
    "dev": 235819,
    "life": 199123,
}


def _active_data_dir() -> Path:
    env_dir = os.environ.get("ADRADAR_DATA_DIR", "").strip()
    return Path(env_dir).resolve() if env_dir else Path(ADRADAR_DATA_DIR).resolve()


def _normalize_url_key(url: str) -> str:
    return str(url or "").strip().lower().rstrip("/")


def load_golden_exclusions(golden_path: Optional[Path] = None) -> Tuple[Set[int], Set[str]]:
    """
    Loads all post_ids and normalized URLs from golden_set_real_vcru.jsonl
    to enforce zero data leakage in raw labeling pools.
    """
    gpath = golden_path or (REPO_ROOT / "legal_sheaf_verifier" / "tests" / "golden_set_real_vcru.jsonl")
    blocked_ids: Set[int] = set()
    blocked_urls: Set[str] = set()
    if not gpath.exists():
        return blocked_ids, blocked_urls
    for raw_line in gpath.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
            if row.get("post_id") is not None:
                blocked_ids.add(int(row["post_id"]))
            if row.get("url"):
                blocked_urls.add(_normalize_url_key(str(row["url"])))
        except Exception:
            continue
    return blocked_ids, blocked_urls


def _load_bundle() -> Optional[AdRadarBrainBundle]:
    candidates = [
        _active_data_dir() / "models" / "champion_v3.bundle",
        MODELS_CACHE_DIR / "champion_v3.bundle",
        REPO_ROOT / "legal_sheaf_verifier" / "artifacts" / "champion_v3.bundle",
    ]
    for path in candidates:
        if path.exists():
            try:
                return AdRadarBrainBundle.load(path, verify_hash=True)
            except Exception:
                continue
    return None


def _build_smart_text_window(
    combined_text: str,
    paragraphs: List[str],
    commercial_targets: List[str],
) -> str:
    """
    Constructs a representative text_window without truncating the article tail:
    - Full text if <= 4500 chars;
    - Otherwise head (first 1200 chars) + paragraphs around links/promo/contacts + tail (last 1500 chars).
    """
    if len(combined_text) <= 4500:
        return combined_text

    head = combined_text[:1200]
    tail = combined_text[-1500:]
    middle_budget = 1600
    selected_mid: List[str] = []
    used = 0
    trigger_pat = re.compile(
        r"(?i)(?:https?://|t\.me/|max\.ru/|vk\.com/|промокод|скидк|erid|#реклам|@[a-z0-9_]{4,}|\+7)"
    )

    for idx, p in enumerate(paragraphs):
        p_norm = p.strip()
        if not p_norm or p_norm in head or p_norm in tail:
            continue
        has_hit = bool(trigger_pat.search(p_norm)) or any(t in p_norm for t in commercial_targets if len(t) >= 6)
        if has_hit:
            ctx_parts = []
            if idx > 0 and paragraphs[idx - 1].strip() not in head and paragraphs[idx - 1].strip() not in tail:
                ctx_parts.append(paragraphs[idx - 1].strip()[:240])
            ctx_parts.append(p_norm[:600])
            snippet = " ".join(ctx_parts)
            if snippet not in selected_mid and used + len(snippet) <= middle_budget:
                selected_mid.append(snippet)
                used += len(snippet)

    if selected_mid:
        return f"{head}\n...\n" + "\n...\n".join(selected_mid) + f"\n...\n{tail}"
    return f"{head}\n...\n{tail}"


def _extract_entry_fields(entry: Dict[str, Any], rubric_hint: str = "") -> Dict[str, Any]:
    post_id = int(entry.get("id") or 0)
    title = str(entry.get("title") or "").strip()
    url = str(entry.get("url") or f"https://vc.ru/{post_id}").strip()
    is_editorial = bool(entry.get("isEditorial"))
    ad_data = entry.get("ad_data") or entry.get("ad") or {}
    is_promo_flag = bool(
        entry.get("isPromo")
        or entry.get("is_promo")
        or ad_data
        or entry.get("isFlash")
    )

    subsite = entry.get("subsite") if isinstance(entry.get("subsite"), dict) else {}
    author = entry.get("author") if isinstance(entry.get("author"), dict) else {}
    subsite_uri = str(subsite.get("uri") or "").strip().lower()
    rubric = rubric_hint or subsite_uri or "general"
    if rubric == "money":
        rubric = "finance"

    author_name = str(author.get("name") or subsite.get("name") or "").strip()
    author_uri = str(author.get("uri") or subsite.get("uri") or "").strip()
    author_tg = str(
        author.get("tgChannelShortname")
        or subsite.get("tgChannelShortname")
        or ""
    ).strip()
    is_company_blog = bool(author.get("isCompany") or subsite.get("isCompany"))

    blocks = entry.get("blocks") or []
    text_parts: List[str] = []
    raw_links: List[str] = []
    first_header = ""
    for block in blocks:
        btype = str(block.get("type") or "").lower()
        bdata = block.get("data", {}) or {}
        raw_t = bdata.get("text", "")
        if isinstance(raw_t, str) and raw_t.strip():
            soup = BeautifulSoup(raw_t, "html.parser")
            cleaned = soup.get_text(separator=" ", strip=True)
            if cleaned:
                text_parts.append(cleaned)
                if not first_header and (btype == "header" or soup.find(["h1", "h2"])):
                    first_header = cleaned[:160]
            for a_tag in soup.find_all("a", href=True):
                raw_links.append(resolve_redirect_url(a_tag["href"].strip()))

        # Handle list blocks (items inside block.data.items)
        items_list = bdata.get("items")
        if isinstance(items_list, list):
            for li in items_list:
                li_str = li if isinstance(li, str) else (li.get("text", "") if isinstance(li, dict) else "")
                if isinstance(li_str, str) and li_str.strip():
                    soup_li = BeautifulSoup(li_str, "html.parser")
                    cl_li = soup_li.get_text(separator=" ", strip=True)
                    if cl_li:
                        text_parts.append(cl_li)
                    for a_tag in soup_li.find_all("a", href=True):
                        raw_links.append(resolve_redirect_url(a_tag["href"].strip()))

        lnk_obj = bdata.get("link")
        if isinstance(lnk_obj, dict) and isinstance(lnk_obj.get("url"), str):
            raw_links.append(resolve_redirect_url(lnk_obj["url"].strip()))
        elif isinstance(bdata.get("url"), str):
            raw_links.append(resolve_redirect_url(bdata["url"].strip()))

    full_text_raw = " ".join(text_parts).strip() or title
    if not title:
        if first_header:
            title = first_header
        elif full_text_raw:
            first_sent = re.split(r"(?<=[.!?])\s+", full_text_raw.strip())[0]
            title = (first_sent[:120] + "...") if len(first_sent) > 120 else first_sent

    combined_text = (
        f"{title}. {full_text_raw}".strip()
        if title and not full_text_raw.startswith(title)
        else full_text_raw
    )

    targets_info = extract_commercial_targets(
        combined_text,
        raw_links=raw_links,
        page_url=url,
    )
    external_links = list(targets_info["commercial_urls"])
    commercial_targets = list(targets_info["all_targets"])

    smart_window_raw = _build_smart_text_window(combined_text, text_parts, commercial_targets)
    anonymized_window = re.sub(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        "[email]",
        smart_window_raw,
    )
    anonymized_window = re.sub(
        r"(?<!\d)(?:\+7|8)[\s\-\(]*9\d{2}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)",
        "[phone]",
        anonymized_window,
    )

    erids = extract_erid_tokens(
        combined_text,
        page_url=" ".join(raw_links),
        platform_ad_meta=ad_data if isinstance(ad_data, dict) else None,
    )

    ad_label_info = analyze_ad_label_status(combined_text)
    has_reklama = bool(ad_label_info.get("has_strict_ad_label"))
    valid_inns = find_valid_inns_in_text(combined_text)
    has_affiliate = any(is_affiliate_link(lnk) for lnk in external_links)
    has_promo_code = bool(PROMO_CODE_REGEX.search(combined_text))

    if len(combined_text) <= 7200:
        model_text = combined_text
    else:
        mid_chunks = []
        for p in text_parts:
            if any(k in p.lower() for k in ("erid", "реклама", "промокод", "utm_", "ref=", "http", "t.me/", "max.ru/")):
                if p not in combined_text[:2500] and p not in combined_text[-3500:]:
                    mid_chunks.append(p[:400])
        mid_join = "\n".join(mid_chunks[:4])
        model_text = f"{combined_text[:2500]}\n...\n{mid_join}\n...\n{combined_text[-3500:]}" if mid_join else f"{combined_text[:3500]}\n...\n{combined_text[-3500:]}"
    if external_links:
        model_text = f"{model_text}\n[Ссылки: {' '.join(external_links[:6])}]"

    has_unlinked_promo_signals = bool(
        targets_info["all_targets"]
        or re.search(
            r"(?i)(?:#реклама|\b(?:промокод|скидк[а-я]*|под\s+ключ|закажите|оставьте\s+заявку|напишите\s+нам\s+в\s+лс|лс\s+канала|инн[:\s]*\d{10,12})\b)",
            combined_text,
        )
    )

    return {
        "post_id": post_id,
        "url": url,
        "rubric": rubric,
        "title": title[:220],
        "author_meta": {
            "name": author_name[:120],
            "uri": author_uri[:80],
            "tg_channel": author_tg[:80],
            "is_company": is_company_blog,
        },
        "paragraphs": [p[:900] for p in text_parts[:60]],
        "text_window": anonymized_window,
        "full_text": model_text,
        "external_links": external_links[:12],
        "commercial_targets": commercial_targets[:16],
        "is_editorial": is_editorial,
        "is_promo_platform_flag": is_promo_flag,
        "platform_ad_meta": ad_data if isinstance(ad_data, dict) else {},
        "erids": erids,
        "has_strict_ad_label": has_reklama,
        "valid_inns": valid_inns[:8],
        "valid_inns_count": len(valid_inns),
        "has_affiliate_link": has_affiliate,
        "has_promo_code": has_promo_code,
        "has_unlinked_promo_signals": has_unlinked_promo_signals,
    }


async def _fetch_post_detail(client: httpx.AsyncClient, post_id: int) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(f"https://api.vc.ru/v2.8/content?id={post_id}", timeout=10.0)
        if resp.status_code != 200:
            return None
        res = resp.json().get("result") or {}
        entry = res.get("entry") or res
        if isinstance(entry, dict) and entry.get("id"):
            return entry
    except Exception:
        return None
    return None


async def _resolve_rubric_subsite_ids(
    client: httpx.AsyncClient,
    slugs: Tuple[str, ...] = DEFAULT_RUBRIC_SLUGS,
) -> List[Tuple[str, int]]:
    resolved: List[Tuple[str, int]] = []
    seen_ids: Set[int] = set()
    for slug in slugs:
        sid: Optional[int] = None
        try:
            resp = await client.get(f"https://api.vc.ru/v2.8/subsite?uri={slug}", timeout=8.0)
            if resp.status_code == 200:
                res = resp.json().get("result") or {}
                raw_sid = res.get("id") or (res.get("subsite") or {}).get("id")
                if raw_sid:
                    sid = int(raw_sid)
        except Exception:
            sid = None
        if sid is None:
            sid = VERIFIED_RUBRIC_FALLBACK_IDS.get(slug)
        if sid and sid not in seen_ids:
            seen_ids.add(sid)
            canon = "finance" if slug == "money" else slug
            resolved.append((canon, sid))
    return resolved


async def harvest_vcru_pool_raw(
    target_pool_count: int = 140,
    scan_limit: int = 240,
    golden_path: Optional[Path] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Harvests 120-150 rich multi-rubric live VC.ru articles across
    marketing, services, ai, crypto, finance, dev, life while strictly
    excluding all post_ids and URLs present in golden_set_real_vcru.jsonl.
    """
    blocked_ids, blocked_urls = load_golden_exclusions(golden_path)
    bundle = _load_bundle()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    candidates_by_id: Dict[int, Tuple[Dict[str, Any], str]] = {}
    excluded_overlap = 0

    async with httpx.AsyncClient(headers=headers, timeout=12.0, follow_redirects=True) as client:
        rubric_pairs = await _resolve_rubric_subsite_ids(client)

        # Round-robin across rubrics via verified v2.8 subsitesIds parameter (2 pages = ~20-24 per rubric)
        # Page 1 of all 7 rubrics first, then Page 2 of all 7 rubrics, so every rubric (including 'life') is evenly represented
        feed_specs: List[Tuple[str, str, int]] = []
        for r_name, sid in rubric_pairs:
            feed_specs.append(
                (r_name, f"https://api.vc.ru/v2.8/timeline?markdown=false&subsitesIds={sid}&sorting=new", 2)
            )
        feed_specs.extend(
            [
                ("general", "https://api.vc.ru/v2.8/timeline?markdown=false&sorting=new", 1),
            ]
        )

        for rubric_tag, base_url, max_pages in feed_specs:
            if len(candidates_by_id) >= scan_limit:
                break
            last_id = None
            last_val = None
            for _ in range(max_pages):
                if len(candidates_by_id) >= scan_limit:
                    break
                page_url = base_url
                if last_id is not None and last_val is not None:
                    page_url += f"&lastId={last_id}&lastSortingValue={last_val}"
                try:
                    resp = await client.get(page_url, timeout=10.0)
                    if resp.status_code != 200:
                        break
                    data = resp.json().get("result") or {}
                    items = data.get("items") or []
                    if not items:
                        break
                    last_id = data.get("lastId")
                    last_val = data.get("lastSortingValue")
                    for it in items:
                        d = it.get("data") or {}
                        pid = int(d.get("id") or 0)
                        cand_url = _normalize_url_key(str(d.get("url") or f"https://vc.ru/{pid}"))
                        if not pid:
                            continue
                        if pid in blocked_ids or (cand_url and cand_url in blocked_urls):
                            excluded_overlap += 1
                            continue
                        if pid not in candidates_by_id:
                            candidates_by_id[pid] = (d, rubric_tag)
                    await asyncio.sleep(0.05)
                except Exception:
                    break

        # Fetch full v2.8 content in bounded concurrency (semaphore=6)
        sem = asyncio.Semaphore(6)

        async def _enrich_one(pid: int, shallow: Dict[str, Any], r_tag: str) -> Optional[Dict[str, Any]]:
            async with sem:
                detail = await _fetch_post_detail(client, pid)
                entry_obj = detail or shallow
                fields = _extract_entry_fields(entry_obj, rubric_hint=r_tag)
                norm_u = _normalize_url_key(fields["url"])
                if fields["post_id"] in blocked_ids or (norm_u and norm_u in blocked_urls):
                    return None
                if len(fields["text_window"]) < 60:
                    return None
                return fields

        tasks = [
            _enrich_one(pid, shallow, r_tag)
            for pid, (shallow, r_tag) in list(candidates_by_id.items())[:scan_limit]
        ]
        enriched_raw = await asyncio.gather(*tasks)

    # Interleave enriched items by rubric so target_pool_count gets balanced coverage across all 7 rubrics
    by_rubric_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for fields in enriched_raw:
        if fields is None:
            continue
        if fields["post_id"] in blocked_ids or _normalize_url_key(fields["url"]) in blocked_urls:
            excluded_overlap += 1
            continue
        by_rubric_buckets.setdefault(fields["rubric"], []).append(fields)

    interleaved_fields: List[Dict[str, Any]] = []
    bucket_keys = list(by_rubric_buckets.keys())
    max_bucket_len = max((len(v) for v in by_rubric_buckets.values()), default=0)
    for depth_idx in range(max_bucket_len):
        for rk in bucket_keys:
            b_list = by_rubric_buckets[rk]
            if depth_idx < len(b_list):
                interleaved_fields.append(b_list[depth_idx])

    pool_items: List[Dict[str, Any]] = []
    rubric_counts: Dict[str, int] = {}
    for fields in interleaved_fields:

        model_verdict: Dict[str, Any] = {}
        if bundle is not None:
            doc = DocumentInput(
                clean_text=fields["full_text"],
                page_url=fields["url"],
                author_domain="vc.ru",
                platform_ad_meta=fields["platform_ad_meta"] or {},
                context_mode="EXTERNAL_PUBLICATION",
            )
            rep = bundle.verify_document(doc)
            model_verdict = {
                "is_violation": bool(rep.is_violation),
                "violation_probability": round(float(rep.violation_probability), 4),
                "regime": rep.regime_name,
                "conformal_verdict": rep.conformal_verdict,
                "total_sheaf_energy": round(float(rep.total_sheaf_energy), 4),
            }

        row = {
            "post_id": fields["post_id"],
            "url": fields["url"],
            "rubric": fields["rubric"],
            "title": fields["title"],
            "author_meta": fields["author_meta"],
            "paragraphs": fields["paragraphs"],
            "text_window": fields["text_window"],
            "full_text": fields["full_text"],
            "external_links": fields["external_links"],
            "commercial_targets": fields["commercial_targets"],
            "is_promo": fields["is_promo_platform_flag"],
            "is_editorial": fields["is_editorial"],
            "platform_ad_meta": fields["platform_ad_meta"],
            "erids": fields["erids"],
            "has_strict_ad_label": fields["has_strict_ad_label"],
            "valid_inns": fields["valid_inns"],
            "has_affiliate_link": fields["has_affiliate_link"],
            "has_promo_code": fields["has_promo_code"],
            "has_unlinked_promo_signals": fields["has_unlinked_promo_signals"],
            "model_verdict": model_verdict,
            "human_label": None,
            "collected_at": datetime.now().isoformat(),
        }
        pool_items.append(row)
        r_key = fields["rubric"]
        rubric_counts[r_key] = rubric_counts.get(r_key, 0) + 1
        if len(pool_items) >= target_pool_count:
            break

    summary = {
        "pool_count": len(pool_items),
        "excluded_golden_overlap": excluded_overlap,
        "blocked_golden_ids_count": len(blocked_ids),
        "rubric_breakdown": rubric_counts,
    }
    return pool_items, summary


async def harvest_vcru_live(
    target_label_count: int = 48,
    min_golden_count: int = 12,
    scan_limit: int = 180,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    bundle = _load_bundle()
    blocked_ids, blocked_urls = load_golden_exclusions()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    raw_entries_by_id: Dict[int, Dict[str, Any]] = {}
    async with httpx.AsyncClient(headers=headers, timeout=12.0, follow_redirects=True) as client:
        case_3255318 = await _fetch_post_detail(client, 3255318)
        if case_3255318:
            raw_entries_by_id[3255318] = case_3255318

        feeds = [
            "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=date",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=popular",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=0&sorting=week",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=199113&sorting=date",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=200396&sorting=date",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=332941&sorting=date",
            "https://api.vc.ru/v2.1/timeline?subsiteIds=199126&sorting=date",
        ]

        for base_url in feeds:
            if len(raw_entries_by_id) >= scan_limit:
                break
            last_id = None
            last_val = None
            for _ in range(6):
                if len(raw_entries_by_id) >= scan_limit:
                    break
                page_url = base_url
                if last_id is not None and last_val is not None:
                    page_url += f"&lastId={last_id}&lastSortingValue={last_val}"
                try:
                    resp = await client.get(page_url, timeout=10.0)
                    if resp.status_code != 200:
                        break
                    data = resp.json().get("result") or {}
                    items = data.get("items") or []
                    if not items:
                        break
                    last_id = data.get("lastId")
                    last_val = data.get("lastSortingValue")
                    for it in items:
                        d = it.get("data") or {}
                        pid = int(d.get("id") or 0)
                        if pid and pid not in raw_entries_by_id:
                            raw_entries_by_id[pid] = d
                    await asyncio.sleep(0.08)
                except Exception:
                    break

        extracted: List[Dict[str, Any]] = []
        for pid, shallow_entry in list(raw_entries_by_id.items()):
            full_entry = shallow_entry if pid == 3255318 else (await _fetch_post_detail(client, pid) or shallow_entry)
            fields = _extract_entry_fields(full_entry)
            if len(fields["text_window"]) < 40:
                continue

            model_verdict: Dict[str, Any] = {}
            if bundle is not None:
                doc = DocumentInput(
                    clean_text=fields["full_text"],
                    page_url=fields["url"],
                    author_domain="vc.ru",
                    platform_ad_meta=fields["platform_ad_meta"] or {},
                    context_mode="EXTERNAL_PUBLICATION",
                )
                rep = bundle.verify_document(doc)
                model_verdict = {
                    "is_violation": bool(rep.is_violation),
                    "violation_probability": round(float(rep.violation_probability), 4),
                    "regime": rep.regime_name,
                    "conformal_verdict": rep.conformal_verdict,
                }
            fields["model_verdict"] = model_verdict
            extracted.append(fields)
            await asyncio.sleep(0.03)

    # 1. Select diverse live articles for human labeling (excluding existing golden IDs)
    to_label_items: List[Dict[str, Any]] = []
    for item in extracted:
        if item["post_id"] in blocked_ids or _normalize_url_key(item["url"]) in blocked_urls:
            continue
        to_label_items.append(
            {
                "post_id": item["post_id"],
                "url": item["url"],
                "rubric": item.get("rubric", "general"),
                "title": item["title"],
                "text_window": item["text_window"],
                "external_links": item["external_links"],
                "commercial_targets": item["commercial_targets"],
                "is_promo": item["is_promo_platform_flag"],
                "is_editorial": item["is_editorial"],
                "erids": item["erids"],
                "has_strict_ad_label": item["has_strict_ad_label"],
                "model_verdict": item["model_verdict"],
                "human_label": None,
                "collected_at": datetime.now().isoformat(),
            }
        )
        if len(to_label_items) >= target_label_count:
            break

    # 2. Build objectively verifiable Golden Set (>= 12 real VC.ru examples)
    golden_items: List[Dict[str, Any]] = []
    seen_golden_ids = set()

    def _add_golden(it: Dict[str, Any], expected_viol: bool, expected_regime: LegalRegime, basis: str) -> None:
        if it["post_id"] in seen_golden_ids:
            return
        seen_golden_ids.add(it["post_id"])
        golden_items.append(
            {
                "post_id": it["post_id"],
                "url": it["url"],
                "title": it["title"],
                "author_domain": "vc.ru",
                "text": it["full_text"],
                "external_links": it["external_links"],
                "is_promo_platform_flag": it["is_promo_platform_flag"],
                "erids": it["erids"],
                "platform_ad_meta": it["platform_ad_meta"],
                "expected_is_violation": expected_viol,
                "expected_regime": expected_regime.name,
                "expected_regime_id": int(expected_regime.value),
                "label_basis": basis,
                "collected_at": datetime.now().isoformat(),
            }
        )

    for it in extracted:
        if it["post_id"] == 3255318:
            _add_golden(
                it,
                expected_viol=True,
                expected_regime=LegalRegime.UNMARKED_AD_NO_ERID,
                basis="verified_case_3255318_promocode_utm_no_erid",
            )

    for it in extracted:
        if len(it["erids"]) > 0 and (it["is_promo_platform_flag"] or it["has_strict_ad_label"]):
            _add_golden(
                it,
                expected_viol=False,
                expected_regime=LegalRegime.CLEAN_MARKED_AD,
                basis="official_vcru_marked_ad_with_erid",
            )
        if sum(1 for g in golden_items if g["expected_regime"] == "CLEAN_MARKED_AD") >= 4:
            break

    domain_counts: Dict[str, int] = {}
    for g in golden_items:
        for lnk in g.get("external_links") or []:
            d = urllib.parse.urlparse(lnk).netloc.lower().split(":")[0].removeprefix("www.")
            if d:
                domain_counts[d] = domain_counts.get(d, 0) + 1

    for it in extracted:
        if it["post_id"] in seen_golden_ids:
            continue
        if (it["has_affiliate_link"] or it["has_promo_code"]) and it["external_links"] and not it["erids"]:
            first_dom = urllib.parse.urlparse(it["external_links"][0]).netloc.lower().split(":")[0].removeprefix("www.")
            if domain_counts.get(first_dom, 0) >= 2:
                continue
            domain_counts[first_dom] = domain_counts.get(first_dom, 0) + 1
            _add_golden(
                it,
                expected_viol=True,
                expected_regime=LegalRegime.UNMARKED_AD_NO_ERID,
                basis="live_affiliate_link_or_promocode_no_erid",
            )
        if sum(1 for g in golden_items if g["expected_is_violation"]) >= 6:
            break

    for it in extracted:
        if it["post_id"] in seen_golden_ids:
            continue
        if (
            not it["external_links"]
            and not it["erids"]
            and not it["is_promo_platform_flag"]
            and not it["has_promo_code"]
            and not it["has_strict_ad_label"]
            and not it["has_unlinked_promo_signals"]
            and (it["is_editorial"] or len(it["text_window"]) >= 180)
        ):
            _add_golden(
                it,
                expected_viol=False,
                expected_regime=LegalRegime.CLEAN_NON_AD,
                basis="editorial_or_news_zero_commercial_links",
            )
        if len(golden_items) >= max(15, min_golden_count) and sum(
            1 for g in golden_items if g["expected_regime"] == "CLEAN_NON_AD"
        ) >= 6:
            break

    if len(golden_items) < min_golden_count:
        for it in extracted:
            if it["post_id"] in seen_golden_ids:
                continue
            if it["erids"] and it["has_strict_ad_label"]:
                _add_golden(
                    it,
                    expected_viol=False,
                    expected_regime=LegalRegime.CLEAN_MARKED_AD,
                    basis="live_post_with_erid_and_ad_label",
                )
            elif (
                not it["external_links"]
                and not it["is_promo_platform_flag"]
                and not it["has_promo_code"]
                and not it["has_strict_ad_label"]
                and not it["has_unlinked_promo_signals"]
            ):
                _add_golden(
                    it,
                    expected_viol=False,
                    expected_regime=LegalRegime.CLEAN_NON_AD,
                    basis="live_post_zero_commercial_links",
                )
            if len(golden_items) >= min_golden_count:
                break

    return to_label_items, golden_items


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect live VC.ru articles for ensemble labeling & objective Golden Set")
    parser.add_argument("--target-label", type=int, default=48, help="Number of articles in vcru_to_label.jsonl")
    parser.add_argument("--target-pool", type=int, default=140, help="Number of articles in vcru_pool_raw.jsonl (120-150)")
    parser.add_argument("--min-golden", type=int, default=14, help="Minimum articles in golden_set_real_vcru.jsonl")
    parser.add_argument("--scan-limit", type=int, default=240, help="Max posts scanned across VC.ru timelines")
    parser.add_argument(
        "--pool-only",
        action="store_true",
        help="Harvest multi-rubric raw pool into vcru_pool_raw.jsonl excluding golden_set_real_vcru.jsonl",
    )
    parser.add_argument(
        "--refresh-label-set-only",
        action="store_true",
        help="Refresh C:\\AdRadarData\\data\\vcru_to_label.jsonl without overwriting golden_set_real_vcru.jsonl",
    )
    args = parser.parse_args()

    data_dir = _active_data_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    golden_out = REPO_ROOT / "legal_sheaf_verifier" / "tests" / "golden_set_real_vcru.jsonl"
    golden_out.parent.mkdir(parents=True, exist_ok=True)

    if args.pool_only:
        pool_out = data_dir / "vcru_pool_raw.jsonl"
        print(f"[*] Harvesting multi-rubric VC.ru raw pool (target={args.target_pool}, scan_limit={args.scan_limit})...")
        pool_rows, summary = asyncio.run(
            harvest_vcru_pool_raw(
                target_pool_count=args.target_pool,
                scan_limit=args.scan_limit,
                golden_path=golden_out,
            )
        )
        with open(pool_out, "w", encoding="utf-8") as f:
            for row in pool_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[+] Saved {len(pool_rows)} raw pool articles -> {pool_out}")
        print(
            f"    Excluded golden overlap: {summary['excluded_golden_overlap']} "
            f"(blocked IDs={summary['blocked_golden_ids_count']})"
        )
        print(f"    Rubrics: {json.dumps(summary['rubric_breakdown'], ensure_ascii=False)}")
        return 0

    data_out = data_dir / "vcru_to_label.jsonl"
    print(f"[*] Collecting live VC.ru articles (scan_limit={args.scan_limit})...")
    to_label, golden = asyncio.run(
        harvest_vcru_live(
            target_label_count=args.target_label,
            min_golden_count=args.min_golden,
            scan_limit=args.scan_limit,
        )
    )

    with open(data_out, "w", encoding="utf-8") as f:
        for row in to_label:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[+] Saved {len(to_label)} articles for labeling -> {data_out}")
    if args.refresh_label_set_only:
        print(f"[i] --refresh-label-set-only active: preserved existing {golden_out}")
        return 0

    with open(golden_out, "w", encoding="utf-8") as f:
        for row in golden:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    viol_cnt = sum(1 for g in golden if g["expected_is_violation"])
    marked_cnt = sum(1 for g in golden if g["expected_regime"] == "CLEAN_MARKED_AD")
    clean_cnt = sum(1 for g in golden if g["expected_regime"] == "CLEAN_NON_AD")

    print(f"[+] Saved {len(golden)} objective real VC.ru golden cases -> {golden_out}")
    print(f"    Breakdown: UNMARKED_AD={viol_cnt}, CLEAN_MARKED_AD={marked_cnt}, CLEAN_NON_AD={clean_cnt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

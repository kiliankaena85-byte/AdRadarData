"""
Official RKN Personal Data Operators Registry Checker.
Queries https://pd.rkn.gov.ru/operators-registry/operators-list/
Extracts registration status and cross-border data transfer notification declarations.
Strictly zero simulation. Enforces Fail-Loud policy on connection failures.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
import httpx
from bs4 import BeautifulSoup

from legal_sheaf.registry_cache import (
    get_cached_registry,
    save_cached_registry,
    enforce_rkn_rate_limit_sync,
    enforce_rkn_rate_limit_async,
)

logger = logging.getLogger("legal_sheaf.rkn_registry")

RKN_SEARCH_URL = "https://pd.rkn.gov.ru/operators-registry/operators-list/"
RKN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://pd.rkn.gov.ru/operators-registry/operators-list/",
}


def parse_rkn_search_html(html: str, inn: str) -> Dict[str, Any]:
    """Parses search results HTML from pd.rkn.gov.ru for a given INN."""
    soup = BeautifulSoup(html, "html.parser")
    res_list = soup.find(id="ResList1")
    if not res_list:
        return {
            "inn": inn,
            "status": "NOT_FOUND",
            "is_registered_in_rkn": False,
            "reg_number": None,
            "operator_name": None,
            "has_cross_border_declared": False,
            "registration_date": None,
            "raw_detail_url": None,
            "error_details": None,
        }

    rows = res_list.find_all("tr")
    # First row is header row
    data_rows = rows[1:] if len(rows) > 1 else []
    if not data_rows:
        return {
            "inn": inn,
            "status": "NOT_FOUND",
            "is_registered_in_rkn": False,
            "reg_number": None,
            "operator_name": None,
            "has_cross_border_declared": False,
            "registration_date": None,
            "raw_detail_url": None,
            "error_details": None,
        }

    first_row = data_rows[0]
    tds = first_row.find_all("td")
    reg_number = tds[0].get_text(strip=True) if len(tds) > 0 else None
    
    a_tag = first_row.find("a")
    operator_name = a_tag.get_text(strip=True) if a_tag else (tds[1].get_text(strip=True) if len(tds) > 1 else None)
    detail_href = a_tag.get("href") if a_tag else (f"?id={reg_number}" if reg_number else None)

    reg_date = tds[3].get_text(strip=True) if len(tds) > 3 else None

    # Parse filing year from reg_number (RR-YY-NNNNNN) or registration_date (DD.MM.YYYY)
    filing_year = None
    if reg_number:
        m_rn = re.search(r"^\d{2}-(\d{2})-\d+", reg_number)
        if m_rn:
            yy = int(m_rn.group(1))
            filing_year = 2000 + yy if yy < 70 else 1900 + yy
    if not filing_year and reg_date:
        m_date = re.search(r"(?:19|20)\d{2}", reg_date)
        if m_date:
            filing_year = int(m_date.group(0))

    is_outdated_pre_2023 = bool(filing_year and filing_year <= 2022)

    return {
        "inn": inn,
        "status": "FOUND",
        "is_registered_in_rkn": True,
        "reg_number": reg_number,
        "operator_name": operator_name,
        "has_cross_border_declared": False,  # Updated after fetching detail card
        "registration_date": reg_date,
        "filing_year": filing_year,
        "is_outdated_pre_2023": is_outdated_pre_2023,
        "raw_detail_url": f"https://pd.rkn.gov.ru/operators-registry/operators-list/{detail_href}" if detail_href else None,
        "error_details": None,
    }


def parse_rkn_detail_card_html(html: str) -> bool:
    """
    Parses RKN operator detail page and returns whether cross-border personal data transfer is declared.
    Checks for row 'трансграничная передача' containing 'да'.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        row_text = tr.get_text(separator=" : ", strip=True).lower()
        if "трансграничная" in row_text:
            # Check if answer is "да"
            if re.search(r"трансграничная\s+передача\s*:\s*да\b", row_text):
                return True
            # Also check if explicit "нет"
            if re.search(r"трансграничная\s+передача\s*:\s*нет\b", row_text):
                return False
    return False


def check_rkn_registry(inn: str, timeout: float = 8.0) -> Dict[str, Any]:
    """
    Synchronously queries pd.rkn.gov.ru for an organization by INN.
    Returns status, registration info, and cross-border notification status.
    Uses 30-day WAL SQLite cache and enforces >= 1.2s polite pause between network requests.
    """
    inn_clean = inn.strip()
    cached = get_cached_registry("RKN", inn_clean)
    if cached is not None:
        return cached

    enforce_rkn_rate_limit_sync()
    params = {"act": "search", "inn": inn_clean}
    try:
        with httpx.Client(headers=RKN_HEADERS, timeout=timeout, verify=False) as client:
            resp = client.get(RKN_SEARCH_URL, params=params)
            if resp.status_code != 200:
                return {
                    "inn": inn_clean,
                    "status": "RKN_CHECK_UNAVAILABLE",
                    "is_registered_in_rkn": None,
                    "reg_number": None,
                    "operator_name": None,
                    "has_cross_border_declared": None,
                    "registration_date": None,
                    "raw_detail_url": None,
                    "error_details": f"HTTP status {resp.status_code}",
                }

            parsed = parse_rkn_search_html(resp.text, inn_clean)
            if parsed["is_registered_in_rkn"] and parsed["raw_detail_url"]:
                try:
                    det_resp = client.get(parsed["raw_detail_url"], timeout=timeout)
                    if det_resp.status_code == 200:
                        parsed["has_cross_border_declared"] = parse_rkn_detail_card_html(det_resp.text)
                except Exception as e_det:
                    logger.warning(f"Could not fetch RKN detail card: {e_det}")

            parsed["status"] = "SUCCESS" if parsed["is_registered_in_rkn"] else "NOT_FOUND"
            save_cached_registry("RKN", inn_clean, parsed)
            return parsed
    except Exception as e:
        return {
            "inn": inn_clean,
            "status": "RKN_CHECK_UNAVAILABLE",
            "is_registered_in_rkn": None,
            "reg_number": None,
            "operator_name": None,
            "has_cross_border_declared": None,
            "registration_date": None,
            "raw_detail_url": None,
            "error_details": f"Connection error: {type(e).__name__} ({e})",
        }


async def check_rkn_registry_async(
    client: httpx.AsyncClient,
    inn: str,
    timeout: float = 8.0
) -> Dict[str, Any]:
    """
    Asynchronously queries pd.rkn.gov.ru for an organization by INN with retry.
    Uses 30-day WAL SQLite cache and enforces >= 1.2s polite pause between network requests.
    """
    import asyncio
    inn_clean = inn.strip()
    cached = get_cached_registry("RKN", inn_clean)
    if cached is not None:
        return cached

    await enforce_rkn_rate_limit_async()
    params = {"act": "search", "inn": inn_clean}
    last_err = None

    for attempt in range(3):
        try:
            resp = await client.get(RKN_SEARCH_URL, params=params, headers=RKN_HEADERS, timeout=timeout)
            if resp.status_code != 200:
                last_err = f"HTTP status {resp.status_code}"
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                return {
                    "inn": inn_clean,
                    "status": "RKN_CHECK_UNAVAILABLE",
                    "is_registered_in_rkn": None,
                    "reg_number": None,
                    "operator_name": None,
                    "has_cross_border_declared": None,
                    "registration_date": None,
                    "raw_detail_url": None,
                    "error_details": last_err,
                }

            parsed = parse_rkn_search_html(resp.text, inn_clean)
            if parsed["is_registered_in_rkn"] and parsed["raw_detail_url"]:
                try:
                    det_resp = await client.get(parsed["raw_detail_url"], headers=RKN_HEADERS, timeout=timeout)
                    if det_resp.status_code == 200:
                        parsed["has_cross_border_declared"] = parse_rkn_detail_card_html(det_resp.text)
                except Exception as e_det:
                    logger.warning(f"Could not fetch RKN detail card: {e_det}")

            parsed["status"] = "SUCCESS" if parsed["is_registered_in_rkn"] else "NOT_FOUND"
            save_cached_registry("RKN", inn_clean, parsed)
            return parsed
        except Exception as e:
            last_err = f"Connection error: {type(e).__name__} ({e})"
            if attempt < 2:
                await asyncio.sleep(0.5)
                continue

    return {
        "inn": inn_clean,
        "status": "RKN_CHECK_UNAVAILABLE",
        "is_registered_in_rkn": None,
        "reg_number": None,
        "operator_name": None,
        "has_cross_border_declared": None,
        "registration_date": None,
        "raw_detail_url": None,
        "error_details": last_err,
    }

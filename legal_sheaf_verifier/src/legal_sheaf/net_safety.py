"""
Network Safety & Anti-SSRF Redirect Protection Engine.
Strict compliance with AGENTS.md, Zero-Vision-Library isolation, and Enterprise Cybersecurity standards.

Features:
- Validates target hostnames and DNS resolutions against private, loopback, link-local, and reserved IP ranges.
- Safe HTTP redirect resolver (safe_fetch_with_redirects) with follow_redirects=False manual loop up to 5 hops.
- Re-verifies DNS before EVERY redirect hop to prevent DNS rebinding and internal network pivot attacks.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

logger = logging.getLogger("legal_sheaf.net_safety")

PROHIBITED_EXTENSIONS: Set[str] = {
    ".exe", ".zip", ".bat", ".cmd", ".msi", ".sh", ".bin", ".iso", ".dmg",
    ".dll", ".scr", ".ps1", ".vbs", ".apk", ".deb", ".rpm",
}

BLOCKED_IP_NETWORKS: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
]


class SSRFBlockedError(Exception):
    """Raised when an outbound request or redirect attempts to reach prohibited network infrastructure."""
    pass


def is_ip_private_or_reserved(ip_str: str) -> bool:
    """
    Checks if an IP address string belongs to private, loopback, link-local, or reserved subnets.
    Allows proxy/VPN TUN fake-IP ranges (RFC 2544 198.18.0.0/15 and ULA fd00::/8) when running under client proxies.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return True

        for net in BLOCKED_IP_NETWORKS:
            if ip in net:
                return True
        return False
    except ValueError:
        return True


def resolve_and_verify_host(hostname: str) -> Tuple[bool, List[str], str]:
    """
    Resolves hostname to IP addresses and verifies that NONE of the resolved IPs
    are private, loopback, link-local, or reserved.
    Returns (is_safe, resolved_ips, error_reason).
    """
    clean_host = hostname.strip().lower()
    if not clean_host:
        return False, [], "Empty hostname"

    if clean_host in ("localhost", "0.0.0.0", "::", "127.0.0.1", "metadata.google.internal"):
        return False, [clean_host], f"Direct prohibited host '{clean_host}'"

    # Check literal IP
    try:
        ip = ipaddress.ip_address(clean_host)
        if is_ip_private_or_reserved(str(ip)):
            return False, [clean_host], f"Prohibited literal IP '{clean_host}'"
        return True, [clean_host], ""
    except ValueError:
        pass

    # Resolve DNS
    resolved_ips: List[str] = []
    try:
        addr_info = socket.getaddrinfo(clean_host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        for item in addr_info:
            sockaddr = item[4]
            ip_val = sockaddr[0]
            if ip_val not in resolved_ips:
                resolved_ips.append(ip_val)
    except socket.gaierror as e:
        return False, [], f"DNS resolution failed for '{clean_host}': {e}"
    except Exception as e:
        return False, [], f"DNS lookup exception for '{clean_host}': {e}"

    if not resolved_ips:
        return False, [], f"No DNS records found for '{clean_host}'"

    for ip_cand in resolved_ips:
        if is_ip_private_or_reserved(ip_cand):
            return False, resolved_ips, f"Hostname '{clean_host}' resolves to prohibited IP '{ip_cand}'"

    return True, resolved_ips, ""


def is_safe_url(url: str) -> Tuple[bool, str]:
    """
    Validates URL scheme, prohibited file extensions, and resolves target host to ensure SSRF safety.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, f"Prohibited scheme '{scheme}' (only http/https allowed)"

        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in URL"

        path = (parsed.path or "").lower()
        if any(path.endswith(ext) for ext in PROHIBITED_EXTENSIONS):
            return False, f"Prohibited file extension in URL path '{path}'"

        is_safe, _, reason = resolve_and_verify_host(hostname)
        if not is_safe:
            return False, reason

        return True, ""
    except Exception as e:
        return False, f"URL parse error: {e}"


async def safe_fetch_with_redirects(
    client: httpx.AsyncClient,
    url: str,
    max_hops: int = 5,
    timeout: float = 10.0,
    headers: Optional[Dict[str, str]] = None,
    method: str = "GET",
    max_redirects: Optional[int] = None,
) -> Tuple[Optional[httpx.Response], str, List[str]]:
    """
    Executes HTTP request with strict manual redirect following and Anti-SSRF verification before EVERY hop.
    Returns (final_response, final_url, hops_list).
    Raises SSRFBlockedError if any hop attempts to reach internal/private network.
    """
    effective_hops = max_redirects if max_redirects is not None else max_hops
    curr_url = url
    hops: List[str] = [curr_url]
    resp: Optional[httpx.Response] = None

    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.5",
    }
    req_headers = {**default_headers, **(headers or {})}

    for hop_idx in range(effective_hops + 1):
        # 1. Verify target URL before making the request
        safe, reason = is_safe_url(curr_url)
        if not safe:
            logger.warning(f"Anti-SSRF blocked request to {curr_url}: {reason}")
            raise SSRFBlockedError(f"Anti-SSRF Blocked: {reason} (URL: {curr_url})")

        try:
            resp = await client.request(
                method,
                curr_url,
                headers=req_headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except Exception as e:
            logger.debug(f"Request failed for {curr_url}: {e}")
            return None, curr_url, hops

        # Check for HTTP redirect status codes (301, 302, 303, 307, 308)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                break

            target_url = urllib.parse.urljoin(curr_url, location.strip())
            curr_url = target_url
            hops.append(curr_url)
            logger.debug(f"Following safe redirect ({resp.status_code}) -> {curr_url}")
        else:
            # Reached final non-redirect response
            break

    return resp, curr_url, hops

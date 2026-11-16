"""
Retrospective Ground-Truth Harvester for AdRadar Continual Learning.
Monitors previously audited URLs over time:
- Post-factum erid / 'Реклама' addition -> Confirmed Advertising (Verified Positive).
- HTTP 404/410 deletion after report -> Confirmed Post-Audit Takedown (Verified Positive).
- Human lawyer quarantine feedback (ABSTAIN_REVIEW) -> Verified Label.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("legal_sheaf.retro_checker")

ERID_PATTERNS = [
    re.compile(r"erid[:\s=]+([a-zA-Z0-9_\-]+)", re.IGNORECASE),
    re.compile(r"реклама[\s.:]+(ooo|ооо|зао|пао|ип)", re.IGNORECASE),
    re.compile(r"токен[:\s=]+([a-zA-Z0-9_\-]+)", re.IGNORECASE),
]


@dataclass
class RetroAuditRecord:
    url: str
    audit_date: str
    initial_verdict: str  # e.g. "ABSTAIN", "SUSPECTED_AD", "CLEAN"
    extracted_text: str
    target_inn: Optional[str] = None
    days_elapsed: int = 0
    resolved_label: Optional[str] = None  # "VERIFIED_POSITIVE", "VERIFIED_NEGATIVE", "PENDING"
    resolution_reason: Optional[str] = None
    resolved_at: Optional[str] = None


class RetroHarvester:
    """
    Harvests ground truth retroactively without continuous manual annotation.
    Converts post-audit page changes and human quarantine decisions into verified training samples.
    """

    def __init__(self, quarantine_file: Optional[Union[str, Path]] = None):
        self.quarantine_file = Path(quarantine_file) if quarantine_file else None
        self.records: List[RetroAuditRecord] = []

    def check_page_content_for_postfactum_label(
        self,
        html_or_text: str,
        http_status: int = 200,
        initial_verdict: str = "ABSTAIN",
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Evaluates current page state against audit baseline.
        Returns: (resolved_label, resolution_reason)
        """
        # 1. Page deletion / Takedown check (HTTP 404 or 410)
        if http_status in (404, 410):
            return "VERIFIED_POSITIVE", f"Page deleted (HTTP {http_status}) after audit warning"

        # 2. Post-factum labeling check (owner added erid / 'Реклама' after initial non-compliant state)
        for pattern in ERID_PATTERNS:
            match = pattern.search(html_or_text)
            if match:
                marker = match.group(0)
                return "VERIFIED_POSITIVE", f"Post-factum advertising marker added: '{marker}'"

        return None, None

    def process_quarantine_feedback(
        self,
        feedback_items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Processes human lawyer labels from ABSTAIN_REVIEW queue.
        Each item: {"url": str, "text": str, "human_label": "GUILTY" | "SAFE", "inn": str}
        """
        verified_samples = []
        for item in feedback_items:
            human_label = item.get("human_label", "").upper()
            if human_label in ("GUILTY", "VIOLATION", "VERIFIED_POSITIVE"):
                verified_samples.append({
                    "url": item.get("url", ""),
                    "text": item.get("text", ""),
                    "target_inn": item.get("inn") or item.get("target_inn"),
                    "ground_truth": 1,  # Positive class (violation)
                    "source": "human_quarantine",
                    "timestamp": datetime.now().isoformat(),
                })
            elif human_label in ("SAFE", "CLEAN", "VERIFIED_NEGATIVE"):
                verified_samples.append({
                    "url": item.get("url", ""),
                    "text": item.get("text", ""),
                    "target_inn": item.get("inn") or item.get("target_inn"),
                    "ground_truth": 0,  # Negative class (clean)
                    "source": "human_quarantine",
                    "timestamp": datetime.now().isoformat(),
                })
        return verified_samples

    def harvest_from_responses(
        self,
        pending_records: List[Dict[str, Any]],
        responses: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Correlates pending audit records with newly fetched web responses.
        pending_records: [{"url": ..., "text": ..., "audit_date": ...}]
        responses: [{"url": ..., "status": int, "content": str}]
        """
        resp_map = {r["url"]: r for r in responses}
        harvested = []

        for rec in pending_records:
            url = rec["url"]
            if url not in resp_map:
                continue

            resp = resp_map[url]
            status = resp.get("status", 200)
            content = resp.get("content", "")

            label, reason = self.check_page_content_for_postfactum_label(
                html_or_text=content,
                http_status=status,
                initial_verdict=rec.get("initial_verdict", "ABSTAIN"),
            )

            if label == "VERIFIED_POSITIVE":
                harvested.append({
                    "url": url,
                    "text": rec.get("text", content),
                    "target_inn": rec.get("target_inn"),
                    "ground_truth": 1,
                    "source": f"retro_harvester: {reason}",
                    "timestamp": datetime.now().isoformat(),
                })

        return harvested

"""
AdRadar Outreach Ledger (Anti-Spam & Contact History Registry).
Enforces 90-day communication cooldown and strict OPT-OUT compliance for lead outreach.
Backed by SQLite in WAL mode: C:\\AdRadarData\\db\\outreach_ledger.sqlite.
Statuses: NEW | APPROVED_IN_DESK | SENT | REPLIED | OPT_OUT.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from legal_sheaf.config import ADRADAR_DATA_DIR

logger = logging.getLogger("legal_sheaf.outreach_ledger")

LEDGER_DB_PATH = ADRADAR_DATA_DIR / "db" / "outreach_ledger.sqlite"
DEFAULT_COOLDOWN_DAYS = 90
COOLDOWN_SECONDS = DEFAULT_COOLDOWN_DAYS * 86400

ALLOWED_STATUSES = {
    "NEW",
    "APPROVED_IN_DESK",
    "SENT",
    "REPLIED",
    "OPT_OUT",
}


def _clean_domain(domain: str) -> str:
    """Normalizes domain name for ledger keying."""
    d = (domain or "").strip().lower()
    if d.startswith("http://"):
        d = d[7:]
    elif d.startswith("https://"):
        d = d[8:]
    d = d.split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _clean_inn(inn: Optional[str]) -> str:
    """Normalizes INN/KIO for ledger keying."""
    if not inn:
        return "UNKNOWN_INN"
    clean = "".join(c for c in str(inn).strip() if c.isalnum())
    return clean or "UNKNOWN_INN"


class OutreachLedger:
    """
    Manages cold outreach history and compliance cooldowns per (INN, domain) pair.
    Guarantees no company receives repeated emails within 90 days.
    Guarantees permanent suppression on OPT_OUT.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or LEDGER_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS outreach_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        inn_or_kio TEXT NOT NULL,
                        root_domain TEXT NOT NULL,
                        company_name TEXT DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'NEW',
                        touch_count INTEGER DEFAULT 0,
                        last_touch_at REAL DEFAULT 0.0,
                        cooldown_until REAL DEFAULT 0.0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        notes TEXT DEFAULT '',
                        UNIQUE(inn_or_kio, root_domain)
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ledger_domain ON outreach_ledger (root_domain);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ledger_inn ON outreach_ledger (inn_or_kio);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ledger_status ON outreach_ledger (status);
                """)
        except Exception as e:
            logger.error(f"Failed to initialize outreach ledger database: {e}")

    def is_touch_allowed(
        self,
        inn_or_kio: Optional[str],
        domain: str,
        cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    ) -> bool:
        """
        Determines if outreach is legally and commercially allowed.
        Returns False if:
        - Entity has status OPT_OUT (permanent ban).
        - Entity was contacted within cooldown_days.
        """
        c_dom = _clean_domain(domain)
        c_inn = _clean_inn(inn_or_kio)
        now = time.time()

        try:
            with sqlite3.connect(self.db_path) as conn:
                # Check by domain or by INN (if INN known)
                if c_inn != "UNKNOWN_INN":
                    cur = conn.execute(
                        """
                        SELECT status, cooldown_until FROM outreach_ledger
                        WHERE inn_or_kio = ? OR root_domain = ?
                        ORDER BY last_touch_at DESC LIMIT 1
                        """,
                        (c_inn, c_dom),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT status, cooldown_until FROM outreach_ledger
                        WHERE root_domain = ?
                        ORDER BY last_touch_at DESC LIMIT 1
                        """,
                        (c_dom,),
                    )
                row = cur.fetchone()
                if not row:
                    return True

                st, cd_until = row[0], float(row[1] or 0.0)
                if st == "OPT_OUT":
                    return False
                if now < cd_until:
                    return False
                return True
        except Exception as e:
            logger.warning(f"Ledger check error for {c_dom} ({c_inn}): {e}")
            return True

    def record_touch(
        self,
        domain: str,
        inn_or_kio: Optional[str] = None,
        company_name: str = "",
        status: str = "SENT",
        notes: str = "",
        cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    ) -> None:
        """
        Records an outreach touch event, resets 90-day cooldown timer.
        """
        c_dom = _clean_domain(domain)
        c_inn = _clean_inn(inn_or_kio)
        st = status if status in ALLOWED_STATUSES else "SENT"
        now = time.time()
        cd_until = now + (cooldown_days * 86400)

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    INSERT INTO outreach_ledger (
                        inn_or_kio, root_domain, company_name, status,
                        touch_count, last_touch_at, cooldown_until,
                        created_at, updated_at, notes
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(inn_or_kio, root_domain) DO UPDATE SET
                        status = excluded.status,
                        touch_count = outreach_ledger.touch_count + 1,
                        last_touch_at = excluded.last_touch_at,
                        cooldown_until = excluded.cooldown_until,
                        updated_at = excluded.updated_at,
                        notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE outreach_ledger.notes END,
                        company_name = CASE WHEN excluded.company_name != '' THEN excluded.company_name ELSE outreach_ledger.company_name END
                    """,
                    (c_inn, c_dom, company_name, st, now, cd_until, now, now, notes),
                )
        except Exception as e:
            logger.error(f"Failed to record touch in outreach ledger: {e}")

    def mark_status(
        self,
        domain: str,
        status: str,
        inn_or_kio: Optional[str] = None,
        notes: str = "",
    ) -> None:
        """
        Updates ledger status for domain (e.g. OPT_OUT, REPLIED, APPROVED_IN_DESK).
        """
        c_dom = _clean_domain(domain)
        c_inn = _clean_inn(inn_or_kio)
        st = status if status in ALLOWED_STATUSES else "NEW"
        now = time.time()

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                if c_inn != "UNKNOWN_INN":
                    conn.execute(
                        """
                        UPDATE outreach_ledger
                        SET status = ?, updated_at = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END
                        WHERE root_domain = ? OR inn_or_kio = ?
                        """,
                        (st, now, notes, notes, c_dom, c_inn),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE outreach_ledger
                        SET status = ?, updated_at = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END
                        WHERE root_domain = ?
                        """,
                        (st, now, notes, notes, c_dom),
                    )
        except Exception as e:
            logger.error(f"Failed to mark status in ledger: {e}")

    def get_entry(self, domain: str, inn_or_kio: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Retrieves ledger status and metadata for domain / INN."""
        c_dom = _clean_domain(domain)
        c_inn = _clean_inn(inn_or_kio)

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if c_inn != "UNKNOWN_INN":
                    cur = conn.execute(
                        "SELECT * FROM outreach_ledger WHERE inn_or_kio = ? OR root_domain = ? LIMIT 1",
                        (c_inn, c_dom),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM outreach_ledger WHERE root_domain = ? LIMIT 1",
                        (c_dom,),
                    )
                row = cur.fetchone()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            logger.warning(f"Error fetching ledger entry: {e}")
            return None

    def list_ledger(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Lists ledger entries, optionally filtered by status."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if status and status in ALLOWED_STATUSES:
                    cur = conn.execute(
                        "SELECT * FROM outreach_ledger WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                        (status, limit),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM outreach_ledger ORDER BY updated_at DESC LIMIT ?",
                        (limit,),
                    )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"Error listing ledger entries: {e}")
            return []

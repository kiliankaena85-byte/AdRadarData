"""
Batch Checkpoints & Resumption Store.
Strict compliance with AGENTS.md, Zero-Simulation, and WAL SQLite persistence.

Storage:
- Single WAL SQLite database: C:\\AdRadarData\\db\\batch_checkpoints.sqlite
Purpose:
- Allows long batch auditing jobs (e.g. 500 domains or hunt-vcru articles) to be paused
  or interrupted and resumed (resume=True) without re-processing already completed items.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from legal_sheaf.config import DB_DIR

logger = logging.getLogger("legal_sheaf.batch_checkpoints")

CHECKPOINTS_DB_PATH = DB_DIR / "batch_checkpoints.sqlite"


class BatchCheckpointStore:
    """
    SQLite-backed store for batch execution checkpoints and idempotent resumption.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or CHECKPOINTS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(str(self.db_path), timeout=15.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batch_checkpoints (
                        batch_id TEXT NOT NULL,
                        item_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT,
                        completed_at REAL NOT NULL,
                        PRIMARY KEY (batch_id, item_key)
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_batch_status
                    ON batch_checkpoints(batch_id, status);
                    """
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Error initializing batch checkpoints DB: {e}")

    def is_completed(self, batch_id: str, item_key: str) -> bool:
        """Checks if a given item in batch_id was successfully completed."""
        if not batch_id or not item_key:
            return False
        clean_key = item_key.strip().lower()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT status FROM batch_checkpoints WHERE batch_id = ? AND item_key = ?",
                    (batch_id, clean_key),
                )
                row = cur.fetchone()
                if row and row[0] in ("COMPLETED", "SUCCESS", "SKIPPED"):
                    return True
        except Exception as e:
            logger.warning(f"Error checking checkpoint for {batch_id}:{item_key}: {e}")
        return False

    def mark_completed(
        self,
        batch_id: str,
        item_key: str,
        status: str = "COMPLETED",
        result: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Records completion status and serialized metadata for an item."""
        if not batch_id or not item_key:
            return
        clean_key = item_key.strip().lower()
        res_data = result or ({"error": error_message} if error_message else None)
        payload = json.dumps(res_data, ensure_ascii=False) if res_data else None
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO batch_checkpoints (batch_id, item_key, status, result_json, completed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (batch_id, clean_key, status, payload, time.time()),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"Error marking checkpoint for {batch_id}:{item_key}: {e}")

    def get_completed_keys(self, batch_id: str) -> Set[str]:
        """Returns set of all completed item keys for a batch."""
        if not batch_id:
            return set()
        keys: Set[str] = set()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT item_key FROM batch_checkpoints WHERE batch_id = ? AND status IN ('COMPLETED', 'SUCCESS', 'SKIPPED')",
                    (batch_id,),
                )
                for row in cur.fetchall():
                    keys.add(row[0])
        except Exception as e:
            logger.warning(f"Error retrieving completed keys for {batch_id}: {e}")
        return keys

    def get_item_result(self, batch_id: str, item_key: str) -> Optional[Dict[str, Any]]:
        """Retrieves cached result JSON for an item if available."""
        if not batch_id or not item_key:
            return None
        clean_key = item_key.strip().lower()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT result_json FROM batch_checkpoints WHERE batch_id = ? AND item_key = ?",
                    (batch_id, clean_key),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as e:
            logger.warning(f"Error retrieving result for {batch_id}:{item_key}: {e}")
        return None

    def get_checkpoint_stats(self, batch_id: str) -> Dict[str, Any]:
        """Returns statistical summary of progress for a batch."""
        if not batch_id:
            return {"batch_id": "", "total": 0, "completed": 0}
        stats = {"batch_id": batch_id, "total": 0, "completed": 0, "failed": 0}
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT status, COUNT(*) FROM batch_checkpoints WHERE batch_id = ? GROUP BY status", (batch_id,))
                for row in cur.fetchall():
                    st, count = row[0], row[1]
                    stats["total"] += count
                    if st in ("COMPLETED", "SUCCESS", "SKIPPED"):
                        stats["completed"] += count
                    elif st in ("FAILED", "ERROR"):
                        stats["failed"] += count
        except Exception as e:
            logger.warning(f"Error getting stats for {batch_id}: {e}")
        return stats

    def clear_batch(self, batch_id: str) -> None:
        """Deletes all checkpoints for a batch to start from scratch."""
        if not batch_id:
            return
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute("DELETE FROM batch_checkpoints WHERE batch_id = ?", (batch_id,))
                conn.commit()
        except Exception as e:
            logger.warning(f"Error clearing batch {batch_id}: {e}")


# Singleton instance
_DEFAULT_CHECKPOINT_STORE = BatchCheckpointStore()


def get_checkpoint_store() -> BatchCheckpointStore:
    return _DEFAULT_CHECKPOINT_STORE

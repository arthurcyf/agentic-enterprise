"""Relay: publish unpublished outbox rows to the executor queue.

At-least-once: if the relay crashes after publishing but before marking the
row, the next run publishes it again. The queue ignores duplicate action ids,
and the executor's ledger makes execution idempotent anyway.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from audit.common import config
from audit.common.auditlog_client import get_audit_log
from audit.outbox import db

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  action_id TEXT PRIMARY KEY,
  engagement_id TEXT NOT NULL,
  proposal_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  action_type TEXT NOT NULL,
  target TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  enqueued_at TEXT NOT NULL
);
"""


def connect_queue(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.QUEUE_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(QUEUE_SCHEMA)
    return conn


def publish(state: sqlite3.Connection, queue: sqlite3.Connection, _crash_before_mark: bool = False) -> int:
    rows = state.execute("SELECT * FROM outbox WHERE published = 0 ORDER BY created_at").fetchall()
    published = 0
    for r in rows:
        cur = queue.execute(
            "INSERT OR IGNORE INTO queue VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
            (r["action_id"], r["engagement_id"], r["proposal_hash"], r["idempotency_key"], r["action_type"],
             r["target"], r["payload_json"], r["payload_hash"], db.now()),
        )
        if _crash_before_mark:
            raise RuntimeError("simulated relay crash before marking published")
        state.execute("UPDATE outbox SET published = 1 WHERE action_id = ?", (r["action_id"],))
        if cur.rowcount:
            published += 1
            get_audit_log().append("relay", "published", {"action_id": r["action_id"], "target": r["target"],
                                                          "engagement_id": r["engagement_id"]})
    return published

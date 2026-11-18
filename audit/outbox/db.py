"""State database and the transactional outbox.

An approval and the action it authorises are written in ONE transaction:
either both rows exist or neither does. The relay later publishes unpublished
outbox rows to the executor queue (at-least-once; the executor deduplicates).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from audit.common import config
from audit.common.canon import canon, idempotency_key, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  proposal_hash TEXT PRIMARY KEY,
  engagement_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  action_type TEXT NOT NULL,
  target TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_hash TEXT NOT NULL UNIQUE,
  engagement_id TEXT NOT NULL,
  approver TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  action_id TEXT PRIMARY KEY,
  engagement_id TEXT NOT NULL,
  proposal_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  action_type TEXT NOT NULL,
  target TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  published INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engagement_id TEXT NOT NULL,
  proposal_hash TEXT,
  source TEXT NOT NULL,            -- 'gate' or 'executor'
  reasons_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  decided_by TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.STATE_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def record_proposal(conn: sqlite3.Connection, proposal: dict, attempt: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO proposals VALUES (?,?,?,?,?,?,?)",
        (proposal["proposal_hash"], proposal["engagement_id"], attempt, proposal["action_type"],
         proposal["target"], canon(proposal), now()),
    )


def record_gate_result(conn: sqlite3.Connection, engagement_id: str, attempt: int, outcome: str, checks: list) -> None:
    conn.execute(
        "INSERT INTO gate_results (engagement_id, attempt, outcome, checks_json, created_at) VALUES (?,?,?,?,?)",
        (engagement_id, attempt, outcome, json.dumps(checks), now()),
    )


def action_payload(proposal: dict) -> dict:
    """What the executor receives: the approved proposal content."""
    return {k: proposal[k] for k in ("engagement_id", "action_type", "target", "payload", "evidence_refs")}


def approve_and_enqueue(conn: sqlite3.Connection, proposal: dict, approver: str,
                        _fail_between: bool = False) -> str | None:
    """Approval row + outbox row in one transaction. Returns the action id (None if already approved)."""
    payload = action_payload(proposal)
    with transaction(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO approvals (proposal_hash, engagement_id, approver, created_at) VALUES (?,?,?,?)",
            (proposal["proposal_hash"], proposal["engagement_id"], approver, now()),
        )
        if cur.rowcount == 0:
            return None
        if _fail_between:  # test hook: simulate a crash between the two inserts
            raise RuntimeError("simulated crash between approval and outbox insert")
        action_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,0,?)",
            (action_id, proposal["engagement_id"], proposal["proposal_hash"],
             idempotency_key(proposal["engagement_id"], proposal["action_type"], proposal["target"]),
             proposal["action_type"], proposal["target"], canon(payload), sha256_hex(canon(payload)), now()),
        )
    return action_id


def escalate(conn: sqlite3.Connection, engagement_id: str, proposal_hash: str | None, source: str, reasons: list) -> int:
    cur = conn.execute(
        "INSERT INTO review_queue (engagement_id, proposal_hash, source, reasons_json, created_at) VALUES (?,?,?,?,?)",
        (engagement_id, proposal_hash, source, json.dumps(reasons), now()),
    )
    return cur.lastrowid

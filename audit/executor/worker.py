"""Executor worker (deterministic, no LLM).

For each queued action:
  1. Check the payload hash against the approved outbox row (the action must be exactly what was approved).
  2. Check the idempotency ledger (unique key per engagement, action type and target): done -> skip.
  3. Run the handler; retry temporary failures with exponential backoff; escalate after 3 failures.
"""

# How this file fits in
# ---------------------
# Used by:  cli.cmd_execute, demo, tests.
# Reads:    var/queue.db (from outbox.relay), var/state.db approvals/outbox (the approved hash).
# Writes:   var/executor.db ledger (UNIQUE idempotency key), the mock targets via executor.handlers,
#           review_queue rows (via outbox.db.escalate) after 3 failures or a hash mismatch, audit log.
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Callable

from audit.common import config
from audit.common.auditlog_client import get_audit_log
from audit.common.canon import sha256_hex
from audit.executor.handlers import HANDLERS, TransientError
from audit.outbox import db

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
  idempotency_key TEXT NOT NULL UNIQUE,
  action_id TEXT NOT NULL,
  status TEXT NOT NULL,          -- 'in_progress' | 'done' | 'failed'
  attempts INTEGER NOT NULL DEFAULT 0,
  result_ref TEXT,
  updated_at TEXT NOT NULL
);
"""
MAX_ATTEMPTS = 3


def connect_ledger(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.LEDGER_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(LEDGER_SCHEMA)
    return conn


class Executor:
    def __init__(self, state, queue, ledger, handler_kwargs: dict | None = None,
                 sleep: Callable[[float], None] = time.sleep, base_delay: float = 0.5):
        self.state, self.queue, self.ledger = state, queue, ledger
        self.handler_kwargs = handler_kwargs or {}
        self.sleep, self.base_delay = sleep, base_delay
        self.log = get_audit_log()
        self._handlers: dict = {}

    def handler(self, action_type: str):
        if action_type not in self._handlers:
            self._handlers[action_type] = HANDLERS[action_type](**self.handler_kwargs)
        return self._handlers[action_type]

    def run(self) -> dict:
        stats = {"executed": 0, "skipped_duplicate": 0, "rejected": 0, "escalated": 0}
        for item in self.queue.execute("SELECT * FROM queue WHERE status = 'pending' ORDER BY enqueued_at").fetchall():
            outcome = self.process(item)
            stats[outcome] += 1
        return stats

    def process(self, item: sqlite3.Row) -> str:
        aid, key = item["action_id"], item["idempotency_key"]
        approved = self.state.execute(
            "SELECT o.payload_hash FROM outbox o JOIN approvals a ON a.proposal_hash = o.proposal_hash "
            "WHERE o.action_id = ?", (aid,)).fetchone()
        actual = sha256_hex(item["payload_json"])
        if approved is None or approved["payload_hash"] != actual or item["payload_hash"] != actual:
            self._finish_queue(aid, "rejected")
            db.escalate(self.state, item["engagement_id"], item["proposal_hash"], "executor",
                        ["payload does not match the approved hash; refusing to execute"])
            self.log.append("executor", "rejected_hash_mismatch", {"action_id": aid, "target": item["target"]})
            return "rejected"

        row = self.ledger.execute("SELECT * FROM ledger WHERE idempotency_key = ?", (key,)).fetchone()
        if row and row["status"] == "done":
            self._finish_queue(aid, "done")
            self.log.append("executor", "skipped_duplicate", {"action_id": aid, "target": item["target"],
                                                              "idempotency_key": key, "first_action": row["action_id"]})
            return "skipped_duplicate"
        if row is None:
            # The UNIQUE constraint is what makes this safe if two workers race.
            try:
                self.ledger.execute("INSERT INTO ledger (idempotency_key, action_id, status, attempts, updated_at) "
                                    "VALUES (?,?, 'in_progress', 0, ?)", (key, aid, db.now()))
            except sqlite3.IntegrityError:
                return "skipped_duplicate"

        action = json.loads(item["payload_json"])
        attempts = row["attempts"] if row else 0
        while True:
            attempts += 1
            try:
                ref = self.handler(item["action_type"]).execute(action, key)
            except TransientError as e:
                self.ledger.execute("UPDATE ledger SET attempts = ?, updated_at = ? WHERE idempotency_key = ?",
                                    (attempts, db.now(), key))
                self.log.append("executor", "transient_failure", {"action_id": aid, "attempt": attempts, "error": str(e)})
                if attempts >= MAX_ATTEMPTS:
                    self.ledger.execute("UPDATE ledger SET status = 'failed' WHERE idempotency_key = ?", (key,))
                    self._finish_queue(aid, "failed")
                    db.escalate(self.state, item["engagement_id"], item["proposal_hash"], "executor",
                                [f"{item['action_type']} failed {attempts} times: {e}"])
                    self.log.append("executor", "escalated", {"action_id": aid, "attempts": attempts})
                    return "escalated"
                self.sleep(self.base_delay * 2 ** (attempts - 1))
                continue
            self.ledger.execute("UPDATE ledger SET status = 'done', attempts = ?, result_ref = ?, updated_at = ? "
                                "WHERE idempotency_key = ?", (attempts, ref, db.now(), key))
            self._finish_queue(aid, "done")
            self.log.append("executor", "executed", {"action_id": aid, "action_type": item["action_type"],
                                                     "target": item["target"], "result": ref, "attempts": attempts})
            return "executed"

    def _finish_queue(self, action_id: str, status: str) -> None:
        self.queue.execute("UPDATE queue SET status = ? WHERE action_id = ?", (status, action_id))

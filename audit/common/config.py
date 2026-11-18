"""Paths and settings. Everything stateful lives under var/ (gitignored)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("AUDIT_ROOT", Path(__file__).resolve().parents[2]))
VAR = Path(os.environ.get("AUDIT_VAR", ROOT / "var"))

SOURCE_DIR = VAR / "source"            # authoritative ERP data (never mounted into the sandbox)
ANSWER_KEY = VAR / "answer_key.json"   # planted exceptions (never mounted into the sandbox)
ENGAGEMENTS = VAR / "engagements"
STATE_DB = VAR / "state.db"            # proposals, gate results, approvals, outbox, review queue
QUEUE_DB = VAR / "queue.db"            # executor queue (the relay publishes here)
LEDGER_DB = VAR / "executor.db"        # executor idempotency ledger
MOCK_TARGETS = VAR / "mock_targets"    # mock audit repo, issue tracker, Slack log
CREDS = VAR / "creds"                  # executor credentials, one per handler
AUDIT_LOG = VAR / "audit" / "audit.jsonl"
EVALS = VAR / "evals"

ENTITY = "ENT01"
QUARTER = "2026Q3"
THRESHOLD = 10_000.00
CONTROL_ID = "AP-01"

AUDITLOG_URL = os.environ.get("AUDITLOG_URL", "")  # empty: write the local file directly
POLICY_URL = os.environ.get("POLICY_URL", "http://localhost:8100")

MODELS = {
    "lead": os.environ.get("AUDIT_MODEL_LEAD", "claude-sonnet-5-5"),
    "tester": os.environ.get("AUDIT_MODEL_TESTER", "claude-sonnet-5-5"),
    "planner": os.environ.get("AUDIT_MODEL_HELPER", "claude-haiku-5-5"),
    "reporter": os.environ.get("AUDIT_MODEL_HELPER", "claude-haiku-5-5"),
    "reviewer": os.environ.get("AUDIT_MODEL_HELPER", "claude-haiku-5-5"),
}


def ensure_dirs() -> None:
    for d in (VAR, SOURCE_DIR, ENGAGEMENTS, MOCK_TARGETS, CREDS, AUDIT_LOG.parent, EVALS):
        d.mkdir(parents=True, exist_ok=True)

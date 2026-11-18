"""In-process stand-in for the sandbox container, for tests (no Docker, no LLM).

It runs the scripted agent against the real broker logic (token check, scope,
allowlist, row hashes) reading the local source CSVs, and writes proposals to
the same attempt folders the container would use.
"""

# How this file fits in
# ---------------------
# Used by:  tests (tests/unit/test_gate.py, test_engagement_review_evals.py) and evals.run in scripted mode.
# Replaces: orchestrator.engagement.run_container, with the same signature and attempt folders.
# Calls:    broker.core.Broker (real scope checks and hashing, local CSVs), runner.scripted,
#           runner.outbox_tool.OutboxWriter.
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from audit.broker.core import Broker
from audit.common import config
from audit.common.token import CaseScope, mint
from audit.data.generate import load_table
from audit.runner.outbox_tool import OutboxWriter
from audit.runner.scripted import run_scripted


def local_erp_fetch(table: str, entity: str, quarter: str) -> list[dict]:
    rows = load_table(table, config.SOURCE_DIR)
    if table == "payments":
        rows = [r for r in rows if r["entity"] == entity and r["quarter"] == quarter]
    return rows


def local_scripted_runner(engagement_id: str, attempt_dir: Path, mode: str, script: str,
                          timeout_s: int = 0, max_budget_usd: float = 0, max_turns: int = 0) -> tuple[dict, bool]:
    if mode != "scripted":
        raise ValueError("the local runner only supports scripted mode")
    secret = secrets.token_hex(16)
    broker = Broker(secret, erp_fetch=local_erp_fetch)
    token = mint(CaseScope(engagement_id, config.ENTITY, config.QUARTER), secret)

    async def fetch(table: str) -> list[dict]:
        rows, offset = [], 0
        while offset is not None:
            page = broker.extract(token, table, offset=offset, limit=200)
            rows += page["rows"]
            offset = page["next_offset"]
        return rows

    writer = OutboxWriter(attempt_dir / "outbox", engagement_id, scratch_dir=attempt_dir / "scratch")
    summary = asyncio.run(run_scripted(script, fetch, writer, attempt_dir / "out"))
    return json.loads(json.dumps(summary)), False

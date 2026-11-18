"""Audit log service: the single writer of the hash-chained log.

Components outside the sandbox (policy, broker, gate, executor, review CLI)
POST entries here. The sandbox cannot reach this service; the runner's stream
is ingested by the orchestrator after the run.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from audit.auditlog.chain import HashChainLog

LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/var/audit/audit.jsonl"))
SECRETS = [os.environ.get(k, "") for k in ("ERP_API_KEY", "CASE_TOKEN_SECRET", "ANTHROPIC_API_KEY")]

app = FastAPI(title="auditlog")
log = HashChainLog(LOG_PATH, extra_secrets=SECRETS)


class Entry(BaseModel):
    actor: str
    type: str
    data: dict


@app.post("/append")
def append(entry: Entry) -> dict:
    e = log.append(entry.actor, entry.type, entry.data)
    return {"seq": e["seq"], "hash": e["hash"]}


@app.get("/health")
def health() -> dict:
    return {"ok": True}

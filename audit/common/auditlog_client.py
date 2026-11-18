"""How components write to the audit log.

If AUDITLOG_URL is set, entries go to the audit log service (the normal case
when the compose stack is running). Otherwise this process writes the local
file itself, which is fine for tests and single-process tools.
"""

# How this file fits in
# ---------------------
# Used by:  policy.service, broker.server, gate.gate, outbox.relay, executor.worker, review.cli,
#           orchestrator.engagement: anything outside the sandbox that must leave an audit trail.
# Calls:    HttpAuditLog -> POST auditlog.service /append (normal case, compose stack running)
#           LocalAuditLog -> auditlog.chain.HashChainLog on var/audit/audit.jsonl (tests, no services)
# The sandbox never uses this: it cannot reach the auditlog service. Its transcript is copied in by
# orchestrator.engagement.ingest_stream after the run.
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import httpx

from audit.common import config


class AuditLogClient:
    def append(self, actor: str, type_: str, data: dict[str, Any]) -> None:
        raise NotImplementedError


class HttpAuditLog(AuditLogClient):
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def append(self, actor: str, type_: str, data: dict[str, Any]) -> None:
        r = httpx.post(f"{self.url}/append", json={"actor": actor, "type": type_, "data": data}, timeout=10)
        r.raise_for_status()


class LocalAuditLog(AuditLogClient):
    def __init__(self, path=None):
        from audit.auditlog.chain import HashChainLog

        secrets = [os.environ.get(k, "") for k in ("ERP_API_KEY", "CASE_TOKEN_SECRET", "ANTHROPIC_API_KEY")]
        self.log = HashChainLog(path or config.AUDIT_LOG, extra_secrets=secrets)

    def append(self, actor: str, type_: str, data: dict[str, Any]) -> None:
        self.log.append(actor, type_, data)


@lru_cache(maxsize=1)
def get_audit_log() -> AuditLogClient:
    url = os.environ.get("AUDITLOG_URL", config.AUDITLOG_URL)
    return HttpAuditLog(url) if url else LocalAuditLog()

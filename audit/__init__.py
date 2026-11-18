"""Agentic internal-audit prototype.

Agents work inside a sandbox; deterministic code (policy service, verification
gate, executor) decides what leaves it. See README.md for the architecture.
"""

# How this file fits in
# ---------------------
# Package map (where each part runs):
#   outside the sandbox (host):   cli, demo, orchestrator, gate, outbox, executor, review, evals
#   outside the sandbox (compose): auditlog.service, erp.app, broker.server, policy.service, egress.proxy
#   inside the sandbox container: runner (plus the parts of common/ it imports)
#   shared by both sides:         common (canonical hashing, tokens, schemas, config, audit-log client)

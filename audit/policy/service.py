"""Policy service (outside the sandbox).

The runner's PreToolUse hook and can_use_tool callback are thin clients of
POST /decide. The policy file lives here, not in the sandbox, so the agent
cannot edit it.
"""

# How this file fits in
# ---------------------
# Runs as:  compose service `policy` (on sandbox_net so the runner can reach it; port 127.0.0.1:8100).
# Called by: runner.hooks.PolicyClient for every tool call (stage "hook" and stage "can_use_tool"), and
#           by runner.probes in the demo.
# Calls:    policy.engine.PolicyEngine.decide, then the audit-log client (every decision is logged).
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from audit.common.auditlog_client import get_audit_log
from audit.policy.engine import PolicyEngine

app = FastAPI(title="policy")
engine = PolicyEngine()


class DecideRequest(BaseModel):
    engagement_id: str
    stage: str                   # "hook" | "can_use_tool" | "probe"
    tool_name: str
    tool_input: dict = {}
    agent_id: str | None = None
    agent_type: str | None = None
    tool_use_id: str | None = None


@app.post("/decide")
def decide(req: DecideRequest) -> dict:
    d = engine.decide(req.tool_name, req.tool_input, req.agent_type)
    get_audit_log().append("policy", "policy_decision", {
        "engagement_id": req.engagement_id, "stage": req.stage, "agent_type": req.agent_type or "lead",
        "agent_id": req.agent_id, "tool_use_id": req.tool_use_id, "tool_name": req.tool_name,
        "tool_input": _summarise(req.tool_input), "decision": d.decision, "rule_id": d.rule_id, "reason": d.reason,
    })
    return {"decision": d.decision, "rule_id": d.rule_id, "reason": d.reason}


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _summarise(tool_input: dict) -> dict:
    """Keep the log readable: truncate long fields such as file contents."""
    out = {}
    for k, v in (tool_input or {}).items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s[:500] + "...[truncated]" if isinstance(s, str) and len(s) > 500 else s
    return out

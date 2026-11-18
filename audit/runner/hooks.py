"""Thin policy clients inside the runner.

Both the PreToolUse hook and the can_use_tool callback forward the call to the
policy service and enforce its answer. They contain no policy themselves.

  * PreToolUse runs for EVERY tool call (including ones Claude Code would
    auto-approve, such as reads and read-only Bash), so it is the primary gate.
    It is registered with on_failure="block": if the policy service is down or
    this code raises, the call is blocked (fail closed).
  * can_use_tool runs only for calls that still need a permission decision.
    It asks again (stage="can_use_tool") as a second layer.
"""

# How this file fits in
# ---------------------
# Used by:  runner.main.build_options (PreToolUse hook, SubagentStart hook, can_use_tool callback)
#           and runner.main scripted mode (runner.probes sends forbidden calls through the same hook).
# Calls:    policy.service POST /decide. Holds no policy itself.
from __future__ import annotations

import os
from typing import Any

import httpx

from audit.common import config


class PolicyClient:
    def __init__(self, url: str | None = None, engagement_id: str | None = None, timeout: float = 8.0):
        self.url = (url or os.environ.get("POLICY_URL") or config.POLICY_URL).rstrip("/")
        self.engagement_id = engagement_id or os.environ.get("ENGAGEMENT_ID", "unknown")
        self.timeout = timeout

    async def decide(self, stage: str, tool_name: str, tool_input: dict, agent_id: str | None,
                     agent_type: str | None, tool_use_id: str | None) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            r = await http.post(f"{self.url}/decide", json={
                "engagement_id": self.engagement_id, "stage": stage, "tool_name": tool_name,
                "tool_input": tool_input, "agent_id": agent_id, "agent_type": agent_type or None,
                "tool_use_id": tool_use_id,
            })
            r.raise_for_status()
            return r.json()


class AgentRegistry:
    """agent_id -> agent_type. can_use_tool only receives agent_id, so hooks record the mapping."""

    def __init__(self):
        self._types: dict[str, str] = {}

    def record(self, agent_id: str | None, agent_type: str | None) -> None:
        if agent_id and agent_type:
            self._types[agent_id] = agent_type

    def type_of(self, agent_id: str | None) -> str | None:
        if not agent_id:
            return None          # main agent ("lead")
        return self._types.get(agent_id, "unknown")  # unknown subagent: policy denies scoped tools


def make_subagent_start_hook(registry: AgentRegistry):
    async def on_start(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        registry.record(input_data.get("agent_id"), input_data.get("agent_type"))
        return {}

    return on_start


def make_pretooluse_hook(policy: PolicyClient, registry: AgentRegistry | None = None):
    async def policy_hook(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        # Exceptions propagate on purpose: with on_failure="block" a failure blocks the call.
        agent_id, agent_type = input_data.get("agent_id"), input_data.get("agent_type")
        if registry:
            registry.record(agent_id, agent_type)
        d = await policy.decide("hook", input_data["tool_name"], input_data.get("tool_input") or {},
                                agent_id, agent_type, tool_use_id)
        if d["decision"] == "allow":
            return {}  # no opinion: the normal permission flow (and can_use_tool) continues
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Policy denied ({d['rule_id']}): {d['reason']}",
        }}

    return policy_hook


def make_can_use_tool(policy: PolicyClient, registry: AgentRegistry | None = None):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], ctx: Any):
        agent_id = getattr(ctx, "agent_id", None)
        agent_type = registry.type_of(agent_id) if registry else None
        try:
            d = await policy.decide("can_use_tool", tool_name, tool_input, agent_id,
                                    agent_type, getattr(ctx, "tool_use_id", None))
        except Exception as e:  # fail closed
            return PermissionResultDeny(message=f"Policy service unavailable, denied: {e}")
        if d["decision"] == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=f"Policy denied ({d['rule_id']}): {d['reason']}")

    return can_use_tool

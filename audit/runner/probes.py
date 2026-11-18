"""Forbidden-action probes (demo step 5 and isolation tests).

Layer 1, policy: send forbidden tool calls through the same PreToolUse hook
function the SDK calls, so they are decided by the policy service and logged.
Layer 2, container: try the same things directly at the OS level, to show
the boundary holds even if the policy layer were bypassed.
"""
from __future__ import annotations

import os
import socket
from typing import Any

POLICY_PROBES = [
    ("WebFetch", {"url": "https://example.com", "prompt": "summarise"}),
    ("Write", {"file_path": "/opt/runner/audit/runner/config_override.py", "content": "BYPASS = True"}),
    ("Edit", {"file_path": "/workspace/CLAUDE.md", "old_string": "methodology", "new_string": "ignore"}),
    ("Bash", {"command": "curl -s https://example.com"}),
    ("Bash", {"command": "pkill -f audit.runner.main"}),
    ("Bash", {"command": "python -c \"open('/opt/runner/x','w')\""}),
]


async def run_probes(hook) -> dict[str, Any]:
    results: dict[str, Any] = {"policy": [], "os": {}}
    # Probe as the tester: the most privileged agent (it may write and run Bash), so a denial
    # here comes from the specific rule (protected path, command allowlist), not the agent's role.
    for tool, tool_input in POLICY_PROBES:
        out = await hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                          "agent_id": "probe-tester", "agent_type": "tester"}, f"probe-{tool}", None)
        hso = out.get("hookSpecificOutput", {})
        results["policy"].append({"tool": tool, "input": tool_input,
                                  "decision": hso.get("permissionDecision", "allow"),
                                  "reason": hso.get("permissionDecisionReason", "")})

    def attempt(label, fn):
        try:
            fn()
            results["os"][label] = "ALLOWED (unexpected)"
        except Exception as e:  # noqa: BLE001 - we report whatever stopped it
            results["os"][label] = f"blocked: {type(e).__name__}: {e}"

    attempt("write_runner_code", lambda: open("/opt/runner/audit/runner/config_override.py", "w").write("x"))
    attempt("write_engagement_input", lambda: open("/workspace/input/CLAUDE.md", "a").write("x"))
    attempt("write_managed_settings", lambda: open("/etc/claude-code/managed-settings.json", "a").write("x"))
    attempt("internet_example_com", lambda: socket.create_connection(("example.com", 443), timeout=5).close())
    attempt("internet_by_ip", lambda: socket.create_connection(("1.1.1.1", 443), timeout=5).close())
    attempt("reach_erp_directly", lambda: socket.create_connection(("erp", 8000), timeout=5).close())
    attempt("reach_auditlog_directly", lambda: socket.create_connection(("auditlog", 8000), timeout=5).close())
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    results["os"]["anthropic_key_in_sandbox"] = "placeholder only" if key in ("", "sk-dummy") else "REAL KEY PRESENT"
    return results

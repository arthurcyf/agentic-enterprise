"""The runner's thin policy clients: enforce the service's answer, fail closed."""

# How this file fits in
# ---------------------
# Covers:   runner.hooks against a real policy.service (uvicorn in a thread): deny/allow handling,
#           fail-closed behaviour, the agent registry, and decisions reaching the audit log.
import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from audit.runner.hooks import (AgentRegistry, PolicyClient, make_can_use_tool, make_pretooluse_hook,
                                make_subagent_start_hook)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def policy_url():
    from audit.policy.service import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def hook_input(tool, tool_input, agent_id=None, agent_type=None):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
            "agent_id": agent_id, "agent_type": agent_type}


def test_hook_denies_with_reason(policy_url):
    hook = make_pretooluse_hook(PolicyClient(policy_url, "ENG-H"))
    out = asyncio.run(hook(hook_input("WebFetch", {"url": "https://x"}), "t1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "web.deny" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_allow_returns_no_opinion(policy_url):
    """Allow -> {} so the normal flow (deny rules, can_use_tool) still runs after the hook."""
    hook = make_pretooluse_hook(PolicyClient(policy_url, "ENG-H"))
    assert asyncio.run(hook(hook_input("Read", {"file_path": "/workspace/CLAUDE.md"}), "t2", None)) == {}


def test_hook_raises_when_policy_down_so_on_failure_block_blocks():
    hook = make_pretooluse_hook(PolicyClient("http://127.0.0.1:1", "ENG-H", timeout=1))
    with pytest.raises(Exception):
        asyncio.run(hook(hook_input("Read", {"file_path": "/workspace/x"}), "t3", None))


def test_can_use_tool_fails_closed_when_policy_down():
    from claude_agent_sdk import PermissionResultDeny
    cut = make_can_use_tool(PolicyClient("http://127.0.0.1:1", "ENG-H", timeout=1))
    res = asyncio.run(cut("Write", {"file_path": "/workspace/scratch/x"}, None))
    assert isinstance(res, PermissionResultDeny) and "unavailable" in res.message


def test_can_use_tool_uses_registry_for_subagent_type(policy_url):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
    from claude_agent_sdk.types import ToolPermissionContext

    reg = AgentRegistry()
    asyncio.run(make_subagent_start_hook(reg)({"agent_id": "a1", "agent_type": "tester"}, None, None))
    cut = make_can_use_tool(PolicyClient(policy_url, "ENG-H"), reg)
    write = {"file_path": "/workspace/scratch/t.py", "content": "x"}
    tester_ctx = ToolPermissionContext(suggestions=[], agent_id="a1")
    unknown_ctx = ToolPermissionContext(suggestions=[], agent_id="zz")
    lead_ctx = ToolPermissionContext(suggestions=[])
    assert isinstance(asyncio.run(cut("Write", write, tester_ctx)), PermissionResultAllow)
    assert isinstance(asyncio.run(cut("Write", write, unknown_ctx)), PermissionResultDeny)   # fail closed
    assert isinstance(asyncio.run(cut("Write", write, lead_ctx)), PermissionResultDeny)      # lead can't write


def test_policy_decisions_are_audit_logged(policy_url):
    import json
    from audit.common import config
    hook = make_pretooluse_hook(PolicyClient(policy_url, "ENG-LOGGED"))
    asyncio.run(hook(hook_input("Bash", {"command": "curl https://x"}, "a9", "tester"), "t9", None))
    entries = [json.loads(l) for l in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()]
    mine = [e for e in entries if e["type"] == "policy_decision" and e["data"]["engagement_id"] == "ENG-LOGGED"]
    assert mine and mine[-1]["data"]["decision"] == "deny" and mine[-1]["data"]["stage"] == "hook"

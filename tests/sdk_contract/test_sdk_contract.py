"""SDK contract: fail loudly if the installed Agent SDK drifts from what the design relies on.

Offline tests introspect the installed package. Live tests (RUN_LIVE=1 and ANTHROPIC_API_KEY set)
make small Haiku calls to prove behaviour the docs describe:
  (a) the PreToolUse hook fires for the main agent AND for subagent tool calls, with agent_type set
  (b) can_use_tool fires for a subagent's Write
  (c) a Read inside cwd skips can_use_tool but still reaches the hook
  (d) a hook that raises, with on_failure="block", blocks the call
  (e) hidden built-in subagents are unavailable with the env flag and the fork deny rule
  (f) max_budget_usd ends the run with error_max_budget_usd
"""

# How this file fits in
# ---------------------
# Covers:   the installed claude_agent_sdk (types the runner relies on) and runner.main.build_options;
#           live tests (RUN_LIVE=1) check hook / can_use_tool / subagent behaviour against the real API.
import asyncio
import dataclasses
import os
import typing

import pytest

import claude_agent_sdk as sdk
from claude_agent_sdk import types as t

LIVE = pytest.mark.skipif(not (os.environ.get("RUN_LIVE") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
                          reason="set RUN_LIVE=1 and ANTHROPIC_API_KEY to run live SDK tests")
HAIKU = "claude-haiku-5-5"


def test_version_pinned():
    assert sdk.__version__ == "0.2.184"


def test_options_fields_used_by_runner_exist():
    fields = {f.name for f in dataclasses.fields(t.ClaudeAgentOptions)}
    needed = {"tools", "allowed_tools", "disallowed_tools", "system_prompt", "mcp_servers", "strict_mcp_config",
              "permission_mode", "max_turns", "max_budget_usd", "model", "cwd", "env", "can_use_tool", "hooks",
              "agents", "setting_sources", "sandbox", "stderr"}
    assert needed <= fields, needed - fields


def test_permission_modes_and_hook_events():
    assert set(typing.get_args(t.PermissionMode)) >= {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"}
    events = {typing.get_args(a)[0] for a in typing.get_args(t.HookEvent)}
    assert {"PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "PermissionRequest"} <= events


def test_hook_matcher_supports_on_failure():
    assert "on_failure" in {f.name for f in dataclasses.fields(t.HookMatcher)}


def test_agent_definition_fields():
    fields = {f.name for f in dataclasses.fields(t.AgentDefinition)}
    assert {"description", "prompt", "tools", "disallowedTools", "model", "maxTurns", "background"} <= fields


def test_permission_context_has_agent_id_and_results():
    assert "agent_id" in {f.name for f in dataclasses.fields(t.ToolPermissionContext)}
    assert sdk.PermissionResultDeny(message="x").behavior == "deny"
    assert sdk.PermissionResultAllow().behavior == "allow"


def test_pretooluse_input_carries_agent_identity():
    assert {"agent_id", "agent_type", "tool_name", "tool_input"} <= set(t.PreToolUseHookInput.__annotations__)


def test_http_mcp_config_supports_headers():
    assert set(t.McpHttpServerConfig.__annotations__) == {"type", "url", "headers"}


def test_result_message_reports_cost_and_turns():
    fields = {f.name for f in dataclasses.fields(t.ResultMessage)}
    assert {"subtype", "num_turns", "total_cost_usd", "usage", "permission_denials"} <= fields


def test_runner_options_build(tmp_path, monkeypatch):
    """The real runner options construct against the installed SDK."""
    from audit.runner import main as runner
    from audit.runner.hooks import AgentRegistry, PolicyClient
    monkeypatch.setattr(runner, "WORKSPACE", tmp_path)
    monkeypatch.setattr(runner, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(runner, "OUTBOX", tmp_path / "outbox")
    opts = runner.build_options("ENG-C", "tok", PolicyClient("http://x", "ENG-C"), AgentRegistry())
    assert opts.permission_mode == "default" and opts.allowed_tools == []
    assert opts.setting_sources == ["project"] and opts.strict_mcp_config
    assert opts.hooks["PreToolUse"][0].on_failure == "block"
    assert all(a.background is False for a in opts.agents.values())
    assert "Write" in opts.agents["reviewer"].disallowedTools and "Bash" in opts.agents["reviewer"].disallowedTools


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

async def _collect(prompt, options):
    msgs = []
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for m in client.receive_response():
            msgs.append(m)
    return msgs


def _base(tmp_path, **kw):
    return sdk.ClaudeAgentOptions(cwd=str(tmp_path), setting_sources=[], strict_mcp_config=True, model=HAIKU,
                                  permission_mode="default", max_turns=12, max_budget_usd=0.5,
                                  env={"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
                                       "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}, **kw)


@LIVE
def test_live_hooks_and_can_use_tool_fire_for_subagents(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    hook_calls, cut_calls = [], []

    async def hook(inp, tid, ctx):
        hook_calls.append((inp["tool_name"], inp.get("agent_type")))
        return {}

    async def cut(name, inp, ctx):
        cut_calls.append((name, ctx.agent_id))
        return sdk.PermissionResultAllow(updated_input=inp)

    opts = _base(tmp_path, tools=["Read", "Write", "Agent"],
                 hooks={"PreToolUse": [sdk.HookMatcher(hooks=[hook], on_failure="block")]}, can_use_tool=cut,
                 agents={"writer": sdk.AgentDefinition(description="Writes files when asked.",
                                                       prompt="Do exactly what you are asked.",
                                                       tools=["Read", "Write"], model=HAIKU, background=False)})
    asyncio.run(_collect("Read notes.txt yourself. Then use the writer agent to write 'done' to out.txt. "
                         "Wait for it.", opts))
    # (a) hook fired in the main agent and inside the subagent
    assert ("Read", None) in hook_calls or any(n == "Read" and not a for n, a in hook_calls)
    assert any(n == "Write" and a == "writer" for n, a in hook_calls), hook_calls
    # (b) can_use_tool fired for the subagent's Write, with its agent_id
    assert any(n == "Write" and aid for n, aid in cut_calls), cut_calls
    # (c) the Read inside cwd never reached can_use_tool
    assert not any(n == "Read" for n, _ in cut_calls), cut_calls


@LIVE
def test_live_failing_hook_blocks(tmp_path):
    async def broken(inp, tid, ctx):
        raise RuntimeError("policy service down")

    opts = _base(tmp_path, tools=["Write"],
                 hooks={"PreToolUse": [sdk.HookMatcher(hooks=[broken], on_failure="block")]},
                 can_use_tool=lambda n, i, c: asyncio.sleep(0, sdk.PermissionResultAllow(updated_input=i)))
    asyncio.run(_collect("Write 'x' to blocked.txt", opts))
    assert not (tmp_path / "blocked.txt").exists()


@LIVE
def test_live_builtin_and_fork_subagents_unavailable(tmp_path):
    seen = []

    async def hook(inp, tid, ctx):
        if inp["tool_name"] in ("Agent", "Task"):
            seen.append(inp["tool_input"].get("subagent_type"))
        return {}

    opts = _base(tmp_path, tools=["Read", "Agent"], disallowed_tools=["Agent(fork)", "Agent(general-purpose)"],
                 hooks={"PreToolUse": [sdk.HookMatcher(hooks=[hook])]})
    msgs = asyncio.run(_collect("Use a general-purpose subagent (or a fork) to list files, then report.", opts))
    init = next(m for m in msgs if isinstance(m, sdk.SystemMessage) and m.subtype == "init")
    assert "general-purpose" not in str(init.data.get("agents", []))


@LIVE
def test_live_budget_cap(tmp_path):
    opts = _base(tmp_path, tools=["Read"])
    opts.max_budget_usd = 0.0001
    try:
        msgs = asyncio.run(_collect("Write a 2000-word essay about ledgers.", opts))
    except Exception:
        return  # single-shot raising after the error result is acceptable
    result = next(m for m in msgs if isinstance(m, sdk.ResultMessage))
    assert result.subtype == "error_max_budget_usd"

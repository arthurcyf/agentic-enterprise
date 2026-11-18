"""Agent runner: runs INSIDE the sandbox container.

    python -m audit.runner.main --mode live
    python -m audit.runner.main --mode scripted --script clean|corrupted|injected|web_attempt

The SDK's tools (Read, Write, Bash, ...) execute where this process runs,
which is why the runner lives in the sandbox. Everything it may do is
decided outside: the policy service decides tool calls, and the gate decides
what leaves through the outbox.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
SCRATCH = WORKSPACE / "scratch"
OUTBOX = WORKSPACE / "outbox"
OUT = WORKSPACE / "out"

# Hard denials: removed from the model's context or denied before any other step.
DISALLOWED_TOOLS = [
    "WebFetch", "WebSearch", "NotebookEdit", "Agent(fork)", "Agent(general-purpose)",
    "Bash(curl *)", "Bash(wget *)", "Bash(nc *)", "Bash(ssh *)", "Bash(scp *)", "Bash(telnet *)",
    "Bash(kill *)", "Bash(pkill *)", "Bash(killall *)",
    "Edit(//opt/runner/**)", "Edit(//etc/claude-code/**)", "Edit(//workspace/input/**)",
]

SDK_ENV = {
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",  # no hidden general-purpose subagent
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",     # subagents cannot spawn subagents
    # Subagents run in the background by default, and AgentDefinition(background=False) does NOT force the
    # foreground. Without this, the Lead's turn ends while the planner is still running, the runner exits,
    # and the subagent is killed (seen in the first live run). This makes Claude wait for each subagent.
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_TOOL_SEARCH": "false",                  # few MCP tools: load them upfront
    "DISABLE_TELEMETRY": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = {"_type": type(obj).__name__}
        for f in dataclasses.fields(obj):
            d[f.name] = to_jsonable(getattr(obj, f.name))
        return d
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def build_options(engagement_id: str, case_token: str, policy, registry):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from audit.common import config
    from audit.runner.agents import LEAD_PROMPT, build_agents
    from audit.runner.broker_client import BrokerClient
    from audit.runner.hooks import make_can_use_tool, make_pretooluse_hook, make_subagent_start_hook
    from audit.runner.outbox_tool import OutboxWriter, build_outbox_server
    from audit.runner.workspace_tool import build_workspace_server

    broker_url = os.environ.get("BROKER_URL", "http://broker:8000/mcp")
    writer = OutboxWriter(OUTBOX, engagement_id, scratch_dir=SCRATCH)
    broker = BrokerClient(broker_url, case_token)

    return ClaudeAgentOptions(
        cwd=str(WORKSPACE),
        setting_sources=["project"],          # CLAUDE.md + project .claude/settings.json only; no user settings
        strict_mcp_config=True,               # only the MCP servers below
        permission_mode="default",            # never acceptEdits/bypassPermissions (they skip can_use_tool)
        system_prompt={"type": "preset", "preset": "claude_code", "append": LEAD_PROMPT},
        model=config.MODELS["lead"],
        tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash", "Agent"],
        allowed_tools=[],                     # nothing pre-approved: every gated call reaches the policy
        disallowed_tools=DISALLOWED_TOOLS,
        mcp_servers={
            "broker": {"type": "http", "url": broker_url, "headers": {"X-Case-Token": case_token}},
            "outbox": build_outbox_server(writer),
            "workspace": build_workspace_server(broker, SCRATCH / "extracts"),
        },
        agents=build_agents(),
        hooks={
            "PreToolUse": [HookMatcher(hooks=[make_pretooluse_hook(policy, registry)], on_failure="block", timeout=15)],
            "SubagentStart": [HookMatcher(hooks=[make_subagent_start_hook(registry)])],
        },
        can_use_tool=make_can_use_tool(policy, registry),
        max_turns=int(os.environ.get("MAX_TURNS", "80")),
        max_budget_usd=float(os.environ.get("MAX_BUDGET_USD", "3.0")),
        env=SDK_ENV,
        stderr=lambda line: None,
    )


async def run_live(engagement_id: str, case_token: str) -> dict:
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage

    from audit.runner.hooks import AgentRegistry, PolicyClient

    policy, registry = PolicyClient(engagement_id=engagement_id), AgentRegistry()
    options = build_options(engagement_id, case_token, policy, registry)
    prompt = "Run the audit engagement described in /workspace/brief.md."
    feedback = WORKSPACE / "input" / "gate_feedback.md"
    if feedback.exists():
        prompt += "\n\nThis is a second attempt.\n" + feedback.read_text(encoding="utf-8")

    summary: dict[str, Any] = {"mode": "live", "subtype": "no_result"}
    started = time.time()
    with (OUT / "stream.jsonl").open("w", encoding="utf-8") as stream:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                stream.write(json.dumps(to_jsonable(message)) + "\n")
                stream.flush()
                if isinstance(message, ResultMessage):
                    summary.update({
                        "subtype": message.subtype, "is_error": message.is_error, "num_turns": message.num_turns,
                        "total_cost_usd": message.total_cost_usd, "usage": message.usage,
                        "model_usage": to_jsonable(message.model_usage), "duration_ms": message.duration_ms,
                        "permission_denials": to_jsonable(message.permission_denials),
                        "errors": message.errors, "result": message.result,
                    })
    summary["wall_seconds"] = round(time.time() - started, 1)
    return summary


async def run_scripted_mode(engagement_id: str, case_token: str, script: str) -> dict:
    from audit.runner.broker_client import BrokerClient
    from audit.runner.hooks import AgentRegistry, PolicyClient, make_pretooluse_hook
    from audit.runner.outbox_tool import OutboxWriter
    from audit.runner.probes import run_probes
    from audit.runner.scripted import run_scripted

    broker = BrokerClient(os.environ.get("BROKER_URL", "http://broker:8000/mcp"), case_token)
    writer = OutboxWriter(OUTBOX, engagement_id, scratch_dir=SCRATCH)
    hook = make_pretooluse_hook(PolicyClient(engagement_id=engagement_id), AgentRegistry())
    return await run_scripted(script, broker.fetch_all, writer, OUT, probe=lambda: run_probes(hook))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "scripted"], default="live")
    ap.add_argument("--script", default="clean")
    args = ap.parse_args()

    engagement_id = os.environ["ENGAGEMENT_ID"]
    # Take the case token out of the environment so the CLI subprocess (and the agent's Bash) don't inherit it.
    case_token = os.environ.pop("CASE_TOKEN")
    for d in (SCRATCH, OUTBOX, OUT):
        d.mkdir(parents=True, exist_ok=True)

    if args.mode == "live":
        summary = asyncio.run(run_live(engagement_id, case_token))
        (OUT / "result.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    else:
        summary = asyncio.run(run_scripted_mode(engagement_id, case_token, args.script))
    print(json.dumps({k: summary.get(k) for k in ("mode", "subtype", "num_turns", "total_cost_usd")}))


if __name__ == "__main__":
    main()

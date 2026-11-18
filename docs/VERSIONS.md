# Versions verified against

Recorded 2026-11-18. `tests/sdk_contract/` fails if these drift.

| Component | Version | How verified |
|---|---|---|
| `claude-agent-sdk` (Python) | **0.2.184** (latest on PyPI at the time) | installed; `claude_agent_sdk.__version__` |
| Claude Code CLI bundled in the SDK wheel | **2.1.327** | `claude_agent_sdk/_bundled/claude --version` |
| Claude Code CLI on the host | 2.1.327 | `claude --version` |
| `mcp` (Python, pulled in by the SDK) | 2.3.0 | `pip show mcp` (2.x: `FastMCP` is now `mcp.server.mcpserver.MCPServer`) |
| Python | 3.11 | host 3.11.6; images use `python:3.11-slim` |
| Docker | 28.3.3 (Docker Desktop, WSL2 backend) | `docker --version` |

## What the installed SDK confirmed

Introspected from the installed package (see `tests/sdk_contract/test_sdk_contract.py`):

- `ClaudeAgentOptions` includes `tools`, `allowed_tools`, `disallowed_tools`, `system_prompt`, `mcp_servers`,
  `strict_mcp_config`, `permission_mode`, `max_turns`, `max_budget_usd`, `model`, `cwd`, `env`, `can_use_tool`,
  `hooks`, `agents`, `setting_sources`, `sandbox`, `task_budget`, `effort`, `output_format`, and more.
- `PermissionMode`: `default | acceptEdits | plan | bypassPermissions | dontAsk | auto`.
- `HookEvent` (Python): `PreToolUse, PostToolUse, PostToolUseFailure, UserPromptSubmit, Stop, SubagentStop,
  PreCompact, Notification, SubagentStart, PermissionRequest`.
- `HookMatcher(matcher, hooks, timeout, on_failure)`. `on_failure="block"` makes a crashing hook block the call.
- `PreToolUseHookInput` carries `agent_id` and `agent_type`. `ToolPermissionContext` carries `agent_id`
  (no `agent_type`, hence the runner's agent registry).
- `AgentDefinition(description, prompt, tools, disallowedTools, model, skills, memory, mcpServers,
  initialPrompt, maxTurns, background, effort, permissionMode)`.
- `McpHttpServerConfig = {type: "http", url, headers}`.
- `ResultMessage` reports `subtype`, `num_turns`, `total_cost_usd`, `usage`, `model_usage`, `permission_denials`.

## Behaviour taken from the docs and covered by live tests (`RUN_LIVE=1`)

Run `RUN_LIVE=1 python -m pytest tests/sdk_contract` with `ANTHROPIC_API_KEY` set to confirm:

- (a) PreToolUse hooks fire for subagent tool calls, with `agent_type` set.
- (b) `can_use_tool` fires for a subagent's Write.
- (c) Reads inside cwd skip `can_use_tool` but still reach the hook.
- (d) A raising hook with `on_failure="block"` blocks the call.
- (e) The hidden built-in subagents are unavailable.
- (f) `max_budget_usd` ends the run with `error_max_budget_usd`.

**Status: not yet run.** No API key was available when this was built.

## Verified inside the sandbox container without an API key

A live run with no key configured (the egress proxy answers 503) still exercised CLI startup in the real
locked-down container (read-only rootfs, uid 10001, no capabilities, internal network). Its `system:init`
message reported:

- `claude_code_version: 2.1.327`, `permissionMode: default`, `cwd: /workspace`, `model: claude-sonnet-5-5`
- tools: `Task, Bash, Edit, Glob, Grep, Read, Write, mcp__broker__extract, mcp__broker__list_tables,
  mcp__outbox__propose_action, mcp__workspace__save_extract` (no WebFetch/WebSearch)
- agents: `planner, reporter, reviewer, tester` (the built-in general-purpose agent is gone)
- MCP: `broker=connected` (remote, case-token header), `outbox=connected`, `workspace=connected`
- every API request went to `egress:8080/v1/messages`. The wall-clock limit killed the run, and the gate's
  budget check failed it (retry, then escalate).

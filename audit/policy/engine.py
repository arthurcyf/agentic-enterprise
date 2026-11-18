"""Pure policy engine: (tool call, agent) -> allow/deny with a rule id.

No I/O, so it is fully unit-testable. The policy service wraps it in HTTP and
writes every decision to the audit log.
"""

# How this file fits in
# ---------------------
# Used by:  policy.service (one PolicyEngine per process) and tests/unit/test_policy.py.
# Reads:    policy.yaml in this folder. Pure function: no I/O besides loading the file.
# The callers inside the sandbox are runner.hooks (PreToolUse hook and can_use_tool), via the service.
from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path

import yaml

POLICY_FILE = Path(__file__).with_name("policy.yaml")


@dataclass(frozen=True)
class Decision:
    decision: str  # "allow" | "deny"
    rule_id: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def allow(rule: str, reason: str) -> Decision:
    return Decision("allow", rule, reason)


def deny(rule: str, reason: str) -> Decision:
    return Decision("deny", rule, reason)


def load_policy(path: Path | None = None) -> dict:
    return yaml.safe_load(Path(path or POLICY_FILE).read_text(encoding="utf-8"))


class PolicyEngine:
    def __init__(self, policy: dict | None = None):
        self.p = policy or load_policy()
        self.ws = self.p["workspace"]

    # -- paths ---------------------------------------------------------------
    def resolve(self, path: str) -> str:
        if not path:
            return self.ws
        if not path.startswith("/"):
            path = posixpath.join(self.ws, path)
        return posixpath.normpath(path)

    @staticmethod
    def _under(path: str, roots: list[str]) -> bool:
        return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)

    def is_protected(self, path: str) -> bool:
        return self._under(self.resolve(path), self.p["protected_paths"])

    # -- entry point ---------------------------------------------------------
    def decide(self, tool_name: str, tool_input: dict, agent_type: str | None = None) -> Decision:
        agent = agent_type or "lead"
        tool_input = tool_input or {}

        if tool_name in self.p["denied_tools"]:
            return deny("web.deny" if tool_name.startswith("Web") else "tool.deny", f"{tool_name} is not permitted")
        if tool_name in self.p["always_allowed_tools"]:
            return allow("tool.harmless", f"{tool_name} is a harness tool")
        if tool_name in self.p.get("agent_denied_tools", {}).get(agent, []):
            return deny("agent.tool", f"the {agent} agent may not use {tool_name}")

        if tool_name.startswith("mcp__"):
            return self._mcp(tool_name, agent)
        if tool_name in ("Agent", "Task"):
            sub = tool_input.get("subagent_type", "")
            if sub in self.p["subagents"]:
                return allow("agent.subagent", f"subagent {sub} is defined")
            return deny("agent.unknown", f"subagent type {sub!r} is not one of the defined audit agents")
        if tool_name in ("Read", "Glob", "Grep"):
            return self._read(tool_name, tool_input)
        if tool_name in ("Write", "Edit", "MultiEdit"):
            return self._write(tool_input.get("file_path", ""))
        if tool_name == "Bash":
            return self._bash(tool_input.get("command", ""))
        return deny("default.deny", f"{tool_name} is not covered by policy")

    # -- rules ---------------------------------------------------------------
    def _mcp(self, tool_name: str, agent: str) -> Decision:
        for prefix, agents in self.p["mcp"].items():
            if tool_name.startswith(prefix):
                if agent in agents:
                    return allow("mcp.allow", f"{agent} may call {tool_name}")
                return deny("mcp.agent", f"{agent} may not call {tool_name}")
        return deny("mcp.unknown", f"{tool_name} is not an approved MCP tool")

    def _read(self, tool_name: str, tool_input: dict) -> Decision:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        resolved = self.resolve(path)
        if resolved.startswith("/opt/runner") or resolved.startswith("/proc") or resolved.startswith("/etc"):
            return deny("read.protected", f"reading {resolved} is not permitted")
        if not self._under(resolved, self.p["readable_dirs"]):
            return deny("read.outside", f"{resolved} is outside the workspace")
        return allow("read.workspace", f"read inside workspace: {resolved}")

    def _write(self, path: str) -> Decision:
        resolved = self.resolve(path)
        if self.is_protected(resolved):
            return deny("write.protected", f"{resolved} is protected (runner code, config or engagement input)")
        if not self._under(resolved, self.p["writable_dirs"]):
            return deny("write.outside", f"{resolved} is outside the writable directories")
        return allow("write.workspace", f"write to {resolved}")

    def _bash(self, command: str) -> Decision:
        b = self.p["bash"]
        for s in b["denied_substrings"]:
            if s in command:
                return deny("bash.substitution", f"command contains {s!r}")
        try:
            lex = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError as e:
            return deny("bash.parse", f"cannot parse command: {e}")
        if not tokens:
            return deny("bash.empty", "empty command")

        segments: list[list[str]] = [[]]
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t == ">&" and i + 1 < len(tokens) and tokens[i + 1] in ("1", "2"):  # 2>&1
                if segments[-1] and segments[-1][-1] in ("1", "2"):
                    segments[-1].pop()
                i += 2
                continue
            if t in (">", ">>", "&>"):
                if segments[-1] and segments[-1][-1] in ("1", "2"):
                    segments[-1].pop()  # the fd number of "2> file"
                if i + 1 >= len(tokens):
                    return deny("bash.parse", "redirection without target")
                target = tokens[i + 1]
                if target != "/dev/null":
                    d = self._write(target)
                    if not d.allowed:
                        return deny("bash.redirect", f"redirection to {target}: {d.reason}")
                i += 2
                continue
            if t and set(t) <= set(";&|<>"):
                if t not in b["allowed_operators"]:
                    return deny("bash.operator", f"operator {t!r} is not allowed")
                segments.append([])
            else:
                segments[-1].append(t)
            i += 1

        for seg in segments:
            if not seg:
                return deny("bash.parse", "empty command segment")
            words = [w for w in seg if "=" not in w.split("/")[0]] or seg  # drop VAR=value prefixes
            cmd = posixpath.basename(words[0])
            if cmd in b["denied_commands"]:
                return deny("bash.denied_command", f"{cmd} is not permitted")
            if cmd not in b["allowed_commands"]:
                return deny("bash.not_allowlisted", f"{cmd} is not on the command allowlist")
            for w in words[1:]:
                hit = next((pp for pp in self.p["protected_paths"] if pp in w), None)
                if hit or ((w.startswith("/") or ".." in w) and self.is_protected(w)):
                    return deny("bash.protected_path", f"command references protected path {hit or w}")
            if cmd == "cd" and len(words) > 1 and not self._under(self.resolve(words[1]), self.p["readable_dirs"]):
                return deny("bash.cd_outside", f"cd outside the workspace: {words[1]}")
        return allow("bash.allowlisted", "every command segment is allowlisted")

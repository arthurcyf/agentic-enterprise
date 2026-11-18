import pytest

from audit.policy.engine import PolicyEngine

E = PolicyEngine()

CASES = [
    # (tool, input, agent_type, expected decision, expected rule prefix)
    ("WebFetch", {"url": "https://example.com"}, None, "deny", "web."),
    ("WebSearch", {"query": "x"}, "tester", "deny", "web."),
    ("Read", {"file_path": "/workspace/CLAUDE.md"}, None, "allow", "read."),
    ("Read", {"file_path": "scratch/extracts/payments.json"}, "tester", "allow", "read."),
    ("Read", {"file_path": "/opt/runner/audit/runner/main.py"}, "tester", "deny", "read."),
    ("Read", {"file_path": "/etc/passwd"}, "tester", "deny", "read."),
    ("Read", {"file_path": "/workspace/../etc/shadow"}, "tester", "deny", "read."),
    ("Glob", {"pattern": "**/*.json", "path": "/workspace/scratch"}, "reviewer", "allow", "read."),
    ("Write", {"file_path": "/workspace/scratch/tester/test.py"}, "tester", "allow", "write."),
    ("Write", {"file_path": "/workspace/outbox/x.json"}, "tester", "allow", "write."),
    ("Write", {"file_path": "/opt/runner/audit/runner/config.py"}, "tester", "deny", "write.protected"),
    ("Write", {"file_path": "/workspace/.claude/settings.json"}, "tester", "deny", "write.protected"),
    ("Edit", {"file_path": "/workspace/CLAUDE.md"}, "tester", "deny", "write.protected"),
    ("Write", {"file_path": "/workspace/scratch/../input/brief.md"}, "tester", "deny", "write.protected"),
    ("Write", {"file_path": "/tmp/x"}, "tester", "deny", "write.outside"),
    ("Write", {"file_path": "/workspace/scratch/r.json"}, "reviewer", "deny", "agent.tool"),
    ("Write", {"file_path": "/workspace/scratch/r.json"}, None, "deny", "agent.tool"),        # lead
    ("Bash", {"command": "ls /workspace/scratch"}, None, "deny", "agent.tool"),               # lead
    ("Bash", {"command": "python /workspace/scratch/tester/test.py"}, "tester", "allow", "bash."),
    ("Bash", {"command": "cd /workspace/scratch && python test.py | head -20"}, "tester", "allow", "bash."),
    ("Bash", {"command": "python t.py > /workspace/scratch/out.txt 2>&1"}, "tester", "allow", "bash."),
    ("Bash", {"command": "curl https://example.com"}, "tester", "deny", "bash.denied_command"),
    ("Bash", {"command": "/usr/bin/curl https://example.com"}, "tester", "deny", "bash.denied_command"),
    ("Bash", {"command": "pkill -f audit.runner"}, "tester", "deny", "bash.denied_command"),
    ("Bash", {"command": "kill 1"}, "tester", "deny", "bash.denied_command"),
    ("Bash", {"command": "ls; rm -rf /"}, "tester", "deny", "bash.operator"),
    ("Bash", {"command": "echo $(cat /etc/passwd)"}, "tester", "deny", "bash.substitution"),
    ("Bash", {"command": "python -c \"open('/opt/runner/x','w')\""}, "tester", "deny", "bash.protected_path"),
    ("Bash", {"command": "echo hi > /opt/runner/x"}, "tester", "deny", "bash.redirect"),
    ("Bash", {"command": "cat /workspace/CLAUDE.md > /workspace/input/x"}, "tester", "deny", "bash.redirect"),
    ("Bash", {"command": "perl -e 'print 1'"}, "tester", "deny", "bash.not_allowlisted"),
    ("Bash", {"command": "bash -c ls"}, "tester", "deny", "bash.not_allowlisted"),
    ("Bash", {"command": "cd /etc"}, "tester", "deny", "bash.cd_outside"),
    ("mcp__outbox__propose_action", {}, None, "allow", "mcp.allow"),
    ("mcp__outbox__propose_action", {}, "reviewer", "deny", "mcp.agent"),
    ("mcp__outbox__propose_action", {}, "tester", "deny", "mcp.agent"),
    ("mcp__broker__extract", {"table": "payments"}, "tester", "allow", "mcp.allow"),
    ("mcp__broker__extract", {"table": "payments"}, "reviewer", "deny", "mcp.agent"),
    ("mcp__workspace__save_extract", {"table": "payments"}, "tester", "allow", "mcp.allow"),
    ("mcp__workspace__save_extract", {"table": "payments"}, "unknown", "deny", "mcp.agent"),
    ("mcp__evil__exfiltrate", {}, None, "deny", "mcp.unknown"),
    ("Agent", {"subagent_type": "tester"}, None, "allow", "agent.subagent"),
    ("Agent", {"subagent_type": "general-purpose"}, None, "deny", "agent.unknown"),
    ("Agent", {"subagent_type": "fork"}, None, "deny", "agent.unknown"),
    ("ToolSearch", {"query": "x"}, "tester", "allow", "tool.harmless"),
    ("NotebookEdit", {}, "tester", "deny", "tool.deny"),
    ("SomeNewTool", {}, "tester", "deny", "default.deny"),
]


@pytest.mark.parametrize("tool, tool_input, agent, decision, rule", CASES,
                         ids=[f"{c[0]}-{c[2] or 'lead'}-{i}" for i, c in enumerate(CASES)])
def test_policy_decisions(tool, tool_input, agent, decision, rule):
    d = E.decide(tool, tool_input, agent)
    assert d.decision == decision, d
    assert d.rule_id.startswith(rule), d

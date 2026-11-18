# Sandbox image: one container per engagement, created fresh and destroyed afterwards.
#
# How this file fits in
# ---------------------
# Built by:  `python -m audit up` (audit/cli.py) as audit-sandbox:latest.
# Run by:    audit/orchestrator/engagement.py (run_container), with sandbox/seccomp.json, a read-only
#            rootfs, no capabilities, uid 10001, and only the audit_sandbox_net network.
# Contains:  the runner (audit/runner, entry audit.runner.main) and the Agent SDK with its bundled
#            Claude Code CLI. managed-settings.json is baked in read-only at /etc/claude-code.
# Talks to:  policy (hooks), broker (MCP, case token), egress (model API). Nothing else.
FROM python:3.11-slim

# The Agent SDK wheel bundles the Claude Code CLI (verified: SDK 0.2.184 bundles CLI 2.1.327).
RUN pip install --no-cache-dir "claude-agent-sdk==0.2.184" "httpx>=0.27" "pydantic>=2.6" "pyyaml>=6" pandas \
 && useradd --uid 10001 --create-home --home-dir /home/agent --shell /usr/sbin/nologin agent

# Runner code and config: owned by root and read-only to everyone, so the agent cannot modify them.
COPY audit /opt/runner/audit
COPY sandbox/managed-settings.json /etc/claude-code/managed-settings.json
RUN chown -R root:root /opt/runner /etc/claude-code \
 && chmod -R a-w,a+rX /opt/runner /etc/claude-code \
 && mkdir -p /workspace && chown root:root /workspace && chmod 755 /workspace

ENV PYTHONPATH=/opt/runner PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 10001:10001
CMD ["python", "-m", "audit.runner.main", "--mode", "scripted", "--script", "clean"]

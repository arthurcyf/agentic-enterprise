"""Orchestrator (host side): one fresh sandbox container per engagement attempt.

  stage input (brief, CLAUDE.md, settings) -> mint case token -> run container
  -> collect outbox + stream -> destroy container -> gate -> (retry once | approve | escalate)
"""

# How this file fits in
# ---------------------
# Used by:  cli.cmd_run, demo.py, evals.run.
# Calls:    common.token.mint (case token), docker (creates the container from sandbox/Dockerfile's
#           image with sandbox/seccomp.json, attaches it to audit_sandbox_net, removes it afterwards),
#           gate.gate.evaluate/apply, the audit-log client.
# Data:     var/engagements/<id>/attempt-<n>/ {input (from audit/templates), scratch, outbox, out}
#           bind-mounted into the container. On a retry it writes input/gate_feedback.md from
#           gate.feedback_for_retry, which runner.main adds to the agent's prompt.
# `runner=` lets tests swap run_container for orchestrator.local.local_scripted_runner.
from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from audit.common import config
from audit.common.auditlog_client import get_audit_log
from audit.common.token import CaseScope, mint
from audit.gate import gate as gate_mod

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "audit-sandbox:latest")
SANDBOX_NET = os.environ.get("SANDBOX_NET", "audit_sandbox_net")
SECCOMP = config.ROOT / "sandbox" / "seccomp.json"
SEED = 20260930


@dataclass
class AttemptResult:
    engagement_id: str
    attempt: int
    dir: Path
    summary: dict
    timed_out: bool = False


@dataclass
class EngagementResult:
    engagement_id: str
    attempts: list[AttemptResult] = field(default_factory=list)
    gate_results: list = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)

    @property
    def final(self):
        return self.gate_results[-1] if self.gate_results else None


def new_engagement_id() -> str:
    return f"ENG-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def case_token_secret() -> str:
    secret = os.environ.get("CASE_TOKEN_SECRET")
    if not secret:
        raise RuntimeError("CASE_TOKEN_SECRET is not set (run `python -m audit up` to create .env)")
    return secret


def stage_input(attempt_dir: Path, engagement_id: str, feedback: str | None) -> Path:
    inp = attempt_dir / "input"
    (inp / ".claude").mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATES / "CLAUDE.md", inp / "CLAUDE.md")
    shutil.copy(TEMPLATES / ".claude" / "settings.json", inp / ".claude" / "settings.json")
    brief = (TEMPLATES / "brief.md").read_text(encoding="utf-8").format(
        engagement_id=engagement_id, entity=config.ENTITY, quarter=config.QUARTER, seed=SEED)
    (inp / "brief.md").write_text(brief, encoding="utf-8")
    if feedback:
        (inp / "gate_feedback.md").write_text(feedback, encoding="utf-8")
    for d in ("scratch", "outbox", "out"):
        (attempt_dir / d).mkdir(parents=True, exist_ok=True)
        os.chmod(attempt_dir / d, 0o777)  # the sandbox user (uid 10001) writes here
    return inp


def run_container(engagement_id: str, attempt_dir: Path, mode: str, script: str,
                  timeout_s: int, max_budget_usd: float, max_turns: int) -> tuple[dict, bool]:
    import docker

    client = docker.from_env()
    inp = attempt_dir / "input"
    token = mint(CaseScope(engagement_id, config.ENTITY, config.QUARTER), case_token_secret(),
                 ttl_seconds=timeout_s + 600)
    binds = {
        str(inp.resolve()): {"bind": "/workspace/input", "mode": "ro"},
        str((inp / "CLAUDE.md").resolve()): {"bind": "/workspace/CLAUDE.md", "mode": "ro"},
        str((inp / "brief.md").resolve()): {"bind": "/workspace/brief.md", "mode": "ro"},
        str((inp / ".claude").resolve()): {"bind": "/workspace/.claude", "mode": "ro"},
        str((attempt_dir / "scratch").resolve()): {"bind": "/workspace/scratch", "mode": "rw"},
        str((attempt_dir / "outbox").resolve()): {"bind": "/workspace/outbox", "mode": "rw"},
        str((attempt_dir / "out").resolve()): {"bind": "/workspace/out", "mode": "rw"},
    }
    container = client.containers.run(
        SANDBOX_IMAGE,
        command=["python", "-m", "audit.runner.main", "--mode", mode, "--script", script],
        name=f"sandbox-{engagement_id.lower()}-{attempt_dir.name}",
        detach=True,
        user="10001:10001",
        read_only=True,
        tmpfs={"/tmp": "rw,size=256m,mode=1777", "/home/agent": "rw,size=256m,uid=10001,gid=10001,mode=700"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true", f"seccomp={SECCOMP.read_text(encoding='utf-8')}"],
        pids_limit=256,
        mem_limit="2g",
        nano_cpus=2_000_000_000,
        network=SANDBOX_NET,
        volumes=binds,
        environment={
            "ENGAGEMENT_ID": engagement_id,
            "CASE_TOKEN": token,
            "POLICY_URL": "http://policy:8000",
            "BROKER_URL": "http://broker:8000/mcp",
            "ANTHROPIC_BASE_URL": "http://egress:8080",
            "ANTHROPIC_API_KEY": "sk-dummy",   # the real key is added by the egress proxy, outside the sandbox
            "HOME": "/home/agent",
            "CLAUDE_CONFIG_DIR": "/home/agent/.claude-config",
            "MAX_BUDGET_USD": str(max_budget_usd),
            "MAX_TURNS": str(max_turns),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AUDIT_MODEL_LEAD": config.MODELS["lead"],
            "AUDIT_MODEL_TESTER": config.MODELS["tester"],
            "AUDIT_MODEL_HELPER": config.MODELS["planner"],
        },
    )
    started, timed_out = time.time(), False
    try:
        container.wait(timeout=timeout_s)
    except Exception:  # requests ReadTimeout / ConnectionError on timeout
        timed_out = True
        container.kill()
    finally:
        (attempt_dir / "out" / "container.log").write_bytes(container.logs())
        container.remove(force=True)  # destroy the sandbox: nothing persists except the collected folders
    result_file = attempt_dir / "out" / "result.json"
    summary = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {"subtype": "no_result"}
    summary["wall_seconds"] = round(time.time() - started, 1)
    summary["timed_out"] = timed_out
    if timed_out:
        summary["subtype"] = "timeout"
    return summary, timed_out


def ingest_stream(engagement_id: str, attempt: int, attempt_dir: Path, summary: dict) -> None:
    """Copy the runner's streamed SDK messages into the audit log (marked as agent-reported)."""
    log = get_audit_log()
    stream = attempt_dir / "out" / "stream.jsonl"
    if stream.exists():
        for line in stream.read_text(encoding="utf-8").splitlines():
            if line.strip():
                log.append("runner", "sdk_message", {"engagement_id": engagement_id, "attempt": attempt,
                                                     "source": "agent-reported", "message": json.loads(line)})
    log.append("orchestrator", "run_result", {"engagement_id": engagement_id, "attempt": attempt,
                                               **{k: summary.get(k) for k in (
                                                   "mode", "script", "subtype", "num_turns", "total_cost_usd",
                                                   "usage", "wall_seconds", "timed_out", "probes")}})


def run_engagement(mode: str = "live", script: str = "clean", engagement_id: str | None = None,
                   timeout_s: int = 1800, max_budget_usd: float = 3.0, max_turns: int = 80,
                   runner=None) -> EngagementResult:
    """Run attempts until the gate approves, escalates, or the one retry is used up.

    `runner` can replace run_container (tests use an in-process runner)."""
    engagement_id = engagement_id or new_engagement_id()
    runner = runner or run_container
    result = EngagementResult(engagement_id)
    log = get_audit_log()
    log.append("orchestrator", "engagement_started", {"engagement_id": engagement_id, "mode": mode, "script": script})
    feedback = None
    for attempt in (1, 2):
        attempt_dir = config.ENGAGEMENTS / engagement_id / f"attempt-{attempt}"
        stage_input(attempt_dir, engagement_id, feedback)
        summary, timed_out = runner(engagement_id, attempt_dir, mode, script, timeout_s, max_budget_usd, max_turns)
        ingest_stream(engagement_id, attempt, attempt_dir, summary)
        result.attempts.append(AttemptResult(engagement_id, attempt, attempt_dir, summary, timed_out))
        g = gate_mod.evaluate(engagement_id, attempt_dir / "outbox", attempt=attempt, run_summary=summary)
        result.gate_results.append(g)
        result.action_ids += gate_mod.apply(g)
        if g.outcome != "retry":
            break
        feedback = gate_mod.feedback_for_retry(g)
    return result

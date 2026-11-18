# audit/templates/

The engagement context given to the agents. Comments are not added to the files themselves, because
anything in them ends up in the agents' context. This page explains how they interact with the code
instead.

`audit/orchestrator/engagement.py` (`stage_input`) copies these files into
`var/engagements/<id>/attempt-<n>/input/` for every attempt. It then mounts them **read-only** into the
sandbox at `/workspace`. This README itself is not copied.

| File | Loaded by | Interacts with |
|---|---|---|
| `CLAUDE.md` | The Claude Code CLI in the sandbox, because `audit/runner/main.py` sets `setting_sources=["project"]` | It's the methodology the agents follow: population, sampling call, test rules, sample-size table, risk register, last year's findings, output shapes. The **gate mirrors it in code**: `audit/gate/sampling.py` (method and minimum sizes), `audit/gate/reperform.py` (test rules), `audit/common/schemas.py` (output shapes). If you change one, change the other. |
| `brief.md` | The Lead agent reads it (`audit/runner/agents.py` tells it to) | It's a Python `str.format` template. `stage_input` fills in `{engagement_id}`, `{entity}`, `{quarter}` and `{seed}` from `audit/common/config.py`. The seed must match what the gate redraws with. |
| `.claude/settings.json` | The Claude Code CLI (project settings, via `setting_sources=["project"]`) | It holds project-level deny rules and turns off auto memory and claude.ai connectors. There are deliberately no allow rules, so nothing skips the policy hook. It duplicates `sandbox/managed-settings.json` and the `disallowed_tools` in `audit/runner/main.py`. |

When a gate check fails on the first attempt, the orchestrator also writes `input/gate_feedback.md`
(from `audit/gate/gate.py` `feedback_for_retry`). `audit/runner/main.py` appends it to the agent's
prompt for the retry.

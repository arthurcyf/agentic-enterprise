# sandbox/

Files that define the per-engagement sandbox container. JSON can't hold comments, so this page explains
how each file interacts with the rest of the repo.

| File | Used by | What it does |
|---|---|---|
| `Dockerfile` | `python -m audit up` builds it as `audit-sandbox:latest`; `audit/orchestrator/engagement.py` runs one container per attempt | Installs the Agent SDK (with its bundled Claude Code CLI) and copies `audit/` to `/opt/runner`, owned by root and read-only. Runs as uid 10001. The container's command is `audit.runner.main`. |
| `seccomp.json` | `audit/orchestrator/engagement.py` passes it as `security_opt=["seccomp=…"]` | Denies `ptrace`, `process_vm_readv/writev`, `pidfd_getfd`, namespace and kernel syscalls. The agent's code can't read or patch the runner's memory, or create namespaces. |
| `managed-settings.json` | Baked into the image at `/etc/claude-code/managed-settings.json` (root-owned, read-only). The Claude Code CLI loads it whatever `setting_sources` says. | Repeats the hard deny rules: web tools, edits to `/opt/runner`, `/etc/claude-code`, the engagement input and `.claude`, and network or kill commands. Disables `bypassPermissions` mode and auto memory. It's a second copy of the rules: the runner sets the same denials through `disallowed_tools` in `audit/runner/main.py`, and the policy service decides everything else. |

At runtime, the orchestrator mounts these into each container:

```
var/engagements/<id>/attempt-<n>/input   -> /workspace/input (ro), plus CLAUDE.md, brief.md, .claude/ at /workspace (ro)
var/engagements/<id>/attempt-<n>/scratch -> /workspace/scratch (rw)   agents' working files
var/engagements/<id>/attempt-<n>/outbox  -> /workspace/outbox  (rw)   proposals, read by the gate
var/engagements/<id>/attempt-<n>/out     -> /workspace/out     (rw)   stream.jsonl, result.json
tmpfs                                    -> /tmp, /home/agent        fresh HOME and CLAUDE_CONFIG_DIR, so no user settings
```

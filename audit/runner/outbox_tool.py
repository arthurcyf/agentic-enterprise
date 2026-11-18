"""The agents' only way to affect anything outside the sandbox: propose an action.

`propose_action` validates the proposal and writes it as a JSON file to
/workspace/outbox. Nothing is executed here. After the run, the gate reads
these files from outside the sandbox and decides.
"""

# How this file fits in
# ---------------------
# Used by:  runner.main (in-process MCP server `outbox`, tool mcp__outbox__propose_action, for the Lead)
#           runner.scripted and orchestrator.local (OutboxWriter directly, no LLM).
# Calls:    common.schemas (validate), common.canon.proposal_hash (stamp).
# Writes:   /workspace/outbox/NNN-<type>-<target>.json, collected by orchestrator.engagement and read
#           by gate.gate.load_proposals. Only payload files under /workspace/scratch may be referenced.
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from audit.common.canon import proposal_hash
from audit.common.schemas import PAYLOAD_MODEL, Proposal

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action_type": {"type": "string", "enum": ["file_workpaper", "raise_finding", "notify_auditor"]},
        "target": {"type": "string", "description": "Stable target id, e.g. 'workpaper:AP-01', 'finding:F-01', 'auditor:lead'"},
        "payload": {"type": "object", "description": "Workpaper, Finding or Notification object (see CLAUDE.md)"},
        "payload_file": {"type": "string", "description": "Instead of payload: path to a JSON file under /workspace/scratch holding the payload object"},
        "evidence_refs_file": {"type": "string", "description": "Instead of evidence_refs: path to a JSON file under /workspace/scratch holding the list"},
        "evidence_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"}, "row_id": {"type": "string"}, "row_hash": {"type": "string"},
                    "values": {"type": "object"},
                },
                "required": ["table", "row_id", "row_hash"],
            },
        },
    },
    "required": ["action_type", "target"],
}


class OutboxWriter:
    def __init__(self, outbox_dir: Path, engagement_id: str, scratch_dir: Path | None = None):
        self.dir = Path(outbox_dir)
        self.engagement_id = engagement_id
        self.scratch = Path(scratch_dir).resolve() if scratch_dir else None
        self.dir.mkdir(parents=True, exist_ok=True)

    def _load_json_file(self, path: str):
        if self.scratch is None:
            raise ValueError("file inputs are not enabled")
        p = Path(path).resolve()
        if self.scratch not in p.parents:
            raise ValueError(f"{path} is not under the scratch directory")
        return json.loads(p.read_text(encoding="utf-8"))

    def _next_seq(self) -> int:
        return len(list(self.dir.glob("*.json"))) + 1

    def propose(self, args: dict[str, Any], proposed_by: str = "lead") -> dict[str, Any]:
        """Validate and write one proposal. Returns {'ok': True, 'file': ..., 'proposal_hash': ...} or an error."""
        args = dict(args)
        try:
            if args.get("payload_file"):
                args["payload"] = self._load_json_file(args.pop("payload_file"))
            if args.get("evidence_refs_file"):
                args["evidence_refs"] = self._load_json_file(args.pop("evidence_refs_file"))
        except (ValueError, OSError, json.JSONDecodeError) as e:
            return {"ok": False, "error": f"cannot read input file: {e}"}
        body = {
            "engagement_id": self.engagement_id,
            "action_type": args.get("action_type"),
            "target": args.get("target"),
            "payload": args.get("payload") or {},
            "evidence_refs": args.get("evidence_refs") or [],
            "proposed_by": proposed_by,
        }
        try:
            prop = Proposal.model_validate(body)
            PAYLOAD_MODEL[prop.action_type].model_validate(prop.payload)
        except (ValidationError, KeyError) as e:
            return {"ok": False, "error": f"invalid proposal: {e}"}
        body = prop.model_dump(exclude={"proposal_hash"})
        body["proposal_hash"] = proposal_hash(body)
        safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "_", body["target"])[:60]
        path = self.dir / f"{self._next_seq():03d}-{body['action_type']}-{safe_target}.json"
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
        return {"ok": True, "file": str(path), "proposal_hash": body["proposal_hash"]}


def build_outbox_server(writer: OutboxWriter):
    """In-process SDK MCP server exposing propose_action."""
    from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

    @tool(
        "propose_action",
        "Propose an action for the verification gate. Action types: file_workpaper, raise_finding, "
        "notify_auditor. Nothing is executed here; the gate re-performs your work before anything is filed.",
        PROPOSE_SCHEMA,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    async def propose_action(args: dict[str, Any]) -> dict[str, Any]:
        result = writer.propose(args)
        return {"content": [{"type": "text", "text": json.dumps(result)}], "is_error": not result["ok"]}

    return create_sdk_mcp_server(name="outbox", version="1.0.0", tools=[propose_action])

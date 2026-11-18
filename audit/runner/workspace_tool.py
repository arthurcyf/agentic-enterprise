"""save_extract: pull a whole AP table through the broker into the workspace.

A full payments table is far too large to return as a tool result. This
in-process tool pages through the remote broker (using the case token the
runner holds) and writes the rows to /workspace/scratch/extracts/<table>.json,
so the Tester can analyse them with Python. The broker is still the only path
to ERP data and still enforces the token's scope.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from audit.runner.broker_client import BrokerClient, BrokerError

TABLES = ["payments", "purchase_orders", "goods_receipts", "approvals", "users_and_roles"]


def build_workspace_server(broker: BrokerClient, extracts_dir: Path):
    from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

    extracts_dir = Path(extracts_dir)

    @tool(
        "save_extract",
        "Extract a full AP table through the broker and save it as JSON (a list of rows) under "
        "/workspace/scratch/extracts/<table>.json. Every row carries _row_id and _row_hash for evidence.",
        {"type": "object", "properties": {"table": {"type": "string", "enum": TABLES}}, "required": ["table"]},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    async def save_extract(args: dict[str, Any]) -> dict[str, Any]:
        table = args.get("table", "")
        try:
            rows = await broker.fetch_all(table)
        except BrokerError as e:
            return {"content": [{"type": "text", "text": f"broker refused: {e}"}], "is_error": True}
        extracts_dir.mkdir(parents=True, exist_ok=True)
        path = extracts_dir / f"{table}.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        cols = list(rows[0].keys()) if rows else []
        return {"content": [{"type": "text", "text": json.dumps(
            {"saved": str(path), "rows": len(rows), "columns": cols})}]}

    return create_sdk_mcp_server(name="workspace", version="1.0.0", tools=[save_extract])

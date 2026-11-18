"""Secrets broker exposed to the sandbox as a remote MCP server (streamable HTTP).

The case token arrives as the X-Case-Token request header, which the runner
sets in its MCP server config.
"""

# How this file fits in
# ---------------------
# Runs as:  compose service `broker` (on backend AND sandbox_net, so the sandbox can reach it).
# Called by: the Claude Code CLI in the sandbox, as MCP tools mcp__broker__list_tables / extract
#           (configured in runner.main.build_options with the X-Case-Token header), and
#           runner.broker_client (used by save_extract and the scripted agent).
# Calls:    broker.core.Broker for every request.
from __future__ import annotations

import json
import os

from mcp.server.mcpserver import Context, MCPServer

from audit.broker.core import Broker, ScopeError


def _token(ctx: Context) -> str | None:
    headers = ctx.headers or {}
    return headers.get("x-case-token") or headers.get("X-Case-Token")


def build_mcp(broker: Broker) -> MCPServer:
    mcp = MCPServer(name="broker", instructions="Read-only access to this engagement's AP tables in the ERP.")

    @mcp.tool(description="List the AP tables and allowlisted fields available to this engagement.")
    def list_tables(ctx: Context) -> str:
        try:
            return json.dumps(broker.list_tables(_token(ctx)))
        except ScopeError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool(description=(
        "Extract one page (max 200 rows) of an AP table for this engagement; entity and quarter come from "
        "the case token. Returns JSON with 'rows' and 'next_offset'. Each row has _row_id and _row_hash, "
        "which you must cite as evidence. Tables: payments, purchase_orders, goods_receipts, approvals, "
        "users_and_roles. To analyse a whole table, use mcp__workspace__save_extract, which saves every "
        "page to a file."
    ))
    def extract(table: str, ctx: Context, entity: str | None = None, quarter: str | None = None,
                offset: int = 0, limit: int = 200) -> str:
        try:
            return json.dumps(broker.extract(_token(ctx), table, entity, quarter, offset, limit))
        except ScopeError as e:
            return json.dumps({"error": str(e)})

    return mcp


def build_app(broker: Broker):
    from mcp.server.transport_security import TransportSecuritySettings

    # Sandbox containers reach the broker by its compose host name; there is no browser here,
    # so DNS-rebinding protection (which checks the Host header) is not needed.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return build_mcp(broker).streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=security, host="0.0.0.0")


def main() -> None:
    import uvicorn

    from audit.common.auditlog_client import get_audit_log

    broker = Broker(secret=os.environ["CASE_TOKEN_SECRET"], audit=get_audit_log())
    uvicorn.run(build_app(broker), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

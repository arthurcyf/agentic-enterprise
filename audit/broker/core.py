"""Broker logic, independent of the MCP transport so it is easy to test.

The broker is the only holder of the ERP API key. The sandbox presents a
case token; the broker verifies it and refuses anything outside its scope,
so the agent cannot use the broker's authority for another entity, quarter
or table (no confused deputy).
"""
from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from audit.common.canon import ALLOWLIST, PRIMARY_KEY, project_row, row_hash
from audit.common.token import CaseScope, TokenError, verify


class ScopeError(Exception):
    pass


ErpFetch = Callable[[str, str, str], list[dict[str, Any]]]


def http_erp_fetch(table: str, entity: str, quarter: str) -> list[dict[str, Any]]:
    url = os.environ.get("ERP_URL", "http://erp:8000")
    r = httpx.get(f"{url}/tables/{table}", params={"entity": entity, "quarter": quarter},
                  headers={"X-ERP-Key": os.environ["ERP_API_KEY"]}, timeout=30)
    r.raise_for_status()
    return r.json()["rows"]


class Broker:
    def __init__(self, secret: str, erp_fetch: ErpFetch = http_erp_fetch, audit=None):
        self.secret = secret
        self.erp_fetch = erp_fetch
        self.audit = audit

    def _log(self, scope: CaseScope | None, tool: str, args: dict, decision: str, detail: str, rows: int = 0):
        if self.audit:
            self.audit.append("broker", "broker_call", {
                "engagement_id": scope.engagement_id if scope else None,
                "token_id": scope.token_id if scope else None,
                "tool": tool, "args": args, "decision": decision, "detail": detail, "rows": rows,
            })

    def authorise(self, token: str | None, tool: str, args: dict) -> CaseScope:
        if not token:
            self._log(None, tool, args, "deny", "missing case token")
            raise ScopeError("missing X-Case-Token header")
        try:
            return verify(token, self.secret)
        except TokenError as e:
            self._log(None, tool, args, "deny", f"token rejected: {e}")
            raise ScopeError(f"case token rejected: {e}")

    def list_tables(self, token: str | None) -> dict:
        scope = self.authorise(token, "list_tables", {})
        self._log(scope, "list_tables", {}, "allow", "ok")
        return {"entity": scope.entity, "quarter": scope.quarter,
                "tables": {t: ALLOWLIST[t] for t in scope.tables}}

    def extract(self, token: str | None, table: str, entity: str | None = None, quarter: str | None = None,
                offset: int = 0, limit: int = 200) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        args = {"table": table, "entity": entity, "quarter": quarter, "offset": offset, "limit": limit}
        scope = self.authorise(token, "extract", args)
        if table not in scope.tables:
            self._log(scope, "extract", args, "deny", "table outside token scope")
            raise ScopeError(f"table {table!r} is outside this engagement's scope")
        if entity not in (None, scope.entity):
            self._log(scope, "extract", args, "deny", "entity outside token scope")
            raise ScopeError(f"entity {entity!r} is outside this engagement's scope ({scope.entity})")
        if quarter not in (None, scope.quarter):
            self._log(scope, "extract", args, "deny", "quarter outside token scope")
            raise ScopeError(f"quarter {quarter!r} is outside this engagement's scope ({scope.quarter})")
        raw = self.erp_fetch(table, scope.entity, scope.quarter)
        rows = []
        for r in raw:
            # Defence in depth: never trust the upstream filter for scoped tables.
            if table == "payments" and (r.get("entity") != scope.entity or r.get("quarter") != scope.quarter):
                continue
            if table == "purchase_orders" and r.get("entity") != scope.entity:
                continue
            out = project_row(table, r)
            out["_row_id"] = out[PRIMARY_KEY[table]]
            out["_row_hash"] = row_hash(table, r)
            rows.append(out)
        page = rows[offset:offset + limit]
        self._log(scope, "extract", args, "allow", "ok", rows=len(page))
        return {"table": table, "entity": scope.entity, "quarter": scope.quarter, "total_rows": len(rows),
                "offset": offset, "row_count": len(page), "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
                "rows": page}

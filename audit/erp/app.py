"""Mock ERP API. Requires the ERP API key, which only the broker holds.

It returns full rows (including fields such as user e-mail that are not
allowlisted); the broker strips them before anything reaches the agent.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException

from audit.common.canon import AP_TABLES
from audit.data.generate import load_table

SOURCE_DIR = Path(os.environ.get("SOURCE_DIR", "/data/source"))
app = FastAPI(title="mock-erp")


def _check_key(x_erp_key: str | None) -> None:
    expected = os.environ.get("ERP_API_KEY", "")
    if not expected or not x_erp_key or not hmac.compare_digest(x_erp_key, expected):
        raise HTTPException(status_code=401, detail="invalid ERP API key")


@app.get("/tables/{table}")
def get_table(table: str, entity: str | None = None, quarter: str | None = None,
              x_erp_key: str | None = Header(default=None)) -> dict:
    _check_key(x_erp_key)
    if table not in AP_TABLES:
        raise HTTPException(status_code=404, detail="unknown table")
    rows = load_table(table, SOURCE_DIR)
    if table == "payments":
        rows = [r for r in rows if (entity is None or r["entity"] == entity)
                and (quarter is None or r["quarter"] == quarter)]
    elif table == "purchase_orders" and entity is not None:
        rows = [r for r in rows if r["entity"] == entity]
    return {"table": table, "rows": rows}


@app.get("/health")
def health() -> dict:
    return {"ok": True}

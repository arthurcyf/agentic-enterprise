"""Canonical JSON and hashing shared by every component.

The broker, the gate and the executor all hash the same canonical form, so a
row hash or proposal hash computed in one place can be checked in another.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# Fields each table exposes through the broker. Everything else (for example
# user e-mail addresses) never leaves the ERP. Row hashes cover exactly these.
ALLOWLIST: dict[str, list[str]] = {
    "payments": [
        "payment_id", "entity", "quarter", "vendor_id", "vendor_name", "po_id",
        "amount", "payment_date", "requested_by", "memo",
    ],
    "purchase_orders": ["po_id", "entity", "vendor_id", "created_by", "po_date", "amount", "status"],
    "goods_receipts": ["gr_id", "po_id", "received_by", "gr_date", "amount"],
    "approvals": ["approval_id", "payment_id", "approver_id", "approved_at", "level"],
    "users_and_roles": ["user_id", "name", "role", "department"],
}

PRIMARY_KEY: dict[str, str] = {
    "payments": "payment_id",
    "purchase_orders": "po_id",
    "goods_receipts": "gr_id",
    "approvals": "approval_id",
    "users_and_roles": "user_id",
}

AP_TABLES = tuple(ALLOWLIST)


def canon(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def project_row(table: str, row: dict[str, Any]) -> dict[str, str]:
    """Keep only allowlisted fields, as strings (CSV values are strings)."""
    return {k: str(row.get(k, "")) for k in ALLOWLIST[table]}


def row_hash(table: str, row: dict[str, Any]) -> str:
    return sha256_hex(canon({"table": table, "row": project_row(table, row)}))


def proposal_hash(proposal: dict[str, Any]) -> str:
    """Hash of a proposal without its own hash field."""
    body = {k: v for k, v in proposal.items() if k != "proposal_hash"}
    return sha256_hex(canon(body))


def idempotency_key(engagement_id: str, action_type: str, target: str) -> str:
    return sha256_hex(f"{engagement_id}|{action_type}|{target}")

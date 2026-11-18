"""Independent re-performance of control AP-01 from the authoritative data.

This is deliberately separate code from anything the agents or the scripted
agent use. The gate trusts only this result and the source CSVs, never the
agent's narrative.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from audit.data.generate import load_table


@dataclass
class Reperformance:
    population: list[str]                                  # sorted payment ids >= threshold, in scope
    item_results: dict[str, dict[str, str]]                # payment_id -> {three_way_match, sod}
    exceptions: set[tuple[str, tuple[str, ...]]] = field(default_factory=set)  # (type, payment_ids)
    payments: dict[str, dict[str, str]] = field(default_factory=dict)          # in-scope payments by id


def reperform(source_dir: Path, entity: str, quarter: str, threshold: float) -> Reperformance:
    limit = Decimal(str(threshold))
    payments = [p for p in load_table("payments", source_dir)
                if p["entity"] == entity and p["quarter"] == quarter]
    pos = {r["po_id"]: r for r in load_table("purchase_orders", source_dir)}
    receipts_by_po: dict[str, int] = defaultdict(int)
    for g in load_table("goods_receipts", source_dir):
        receipts_by_po[g["po_id"]] += 1
    approvers: dict[str, list[str]] = defaultdict(list)
    for a in load_table("approvals", source_dir):
        approvers[a["payment_id"]].append(a["approver_id"])

    def matched(p: dict) -> bool:
        po = pos.get(p["po_id"]) if p["po_id"] else None
        return bool(po and po["vendor_id"] == p["vendor_id"] and po["entity"] == p["entity"]
                    and receipts_by_po[po["po_id"]] > 0)

    def segregated(p: dict) -> bool:
        who = approvers.get(p["payment_id"], [])
        return bool(who) and all(a != p["requested_by"] for a in who)

    big = sorted(p["payment_id"] for p in payments if Decimal(p["amount"]) >= limit)
    by_id = {p["payment_id"]: p for p in payments}
    results, exceptions = {}, set()
    for pid in big:
        p = by_id[pid]
        twm, sod = matched(p), segregated(p)
        results[pid] = {"three_way_match": "pass" if twm else "fail", "sod": "pass" if sod else "fail"}
        if not twm:
            exceptions.add(("no_po", (pid,)))
        if not sod:
            exceptions.add(("sod", (pid,)))

    # Split payments: same vendor, same day, each under the threshold, together at or over it.
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in payments:
        if Decimal(p["amount"]) < limit:
            groups[(p["vendor_id"], p["payment_date"])].append(p)
    for members in groups.values():
        if len(members) >= 2 and sum(Decimal(m["amount"]) for m in members) >= limit:
            exceptions.add(("split", tuple(sorted(m["payment_id"] for m in members))))

    return Reperformance(population=big, item_results=results, exceptions=exceptions, payments=by_id)

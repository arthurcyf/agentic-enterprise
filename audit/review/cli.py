"""Human review queue: an auditor sees escalated items with their evidence and decides.

    python -m audit review list
    python -m audit review show <id>
    python -m audit review approve <id> --by "Jane Auditor"
    python -m audit review reject <id> --by "Jane Auditor" [--note "..."]

Approving a gate escalation uses the same transactional outbox as the gate
(approval row + outbox row in one transaction), with approver "human:<name>".
"""
from __future__ import annotations

import json

from audit.common import config
from audit.common.auditlog_client import get_audit_log
from audit.common.canon import PRIMARY_KEY, project_row, row_hash
from audit.data.generate import load_table
from audit.outbox import db


def list_items(conn, status: str = "open") -> list[dict]:
    rows = conn.execute("SELECT * FROM review_queue WHERE status = ? ORDER BY id", (status,)).fetchall()
    out = []
    for r in rows:
        p = conn.execute("SELECT action_type, target FROM proposals WHERE proposal_hash = ?",
                         (r["proposal_hash"],)).fetchone()
        out.append({"id": r["id"], "engagement_id": r["engagement_id"], "source": r["source"],
                    "action": f"{p['action_type']} {p['target']}" if p else "-",
                    "reasons": json.loads(r["reasons_json"])})
    return out


def show(conn, item_id: int) -> str:
    r = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    if r is None:
        return f"no review item {item_id}"
    lines = [f"Review item {r['id']}  [{r['status']}]  engagement {r['engagement_id']}  (escalated by {r['source']})",
             "", "Why it was escalated:"]
    lines += [f"  - {x}" for x in json.loads(r["reasons_json"])]
    p = conn.execute("SELECT body_json FROM proposals WHERE proposal_hash = ?", (r["proposal_hash"],)).fetchone()
    if p:
        body = json.loads(p["body_json"])
        lines += ["", f"Proposal: {body['action_type']} -> {body['target']}",
                  json.dumps(body["payload"], indent=2)[:3000]]
        refs = body.get("evidence_refs") or []
        if refs:
            lines += ["", "Cited evidence vs. source data:"]
            index = {}
            for ref in refs:
                t = ref["table"]
                if t not in index and t in PRIMARY_KEY:
                    index[t] = {row[PRIMARY_KEY[t]]: row for row in load_table(t, config.SOURCE_DIR)}
                src = index.get(t, {}).get(ref["row_id"])
                if src is None:
                    lines.append(f"  {t}/{ref['row_id']}: NOT FOUND in source")
                    continue
                ok = row_hash(t, src) == ref["row_hash"]
                lines.append(f"  {t}/{ref['row_id']}: hash {'matches' if ok else 'DOES NOT MATCH'} source")
                if ref.get("values"):
                    lines.append(f"      cited:  {ref['values']}")
                lines.append(f"      source: {project_row(t, src)}")
    return "\n".join(lines)


def decide(conn, item_id: int, approve: bool, by: str, note: str = "") -> str:
    r = conn.execute("SELECT * FROM review_queue WHERE id = ?", (item_id,)).fetchone()
    if r is None or r["status"] != "open":
        return f"review item {item_id} is not open"
    approver = f"human:{by}"
    result = "rejected"
    if approve:
        if r["source"] == "gate":
            p = conn.execute("SELECT body_json FROM proposals WHERE proposal_hash = ?", (r["proposal_hash"],)).fetchone()
            if p is None:
                return f"review item {item_id} has no proposal to approve"
            aid = db.approve_and_enqueue(conn, json.loads(p["body_json"]), approver=approver)
            result = f"approved; enqueued action {aid}" if aid else "approved (was already approved)"
        else:  # executor escalation: allow one more execution attempt
            from audit.executor.worker import connect_ledger
            from audit.outbox.relay import connect_queue

            q = connect_queue()
            item = q.execute("SELECT * FROM queue WHERE proposal_hash = ?", (r["proposal_hash"],)).fetchone()
            if item:
                connect_ledger().execute("DELETE FROM ledger WHERE idempotency_key = ? AND status = 'failed'",
                                         (item["idempotency_key"],))
                q.execute("UPDATE queue SET status = 'pending' WHERE action_id = ?", (item["action_id"],))
            result = "approved for re-execution"
    conn.execute("UPDATE review_queue SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                 ("approved" if approve else "rejected", approver, db.now(), item_id))
    get_audit_log().append("review", "review_decision", {"review_id": item_id, "engagement_id": r["engagement_id"],
                                                         "proposal_hash": r["proposal_hash"], "decision":
                                                         "approve" if approve else "reject", "by": approver,
                                                         "note": note, "result": result})
    return result

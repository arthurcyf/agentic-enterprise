"""Deterministic verification gate (no LLM).

Inputs: the proposals the sandbox left in its outbox, the run summary, and the
authoritative source data. Output per engagement attempt:
  approved  -> every proposal is approved and enqueued (transactional outbox)
  retry     -> first attempt failed; the orchestrator re-runs the agent once with the reasons
  escalated -> second failure, or a high-risk conclusion; a human decides in the review CLI
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from audit.common import config
from audit.common.auditlog_client import get_audit_log
from audit.common.canon import PRIMARY_KEY, project_row, proposal_hash, row_hash
from audit.common.schemas import PAYLOAD_MODEL, Proposal, Workpaper
from audit.data.generate import load_table
from audit.gate import sampling
from audit.gate.reperform import reperform
from audit.outbox import db

SECRET_RE = re.compile(r"sk-ant-|x-api-key|BEGIN [A-Z ]*PRIVATE KEY")
BUDGET_FAILURES = {"error_max_turns", "error_max_budget_usd", "timeout", "error_during_execution"}


@dataclass
class Check:
    name: str
    ok: bool
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "details": self.details[:50]}


@dataclass
class GateResult:
    engagement_id: str
    attempt: int
    outcome: str
    checks: list[Check]
    proposals: list[dict]
    escalation_reason: str = ""

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def summary(self) -> dict:
        return {"engagement_id": self.engagement_id, "attempt": self.attempt, "outcome": self.outcome,
                "escalation_reason": self.escalation_reason, "checks": [c.as_dict() for c in self.checks]}


def load_proposals(outbox_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(outbox_dir).glob("*.json"))]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_schema(engagement_id: str, proposals: list[dict]) -> tuple[Check, Workpaper | None, list[dict]]:
    c = Check("schema", True)
    workpapers, findings = [], []
    if not proposals:
        c.ok = False
        c.details.append("no proposals in outbox")
    for p in proposals:
        pid = p.get("target", "?")
        try:
            prop = Proposal.model_validate(p)
            payload = PAYLOAD_MODEL[prop.action_type].model_validate(prop.payload)
        except (ValidationError, KeyError) as e:
            c.ok = False
            c.details.append(f"{pid}: invalid proposal: {str(e).splitlines()[0]}")
            continue
        if prop.engagement_id != engagement_id:
            c.ok = False
            c.details.append(f"{pid}: wrong engagement id {prop.engagement_id}")
        if proposal_hash(p) != p.get("proposal_hash"):
            c.ok = False
            c.details.append(f"{pid}: proposal_hash does not match content (modified after proposing)")
        if prop.action_type == "file_workpaper":
            workpapers.append(payload)
        elif prop.action_type == "raise_finding":
            findings.append(p)
            if not prop.evidence_refs:
                c.ok = False
                c.details.append(f"{pid}: finding cites no evidence rows")
    if len(workpapers) != 1:
        c.ok = False
        c.details.append(f"expected exactly one workpaper, found {len(workpapers)}")
    return c, (workpapers[0] if len(workpapers) == 1 else None), findings


def check_evidence(proposals: list[dict], workpaper: Workpaper | None, source_dir: Path,
                   entity: str, quarter: str) -> Check:
    c = Check("evidence_integrity", True)
    index = {t: {r[k]: r for r in load_table(t, source_dir)} for t, k in PRIMARY_KEY.items()}

    def verify_ref(where: str, table: str, row_id: str, cited_hash: str, values: dict | None) -> None:
        row = index.get(table, {}).get(row_id)
        if row is None:
            c.ok = False
            c.details.append(f"{where}: {table}/{row_id} does not exist in source")
            return
        if table == "payments" and (row["entity"] != entity or row["quarter"] != quarter):
            c.ok = False
            c.details.append(f"{where}: {table}/{row_id} is outside the engagement scope")
        if row_hash(table, row) != cited_hash:
            c.ok = False
            c.details.append(f"{where}: {table}/{row_id} row_hash does not match source (evidence altered)")
        if values:
            src = project_row(table, row)
            for k, v in values.items():
                if src.get(k) != str(v):
                    c.ok = False
                    c.details.append(f"{where}: {table}/{row_id}.{k} cited as {v!r}, source has {src.get(k)!r}")

    for p in proposals:
        for ref in p.get("evidence_refs", []) or []:
            verify_ref(p.get("target", "?"), ref.get("table", ""), ref.get("row_id", ""),
                       ref.get("row_hash", ""), ref.get("values"))
        if p.get("action_type") == "raise_finding":
            cited = {r.get("row_id") for r in p.get("evidence_refs", []) if r.get("table") == "payments"}
            missing = set(p.get("payload", {}).get("payment_ids", [])) - cited
            if missing:
                c.ok = False
                c.details.append(f"{p.get('target')}: payments {sorted(missing)} not cited as evidence")
    if workpaper:
        for r in workpaper.sample_results:
            verify_ref("workpaper.sample_results", "payments", r.payment_id, r.row_hash, None)
    return c


def check_sample(workpaper: Workpaper | None, population: list[str]) -> Check:
    c = Check("sample_integrity", True)
    if workpaper is None:
        return Check("sample_integrity", False, ["no valid workpaper"])
    pop, s = workpaper.population, workpaper.sample
    if (pop.entity, pop.quarter, float(pop.min_amount)) != (config.ENTITY, config.QUARTER, config.THRESHOLD):
        c.ok = False
        c.details.append(f"population filter {pop.entity}/{pop.quarter}/>={pop.min_amount} is not the engagement scope")
    if pop.size != len(population):
        c.ok = False
        c.details.append(f"population size {pop.size} != {len(population)} recomputed")
    if s.size != len(s.ids):
        c.ok = False
        c.details.append(f"sample size {s.size} != {len(s.ids)} ids listed")
    minimum = sampling.minimum_for(workpaper.control_id, len(population))
    if s.size < minimum:
        c.ok = False
        c.details.append(f"sample size {s.size} below methodology minimum {minimum}")
    if s.size <= len(population):
        redrawn = sampling.draw(population, s.seed, s.size)
        if redrawn != s.ids:
            c.ok = False
            diff = sorted(set(s.ids) ^ set(redrawn))[:10]
            c.details.append(f"sample does not match redraw from seed {s.seed} (differs on {diff or 'order'})")
    else:
        c.ok = False
        c.details.append("sample larger than population")
    tested = [r.payment_id for r in workpaper.sample_results]
    if sorted(tested) != sorted(s.ids):
        c.ok = False
        c.details.append("sample_results do not cover exactly the sampled items")
    return c


def check_reperformance(workpaper: Workpaper | None, findings: list[dict], rp) -> Check:
    c = Check("reperformance", True)
    if workpaper is None:
        return Check("reperformance", False, ["no valid workpaper"])
    for r in workpaper.sample_results:
        expected = rp.item_results.get(r.payment_id)
        if expected is None:
            c.ok = False
            c.details.append(f"{r.payment_id}: not in recomputed population")
            continue
        for attr in ("three_way_match", "sod"):
            if getattr(r, attr) != expected[attr]:
                c.ok = False
                c.details.append(f"{r.payment_id}.{attr}: agent says {getattr(r, attr)}, re-performance says {expected[attr]}")
    expected = rp.exceptions
    claimed_wp = {(e.type, tuple(sorted(e.payment_ids))) for e in workpaper.population_exceptions}
    claimed_f = {(f["payload"]["type"], tuple(sorted(f["payload"]["payment_ids"]))) for f in findings
                 if "payload" in f and "type" in f["payload"]}
    for label, claimed in (("workpaper", claimed_wp), ("findings", claimed_f)):
        for miss in sorted(expected - claimed):
            c.ok = False
            c.details.append(f"{label}: missed exception {miss[0]} {list(miss[1])}")
        for extra in sorted(claimed - expected):
            c.ok = False
            c.details.append(f"{label}: reported exception not supported by data {extra[0]} {list(extra[1])}")
    return c


def check_consistency(workpaper: Workpaper | None, rp) -> Check:
    c = Check("conclusion_consistency", True)
    if workpaper is None:
        return Check("conclusion_consistency", False, ["no valid workpaper"])
    if workpaper.conclusion == "effective" and (workpaper.population_exceptions or rp.exceptions):
        c.ok = False
        c.details.append("conclusion 'effective' is incompatible with exceptions in the population")
    if workpaper.conclusion == "not_effective" and not rp.exceptions:
        c.ok = False
        c.details.append("conclusion 'not_effective' but re-performance found no exceptions")
    return c


def check_budget(run_summary: dict | None) -> Check:
    c = Check("budget", True)
    if not run_summary:
        return c  # scripted runs without a summary are not budget-limited
    sub = run_summary.get("subtype")
    if sub in BUDGET_FAILURES or run_summary.get("timed_out"):
        c.ok = False
        c.details.append(f"run ended with {sub or 'timeout'} (turns={run_summary.get('num_turns')}, "
                         f"cost=${run_summary.get('total_cost_usd')}, wall={run_summary.get('wall_seconds')}s)")
    return c


def check_secrets(proposals: list[dict]) -> Check:
    c = Check("no_secrets", True)
    for p in proposals:
        if SECRET_RE.search(json.dumps(p)):
            c.ok = False
            c.details.append(f"{p.get('target')}: proposal contains secret-like material")
    return c


# ---------------------------------------------------------------------------

def evaluate(engagement_id: str, outbox_dir: Path, attempt: int = 1, run_summary: dict | None = None,
             source_dir: Path | None = None, max_attempts: int = 2) -> GateResult:
    source_dir = Path(source_dir or config.SOURCE_DIR)
    proposals = load_proposals(outbox_dir)
    rp = reperform(source_dir, config.ENTITY, config.QUARTER, config.THRESHOLD)

    schema, workpaper, findings = check_schema(engagement_id, proposals)
    checks = [
        schema,
        check_evidence(proposals, workpaper, source_dir, config.ENTITY, config.QUARTER),
        check_sample(workpaper, rp.population),
        check_reperformance(workpaper, findings, rp),
        check_consistency(workpaper, rp),
        check_budget(run_summary),
        check_secrets(proposals),
    ]
    reason = ""
    if all(c.ok for c in checks):
        if workpaper and workpaper.conclusion == "effective" and not workpaper.population_exceptions:
            outcome, reason = "escalated", "risk_tier: 'control effective' with zero exceptions needs auditor sign-off"
        else:
            outcome = "approved"
    elif attempt < max_attempts:
        outcome = "retry"
    else:
        outcome, reason = "escalated", "verification failed after retry"
    return GateResult(engagement_id, attempt, outcome, checks, proposals, reason)


def apply(result: GateResult, conn=None) -> list[str]:
    """Persist the gate result; approve+enqueue or escalate. Returns action ids enqueued."""
    conn = conn or db.connect()
    log = get_audit_log()
    for p in result.proposals:
        if "proposal_hash" in p and "engagement_id" in p and "action_type" in p and "target" in p:
            db.record_proposal(conn, p, result.attempt)
    db.record_gate_result(conn, result.engagement_id, result.attempt, result.outcome,
                          [c.as_dict() for c in result.checks])
    log.append("gate", "gate_result", result.summary())
    action_ids = []
    if result.outcome == "approved":
        for p in result.proposals:
            aid = db.approve_and_enqueue(conn, p, approver="gate")
            if aid:
                action_ids.append(aid)
                log.append("gate", "approved", {"engagement_id": result.engagement_id, "action_id": aid,
                                                "proposal_hash": p["proposal_hash"], "target": p["target"]})
    elif result.outcome == "escalated":
        reasons = [r for r in [result.escalation_reason] if r]
        reasons += [f"{c.name}: {d}" for c in result.failures for d in c.details]
        for p in result.proposals or [{}]:
            rid = db.escalate(conn, result.engagement_id, p.get("proposal_hash"), "gate", reasons)
            log.append("gate", "escalated", {"engagement_id": result.engagement_id, "review_id": rid,
                                             "proposal_hash": p.get("proposal_hash"), "reasons": reasons[:10]})
    return action_ids


def feedback_for_retry(result: GateResult) -> str:
    lines = [f"- {c.name}: {d}" for c in result.failures for d in c.details[:15]]
    return "The verification gate rejected the previous attempt for these reasons:\n" + "\n".join(lines)

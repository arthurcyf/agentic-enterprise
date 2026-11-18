"""Proposal and workpaper schemas. The outbox tool and the gate both use them."""

# How this file fits in
# ---------------------
# Used by:  runner.outbox_tool validates a proposal when the agent calls propose_action (fast feedback
#           to the agent), and gate.gate validates it AGAIN outside the sandbox (the gate never trusts the
#           sandbox's check). The shapes also appear, in prose, in templates/CLAUDE.md for the agents.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal["file_workpaper", "raise_finding", "notify_auditor"]
ExceptionType = Literal["no_po", "sod", "split"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(Strict):
    table: str
    row_id: str
    row_hash: str
    values: dict[str, str] | None = None  # optional copy of cited fields; checked if present


class PopulationSpec(Strict):
    definition: str
    entity: str
    quarter: str
    min_amount: float
    size: int


class SampleSpec(Strict):
    method: str
    seed: int
    size: int
    ids: list[str]


class SampleResult(Strict):
    payment_id: str
    row_hash: str
    three_way_match: Literal["pass", "fail"]
    sod: Literal["pass", "fail"]


class ExceptionItem(Strict):
    type: ExceptionType
    payment_ids: list[str]


class Workpaper(Strict):
    control_id: str
    population: PopulationSpec
    sample: SampleSpec
    sample_results: list[SampleResult]
    population_exceptions: list[ExceptionItem]
    conclusion: Literal["effective", "not_effective"]
    narrative: str = ""


class Finding(Strict):
    finding_id: str
    type: ExceptionType
    payment_ids: list[str] = Field(min_length=1)
    description: str
    severity: Literal["low", "medium", "high"] = "medium"


class Notification(Strict):
    message: str


class Proposal(Strict):
    engagement_id: str
    action_type: ActionType
    target: str
    payload: dict
    evidence_refs: list[EvidenceRef] = []
    proposed_by: str = "lead"
    proposal_hash: str = ""


PAYLOAD_MODEL = {
    "file_workpaper": Workpaper,
    "raise_finding": Finding,
    "notify_auditor": Notification,
}

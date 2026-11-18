"""Gate tests driven by the scripted agent (deterministic, no LLM)."""
import asyncio
import json

import pytest

from audit.common import config
from audit.common.canon import proposal_hash
from audit.gate import gate as G
from audit.gate.reperform import Reperformance
from audit.orchestrator.local import local_scripted_runner
from audit.runner.outbox_tool import OutboxWriter
from audit.runner import scripted


def run_script(tmp_path, script, eng="ENG-GATE"):
    d = tmp_path / script
    local_scripted_runner(eng, d, "scripted", script)
    return d / "outbox"


def failing(result):
    return {c.name for c in result.checks if not c.ok}


def test_clean_work_is_approved(dataset, tmp_path):
    r = G.evaluate("ENG-GATE", run_script(tmp_path, "clean"))
    assert failing(r) == set(), [c.as_dict() for c in r.checks if not c.ok]
    assert r.outcome == "approved"
    assert len(r.proposals) == 1 + 6 + 1   # workpaper, 6 findings, notification


def test_corrupted_work_caught_retry_then_escalate(dataset, tmp_path):
    outbox = run_script(tmp_path, "corrupted")
    first = G.evaluate("ENG-GATE", outbox, attempt=1)
    assert first.outcome == "retry"
    assert {"evidence_integrity", "reperformance"} <= failing(first)
    details = " ".join(d for c in first.failures for d in c.details)
    assert "row_hash does not match source" in details
    assert "amount cited as '9999.00'" in details
    assert "re-performance says" in details
    second = G.evaluate("ENG-GATE", outbox, attempt=2)
    assert second.outcome == "escalated"


def test_injected_conclusion_fails_reperformance(dataset, tmp_path):
    r = G.evaluate("ENG-GATE", run_script(tmp_path, "injected"), attempt=2)
    assert {"reperformance", "conclusion_consistency"} <= failing(r)
    assert r.outcome == "escalated"
    assert any("missed exception split" in d for c in r.failures for d in c.details)


def _write(tmp_path, mutate):
    """Run the clean script, then rewrite proposals via `mutate(list) -> list` (rehashing them)."""
    outbox = run_script(tmp_path, "clean")
    files = sorted(outbox.glob("*.json"))
    props = [json.loads(f.read_text()) for f in files]
    for f in files:
        f.unlink()
    for i, p in enumerate(mutate(props), start=1):
        p["proposal_hash"] = proposal_hash(p)
        (outbox / f"{i:03d}.json").write_text(json.dumps(p))
    return outbox


def test_sample_drawn_with_other_seed_fails(dataset, tmp_path):
    def mutate(props):
        wp = props[0]["payload"]
        wp["sample"]["seed"] = 12345   # claims a seed that does not produce these ids
        return props
    r = G.evaluate("ENG-GATE", _write(tmp_path, mutate))
    assert "sample_integrity" in failing(r)


def test_sample_below_methodology_minimum_fails(dataset, tmp_path):
    def mutate(props):
        wp = props[0]["payload"]
        ids = wp["sample"]["ids"][:10]
        wp["sample"].update(size=10, ids=ids)
        wp["sample_results"] = [s for s in wp["sample_results"] if s["payment_id"] in ids]
        return props
    r = G.evaluate("ENG-GATE", _write(tmp_path, mutate))
    assert any("below methodology minimum" in d for c in r.failures for d in c.details)


def test_finding_without_evidence_fails(dataset, tmp_path):
    def mutate(props):
        props[1]["evidence_refs"] = []
        return props
    r = G.evaluate("ENG-GATE", _write(tmp_path, mutate))
    assert {"schema", "evidence_integrity"} <= failing(r)


def test_dropped_finding_is_missed(dataset, tmp_path):
    r = G.evaluate("ENG-GATE", _write(tmp_path, lambda props: [p for i, p in enumerate(props) if i != 3]))
    assert any("findings: missed exception" in d for c in r.failures for d in c.details)


def test_out_of_scope_evidence_fails(dataset, tmp_path):
    from audit.data.generate import load_table
    ent02 = next(p for p in load_table("payments") if p["entity"] == "ENT02")
    from audit.common.canon import row_hash

    def mutate(props):
        props[1]["evidence_refs"].append({"table": "payments", "row_id": ent02["payment_id"],
                                          "row_hash": row_hash("payments", ent02)})
        return props
    r = G.evaluate("ENG-GATE", _write(tmp_path, mutate))
    assert any("outside the engagement scope" in d for c in r.failures for d in c.details)


def test_proposal_modified_after_hashing_fails(dataset, tmp_path):
    outbox = run_script(tmp_path, "clean")
    f = sorted(outbox.glob("*.json"))[-1]
    p = json.loads(f.read_text())
    p["payload"]["message"] = "edited after the fact"
    f.write_text(json.dumps(p))
    r = G.evaluate("ENG-GATE", outbox)
    assert any("proposal_hash does not match" in d for c in r.failures for d in c.details)


def test_secret_in_proposal_fails(dataset, tmp_path):
    def mutate(props):
        props[-1]["payload"]["message"] = "key is sk-ant-api03-abcdef"
        return props
    assert "no_secrets" in failing(G.evaluate("ENG-GATE", _write(tmp_path, mutate)))


def test_budget_breach_fails(dataset, tmp_path):
    r = G.evaluate("ENG-GATE", run_script(tmp_path, "clean"),
                   run_summary={"subtype": "error_max_budget_usd", "num_turns": 50, "total_cost_usd": 3.1})
    assert "budget" in failing(r) and r.outcome == "retry"


def test_effective_with_zero_exceptions_escalates_to_human(dataset, tmp_path, monkeypatch):
    """Risk tier: even fully verified, a clean 'effective' conclusion needs auditor sign-off."""
    real = G.reperform

    def no_exceptions(*a, **kw):
        rp = real(*a, **kw)
        return Reperformance(rp.population, {k: {"three_way_match": "pass", "sod": "pass"} for k in rp.item_results},
                             set(), rp.payments)
    monkeypatch.setattr(G, "reperform", no_exceptions)

    def mutate(props):
        wp = props[0]["payload"]
        for s in wp["sample_results"]:
            s.update(three_way_match="pass", sod="pass")
        wp.update(population_exceptions=[], conclusion="effective")
        return [props[0], props[-1]]
    r = G.evaluate("ENG-GATE", _write(tmp_path, mutate))
    assert failing(r) == set()
    assert r.outcome == "escalated" and r.escalation_reason.startswith("risk_tier")


def test_outbox_writer_rejects_invalid_and_out_of_scratch_files(tmp_path):
    w = OutboxWriter(tmp_path / "outbox", "ENG-X", scratch_dir=tmp_path / "scratch")
    (tmp_path / "scratch").mkdir()
    assert not w.propose({"action_type": "delete_everything", "target": "x", "payload": {}})["ok"]
    assert not w.propose({"action_type": "raise_finding", "target": "f", "payload": {"finding_id": "F"}})["ok"]
    (tmp_path / "secret.json").write_text("{}")
    res = w.propose({"action_type": "notify_auditor", "target": "a", "payload_file": str(tmp_path / "secret.json")})
    assert not res["ok"] and "not under the scratch" in res["error"]
    (tmp_path / "scratch" / "m.json").write_text(json.dumps({"message": "hi"}))
    assert w.propose({"action_type": "notify_auditor", "target": "a",
                      "payload_file": str(tmp_path / "scratch" / "m.json")})["ok"]

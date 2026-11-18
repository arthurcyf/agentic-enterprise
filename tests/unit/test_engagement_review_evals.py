"""Full engagement path without Docker or an LLM: scripted agent -> broker logic -> outbox -> gate
-> transactional outbox -> review queue -> relay -> executor; plus eval metrics."""

# How this file fits in
# ---------------------
# Covers:   the whole host path without Docker: orchestrator.engagement (with orchestrator.local) ->
#           gate -> outbox.db -> review.cli -> outbox.relay -> executor.worker -> audit log; evals.
import json

import pytest

from audit.common import config
from audit.evals.metrics import pass_hat_k, recall_precision
from audit.orchestrator.engagement import run_engagement
from audit.orchestrator.local import local_scripted_runner
from audit.outbox import db
from audit.review import cli as review


def run(script):
    return run_engagement(mode="scripted", script=script, runner=local_scripted_runner)


def test_clean_engagement_is_approved_and_enqueued(dataset):
    r = run("clean")
    assert [g.outcome for g in r.gate_results] == ["approved"]
    assert len(r.action_ids) == 8
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM approvals WHERE engagement_id = ?", (r.engagement_id,)).fetchone()[0] == 8


def test_corrupted_engagement_retried_once_with_feedback_then_escalated(dataset):
    r = run("corrupted")
    assert [g.outcome for g in r.gate_results] == ["retry", "escalated"]
    feedback = (r.attempts[1].dir / "input" / "gate_feedback.md").read_text(encoding="utf-8")
    assert "evidence_integrity" in feedback and "reperformance" in feedback
    assert not r.action_ids
    items = [i for i in review.list_items(db.connect()) if i["engagement_id"] == r.engagement_id]
    assert items and all(i["source"] == "gate" for i in items)


def test_review_show_reject_and_approve(dataset):
    r = run("corrupted")
    conn = db.connect()
    items = [i for i in review.list_items(conn) if i["engagement_id"] == r.engagement_id]
    finding_item = next(i for i in items if i["action"].startswith("raise_finding finding:F-01"))
    text = review.show(conn, finding_item["id"])
    assert "DOES NOT MATCH" in text and "cited:  {'amount': '9999.00'}" in text
    before = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    assert review.decide(conn, finding_item["id"], approve=False, by="jane") == "rejected"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == before
    notify_item = next(i for i in items if i["action"].startswith("notify_auditor"))
    assert review.decide(conn, notify_item["id"], approve=True, by="jane").startswith("approved; enqueued")
    assert conn.execute("SELECT approver FROM approvals ORDER BY id DESC LIMIT 1").fetchone()[0] == "human:jane"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == before + 1
    assert review.decide(conn, notify_item["id"], approve=True, by="jane").endswith("is not open")


def test_end_to_end_relay_and_executor_files_once(dataset):
    from audit.executor.worker import Executor, connect_ledger
    from audit.outbox.relay import connect_queue, publish
    r = run("clean")
    state, queue, ledger = db.connect(), connect_queue(), connect_ledger()
    publish(state, queue)
    first = Executor(state, queue, ledger).run()
    assert first["executed"] >= 8
    queue.execute("UPDATE queue SET status = 'pending'")
    again = Executor(state, queue, ledger).run()
    assert again["executed"] == 0 and again["skipped_duplicate"] >= 8
    wp = config.MOCK_TARGETS / "repo" / r.engagement_id / "workpaper_AP-01.json"
    assert json.loads(wp.read_text())["payload"]["conclusion"] == "not_effective"


def test_audit_log_verifies_after_everything(dataset):
    from audit.auditlog.chain import verify
    run("clean")
    ok, n, msg = verify(config.AUDIT_LOG)
    assert ok, msg
    types = {json.loads(l)["type"] for l in config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()}
    assert {"engagement_started", "gate_result", "approved", "run_result"} <= types


def test_metrics():
    exp = [{"type": "sod", "payment_ids": ["P1"]}, {"type": "split", "payment_ids": ["P3", "P2"]}]
    got = [{"type": "sod", "payment_ids": ["P1"]}, {"type": "split", "payment_ids": ["P2", "P3"]},
           {"type": "no_po", "payment_ids": ["P9"]}]
    m = recall_precision(got, exp)
    assert (m["recall"], round(m["precision"], 3)) == (1.0, 0.667)
    assert pass_hat_k(5, 5, 3) == 1.0 and pass_hat_k(5, 0, 1) == 0.0
    assert pass_hat_k(4, 2, 2) == pytest.approx(1 / 6)


def test_eval_harness_scripted(dataset):
    from audit.evals.run import run_evals
    rep = run_evals(n=2, k=2, mode="scripted", runner=local_scripted_runner)
    assert rep["mean_recall"] == 1.0 and rep["mean_precision"] == 1.0
    assert rep["pass^2"] == 1.0 and rep["corrupted_control_escalated"]
    assert rep["escalation_correctness"] == 1.0

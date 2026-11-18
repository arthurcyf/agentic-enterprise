"""Transactional outbox, relay, executor, idempotency ledger. Deterministic, no LLM."""

# How this file fits in
# ---------------------
# Covers:   outbox.db (atomic approval + outbox), outbox.relay (re-run, crash), executor.worker
#           (idempotency ledger, hash check, retry/backoff, escalation), executor.handlers credentials.
import json

import pytest

from audit.common.canon import proposal_hash
from audit.executor import handlers as H
from audit.executor.worker import Executor, connect_ledger
from audit.outbox import db
from audit.outbox.relay import connect_queue, publish


def make_proposal(target="finding:F-01", eng="ENG-EX"):
    p = {"engagement_id": eng, "action_type": "raise_finding", "target": target,
         "payload": {"finding_id": target.split(":")[1], "type": "sod", "payment_ids": ["P1"],
                     "description": "d", "severity": "high"},
         "evidence_refs": [{"table": "payments", "row_id": "P1", "row_hash": "h"}], "proposed_by": "t"}
    p["proposal_hash"] = proposal_hash(p)
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("FAIL_N", raising=False)
    creds, targets = tmp_path / "creds", tmp_path / "targets"
    H.provision_credentials(creds, targets)
    state, queue, ledger = db.connect(tmp_path / "s.db"), connect_queue(tmp_path / "q.db"), connect_ledger(tmp_path / "l.db")
    sleeps = []
    ex = Executor(state, queue, ledger, handler_kwargs={"creds_dir": creds, "targets_dir": targets},
                  sleep=sleeps.append)
    return {"state": state, "queue": queue, "ledger": ledger, "ex": ex, "sleeps": sleeps,
            "creds": creds, "targets": targets}


def issues(env):
    import sqlite3
    conn = sqlite3.connect(env["targets"] / "tracker" / "issues.db")
    return conn.execute("SELECT finding_id FROM issues").fetchall()


def test_approval_and_outbox_rows_are_atomic(state):
    p = make_proposal()
    with pytest.raises(RuntimeError):
        db.approve_and_enqueue(state, p, "gate", _fail_between=True)
    assert state.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert db.approve_and_enqueue(state, p, "gate")
    assert state.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_double_approval_does_not_enqueue_twice(state):
    p = make_proposal()
    assert db.approve_and_enqueue(state, p, "gate")
    assert db.approve_and_enqueue(state, p, "human:x") is None
    assert state.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_relay_rerun_and_crash_do_not_duplicate(env):
    db.approve_and_enqueue(env["state"], make_proposal(), "gate")
    with pytest.raises(RuntimeError):
        publish(env["state"], env["queue"], _crash_before_mark=True)   # published, not marked
    assert publish(env["state"], env["queue"]) == 0                    # re-publish: ignored duplicate
    assert publish(env["state"], env["queue"]) == 0
    assert env["queue"].execute("SELECT COUNT(*) FROM queue").fetchone()[0] == 1


def test_executor_files_once_even_when_redelivered(env):
    db.approve_and_enqueue(env["state"], make_proposal(), "gate")
    publish(env["state"], env["queue"])
    assert env["ex"].run()["executed"] == 1
    assert env["ex"].run()["executed"] == 0
    env["queue"].execute("UPDATE queue SET status = 'pending'")        # duplicate delivery
    stats = env["ex"].run()
    assert stats["executed"] == 0 and stats["skipped_duplicate"] == 1
    assert len(issues(env)) == 1


def test_same_target_from_two_approvals_files_once(env):
    """Idempotency key = (engagement, action type, target): a second proposal for the same target is a no-op."""
    p1, p2 = make_proposal(), make_proposal()
    p2["payload"]["description"] = "different wording"
    p2["proposal_hash"] = proposal_hash(p2)
    db.approve_and_enqueue(env["state"], p1, "gate")
    db.approve_and_enqueue(env["state"], p2, "gate")
    publish(env["state"], env["queue"])
    stats = env["ex"].run()
    assert stats == {"executed": 1, "skipped_duplicate": 1, "rejected": 0, "escalated": 0}


def test_tampered_queue_payload_is_rejected(env):
    db.approve_and_enqueue(env["state"], make_proposal(), "gate")
    publish(env["state"], env["queue"])
    row = env["queue"].execute("SELECT payload_json FROM queue").fetchone()
    tampered = json.loads(row[0])
    tampered["payload"]["payment_ids"] = ["P999"]
    env["queue"].execute("UPDATE queue SET payload_json = ?", (json.dumps(tampered),))
    assert env["ex"].run()["rejected"] == 1
    assert not (env["targets"] / "tracker" / "issues.db").exists()
    assert env["state"].execute("SELECT COUNT(*) FROM review_queue WHERE source='executor'").fetchone()[0] == 1


def test_transient_failures_retry_with_backoff(env, monkeypatch):
    monkeypatch.setenv("FAIL_N", "2")
    env["ex"]._handlers.clear()
    db.approve_and_enqueue(env["state"], make_proposal(), "gate")
    publish(env["state"], env["queue"])
    assert env["ex"].run()["executed"] == 1
    assert env["sleeps"] == [0.5, 1.0]


def test_three_failures_escalate(env, monkeypatch):
    monkeypatch.setenv("FAIL_N", "5")
    env["ex"]._handlers.clear()
    db.approve_and_enqueue(env["state"], make_proposal(), "gate")
    publish(env["state"], env["queue"])
    assert env["ex"].run()["escalated"] == 1
    assert env["ledger"].execute("SELECT status, attempts FROM ledger").fetchone()[:] == ("failed", 3)
    assert env["state"].execute("SELECT COUNT(*) FROM review_queue WHERE source='executor'").fetchone()[0] == 1


def test_each_handler_uses_only_its_own_credential(env):
    # Swap the tracker's credential for the repo's: the mock tracker must refuse it.
    (env["creds"] / "tracker.token").write_text((env["creds"] / "repo.token").read_text())
    h = H.TrackerHandler(env["creds"], env["targets"])
    with pytest.raises(H.AuthError):
        h.execute({"engagement_id": "E", "payload": {}}, "k")

"""Eval harness: run the full scenario N times and score it against the answer key.

Reports per run: recall/precision of the proposed findings (final attempt), gate
outcome, escalation correctness, cost and turns. Overall: mean recall/precision,
pass^k (a run "passes" when the gate approved it and its findings match the key
exactly), plus a scripted control run that must be escalated.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from statistics import mean

from audit.common import config
from audit.evals.metrics import by_type, pass_hat_k, recall_precision
from audit.orchestrator.engagement import run_engagement


def proposed_findings(gate_result) -> list[dict]:
    return [p["payload"] for p in gate_result.proposals
            if p.get("action_type") == "raise_finding" and "payload" in p
            and {"type", "payment_ids"} <= set(p["payload"])]


def conclusion(gate_result) -> str | None:
    for p in gate_result.proposals:
        if p.get("action_type") == "file_workpaper":
            return p.get("payload", {}).get("conclusion")
    return None


def score_run(result, key: dict) -> dict:
    g = result.final
    found = proposed_findings(g)
    rp = recall_precision(found, key["expected_findings"])
    exact = rp["fp"] == 0 and rp["fn"] == 0
    cost = sum((a.summary.get("total_cost_usd") or 0.0) for a in result.attempts)
    turns = sum((a.summary.get("num_turns") or 0) for a in result.attempts)
    # The key's control is NOT effective, so an "effective/zero exceptions" escalation would be wrong;
    # an approved run is correct only if it matches the key.
    escalation_correct = (g.outcome == "approved" and exact) or (g.outcome == "escalated" and not exact)
    return {
        "engagement_id": result.engagement_id, "attempts": len(result.attempts), "gate_outcome": g.outcome,
        "conclusion": conclusion(g), "recall": rp["recall"], "precision": rp["precision"],
        "missed": rp["missed"], "spurious": rp["spurious"], "by_type": by_type(found, key["expected_findings"]),
        "passed": g.outcome == "approved" and exact, "escalation_correct": escalation_correct,
        "cost_usd": round(cost, 4), "turns": turns,
    }


def run_evals(n: int = 3, k: int = 2, mode: str = "live", budget: float = 3.0, timeout: int = 1800,
              runner=None) -> dict:
    key = json.loads(config.ANSWER_KEY.read_text(encoding="utf-8"))
    runs = []
    for i in range(n):
        print(f"[eval] run {i + 1}/{n} ({mode})", flush=True)
        r = run_engagement(mode=mode, script="clean", timeout_s=timeout, max_budget_usd=budget, runner=runner)
        runs.append(score_run(r, key))
        s = runs[-1]
        print(f"       gate={s['gate_outcome']} recall={s['recall']:.2f} precision={s['precision']:.2f} "
              f"cost=${s['cost_usd']} turns={s['turns']}", flush=True)

    print("[eval] control run: scripted 'corrupted' must be escalated", flush=True)
    ctrl = run_engagement(mode="scripted", script="corrupted", runner=runner)
    control_ok = ctrl.final.outcome == "escalated"

    c = sum(r["passed"] for r in runs)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "mode": mode, "n": n, "k": k,
        "mean_recall": mean(r["recall"] for r in runs), "mean_precision": mean(r["precision"] for r in runs),
        "passes": c, f"pass^{k}": pass_hat_k(n, c, min(k, n)),
        "escalation_correctness": (sum(r["escalation_correct"] for r in runs) + control_ok) / (n + 1),
        "corrupted_control_escalated": control_ok,
        "mean_cost_usd": mean(r["cost_usd"] for r in runs), "mean_turns": mean(r["turns"] for r in runs),
        "runs": runs,
    }
    config.EVALS.mkdir(parents=True, exist_ok=True)
    out = config.EVALS / f"eval-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== Eval report ===")
    print(f"runs: {n}   passes: {c}   pass^{k}: {report[f'pass^{k}']:.2f}")
    print(f"mean recall: {report['mean_recall']:.2f}   mean precision: {report['mean_precision']:.2f}")
    print(f"escalation correctness: {report['escalation_correctness']:.2f} "
          f"(corrupted control escalated: {control_ok})")
    print(f"mean cost per run: ${report['mean_cost_usd']:.4f}   mean turns per run: {report['mean_turns']:.1f}")
    print(f"report: {out}")
    return report

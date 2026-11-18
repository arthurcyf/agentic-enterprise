"""Eval metrics against the hidden answer key."""

# How this file fits in
# ---------------------
# Used by:  evals.run and demo (recall_precision). Pure functions, no I/O.
from __future__ import annotations

from math import comb


def finding_set(findings: list[dict]) -> set[tuple[str, tuple[str, ...]]]:
    return {(f["type"], tuple(sorted(f["payment_ids"]))) for f in findings}


def recall_precision(predicted: list[dict], expected: list[dict]) -> dict:
    p, e = finding_set(predicted), finding_set(expected)
    tp = len(p & e)
    return {
        "tp": tp, "fp": len(p - e), "fn": len(e - p),
        "recall": tp / len(e) if e else 1.0,
        "precision": tp / len(p) if p else (1.0 if not e else 0.0),
        "missed": sorted(e - p), "spurious": sorted(p - e),
    }


def by_type(predicted: list[dict], expected: list[dict]) -> dict:
    types = sorted({f["type"] for f in expected} | {f["type"] for f in predicted})
    return {t: recall_precision([f for f in predicted if f["type"] == t],
                                [f for f in expected if f["type"] == t]) for t in types}


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Probability that k runs drawn from n all pass, given c of the n passed (unbiased estimator)."""
    if k > n:
        raise ValueError("k must be <= n")
    return comb(c, k) / comb(n, k)

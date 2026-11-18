"""The documented sampling method (also written in CLAUDE.md for the agents).

    population_ids = sorted(ids)
    sample_ids     = random.Random(seed).sample(population_ids, n)

The gate redraws with the agent's stated seed and size and requires an
identical, identically ordered list.
"""
from __future__ import annotations

import random

METHOD = "random.Random(seed).sample(sorted(population_ids), n)"

# Minimum sample sizes (must match the table in CLAUDE.md).
MIN_SAMPLE = {"high": 40, "medium": 25, "low": 15}
CONTROL_RISK = {"AP-01": "high"}


def draw(population_ids: list[str], seed: int, n: int) -> list[str]:
    return random.Random(seed).sample(sorted(population_ids), n)


def minimum_for(control_id: str, population_size: int) -> int:
    return min(MIN_SAMPLE[CONTROL_RISK.get(control_id, "high")], population_size)

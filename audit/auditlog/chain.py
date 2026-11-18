"""Append-only JSONL audit log with a hash chain.

Each entry stores the previous entry's hash, and its own hash covers its
content plus that link. Editing, deleting or reordering any line breaks the
chain from that point, and `verify` reports the first bad sequence number.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.common.canon import canon, sha256_hex

GENESIS = "0" * 64

# Anything that looks like a secret is replaced before it is written.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(x-api-key|x-erp-key|x-case-token|authorization)([\"']?\s*[:=]\s*[\"']?)"
               r"(?:bearer\s+|basic\s+)?[^\s\"',}]+"),
]


_SECRET_KEYS = {"x-api-key", "x-erp-key", "x-case-token", "authorization", "api_key", "case_token"}


def redact(value: Any, extra_secrets: list[str] | None = None) -> Any:
    secrets = [s for s in (extra_secrets or []) if s and len(s) >= 6]
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, "[REDACTED]")
        value = _SECRET_PATTERNS[0].sub("[REDACTED]", value)
        value = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {k: "[REDACTED]" if str(k).lower() in _SECRET_KEYS else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return sha256_hex(canon(body))


class HashChainLog:
    """Single-writer append-only log. One process owns the file."""

    def __init__(self, path: Path, extra_secrets: list[str] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._secrets = extra_secrets or []
        self._seq, self._prev = self._tail()

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        last = None
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = line
        entry = json.loads(last)
        return entry["seq"], entry["hash"]

    def append(self, actor: str, type_: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry = {
                "seq": self._seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "type": type_,
                "data": redact(data, self._secrets),
                "prev_hash": self._prev,
            }
            entry["hash"] = entry_hash(entry)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(canon(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seq, self._prev = entry["seq"], entry["hash"]
            return entry


def verify(path: Path) -> tuple[bool, int, str]:
    """Return (ok, entries_checked, message)."""
    path = Path(path)
    if not path.exists():
        return True, 0, "empty log"
    prev, expected_seq, n = GENESIS, 1, 0
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, n, f"line {lineno}: not valid JSON"
            seq = entry.get("seq")
            if seq != expected_seq:
                return False, n, f"line {lineno}: expected seq {expected_seq}, found {seq} (deleted or reordered entry)"
            if entry.get("prev_hash") != prev:
                return False, n, f"seq {seq}: prev_hash does not match previous entry"
            if entry_hash(entry) != entry.get("hash"):
                return False, n, f"seq {seq}: content hash mismatch (entry was modified)"
            prev, expected_seq, n = entry["hash"], expected_seq + 1, n + 1
    return True, n, f"OK ({n} entries)"

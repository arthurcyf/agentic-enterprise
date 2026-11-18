"""One handler per action type, each with its own credential.

The mock targets check the credential, so a handler cannot act on a system it
was not given access to. None of these credentials is the broker's ERP key.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path

from audit.common import config

TARGETS = ("repo", "tracker", "slack")


class TransientError(Exception):
    """A temporary failure worth retrying (simulated with FAIL_N=<n>)."""


class AuthError(Exception):
    pass


def provision_credentials(creds_dir: Path | None = None, targets_dir: Path | None = None) -> None:
    """Create one credential per handler and register it with its mock target."""
    creds_dir, targets_dir = Path(creds_dir or config.CREDS), Path(targets_dir or config.MOCK_TARGETS)
    for t in TARGETS:
        (targets_dir / t).mkdir(parents=True, exist_ok=True)
        cred = creds_dir / f"{t}.token"
        if not cred.exists():
            creds_dir.mkdir(parents=True, exist_ok=True)
            cred.write_text(secrets.token_hex(16), encoding="utf-8")
        (targets_dir / t / ".expected_token").write_text(cred.read_text(encoding="utf-8"), encoding="utf-8")


class _FailureInjector:
    """FAIL_N=2 makes the next two calls (per process) raise TransientError."""

    def __init__(self):
        self.remaining = int(os.environ.get("FAIL_N", "0"))

    def maybe_fail(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1
            raise TransientError("simulated temporary outage")


class Handler:
    target = ""

    def __init__(self, creds_dir: Path | None = None, targets_dir: Path | None = None, injector=None):
        self.creds_dir = Path(creds_dir or config.CREDS)
        self.root = Path(targets_dir or config.MOCK_TARGETS) / self.target
        self.injector = injector or _FailureInjector()

    def _authenticate(self) -> None:
        token = (self.creds_dir / f"{self.target}.token").read_text(encoding="utf-8")
        expected = (self.root / ".expected_token").read_text(encoding="utf-8")
        if not hmac.compare_digest(token, expected):
            raise AuthError(f"credential rejected by mock {self.target}")

    def execute(self, action: dict, idem_key: str) -> str:
        self.injector.maybe_fail()
        self._authenticate()
        return self._do(action, idem_key)

    def _do(self, action: dict, idem_key: str) -> str:
        raise NotImplementedError


class RepoHandler(Handler):
    """file_workpaper -> mock audit repository (a folder of JSON documents)."""
    target = "repo"

    def _do(self, action, idem_key):
        path = self.root / action["engagement_id"] / f"{action['target'].replace(':', '_')}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(action, indent=2), encoding="utf-8")
        return str(path)


class TrackerHandler(Handler):
    """raise_finding -> mock issue tracker (SQLite)."""
    target = "tracker"

    def _do(self, action, idem_key):
        conn = sqlite3.connect(self.root / "issues.db")
        conn.execute("CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, engagement_id TEXT, "
                     "finding_id TEXT, type TEXT, payment_ids TEXT, description TEXT, severity TEXT)")
        p = action["payload"]
        cur = conn.execute("INSERT INTO issues (engagement_id, finding_id, type, payment_ids, description, severity) "
                           "VALUES (?,?,?,?,?,?)", (action["engagement_id"], p["finding_id"], p["type"],
                                                    ",".join(p["payment_ids"]), p["description"], p.get("severity")))
        conn.commit()
        conn.close()
        return f"issue #{cur.lastrowid}"


class SlackHandler(Handler):
    """notify_auditor -> mock Slack channel (an append-only log file)."""
    target = "slack"

    def _do(self, action, idem_key):
        with (self.root / "slack.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"channel": "#audit", "engagement_id": action["engagement_id"],
                                "text": action["payload"]["message"]}) + "\n")
        return "slack:#audit"


HANDLERS = {"file_workpaper": RepoHandler, "raise_finding": TrackerHandler, "notify_auditor": SlackHandler}

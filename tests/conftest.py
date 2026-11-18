"""Test setup: every test session gets its own throwaway var/ directory.

AUDIT_VAR must be set before any audit module is imported, because
audit.common.config computes its paths at import time.
"""

# How this file fits in
# ---------------------
# Imported by pytest before every test module. Points AUDIT_VAR at a temp folder before any audit
# module (and so common.config) is imported, and provides the shared `dataset` fixture.
import os
import tempfile

_SESSION_VAR = tempfile.mkdtemp(prefix="audit-test-")
os.environ["AUDIT_VAR"] = _SESSION_VAR
os.environ.pop("AUDITLOG_URL", None)
os.environ.setdefault("CASE_TOKEN_SECRET", "test-secret")

import pytest  # noqa: E402

from audit.common import config  # noqa: E402


@pytest.fixture(scope="session")
def dataset():
    """Generate the seeded dataset once per session."""
    from audit.data.generate import generate
    from audit.executor.handlers import provision_credentials

    config.ensure_dirs()
    key = generate(seed=42)
    provision_credentials()
    return key


@pytest.fixture
def state(tmp_path):
    from audit.outbox import db
    return db.connect(tmp_path / "state.db")

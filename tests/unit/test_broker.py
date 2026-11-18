import asyncio
import json
import socket
import threading
import time

import pytest

from audit.broker.core import Broker, ScopeError
from audit.common.canon import row_hash
from audit.common.token import CaseScope, mint
from audit.orchestrator.local import local_erp_fetch

SECRET = "broker-test-secret"
ERP_KEY = "erp-super-secret-key-123"


class ListLog:
    def __init__(self):
        self.entries = []

    def append(self, actor, type_, data):
        self.entries.append({"actor": actor, "type": type_, "data": data})


@pytest.fixture
def broker(dataset, monkeypatch):
    monkeypatch.setenv("ERP_API_KEY", ERP_KEY)
    return Broker(SECRET, erp_fetch=local_erp_fetch, audit=ListLog())


def tok(**kw):
    scope = dict(engagement_id="ENG-T", entity="ENT01", quarter="2026Q3") | kw
    return mint(CaseScope(**scope), SECRET)


def test_extract_in_scope_rows_only(broker):
    rows = broker.extract(tok(), "payments", limit=200)
    assert rows["total_rows"] > 500
    assert all(r["entity"] == "ENT01" and r["quarter"] == "2026Q3" for r in rows["rows"])


def test_rows_carry_ids_and_verifiable_hashes(broker):
    r = broker.extract(tok(), "approvals", limit=5)["rows"][0]
    assert r["_row_id"] == r["approval_id"]
    assert r["_row_hash"] == row_hash("approvals", r)


def test_non_allowlisted_fields_are_stripped(broker):
    rows = broker.extract(tok(), "users_and_roles")["rows"]
    assert rows and all("email" not in r for r in rows)


@pytest.mark.parametrize("kwargs, msg", [
    ({"entity": "ENT02"}, "entity"),
    ({"quarter": "2026Q2"}, "quarter"),
])
def test_out_of_scope_requests_denied(broker, kwargs, msg):
    with pytest.raises(ScopeError, match=msg):
        broker.extract(tok(), "payments", **kwargs)


def test_table_outside_token_scope_denied(broker):
    narrow = mint(CaseScope("ENG-T", "ENT01", "2026Q3", tables=("payments",)), SECRET)
    with pytest.raises(ScopeError, match="table"):
        broker.extract(narrow, "approvals")


def test_missing_or_forged_token_denied(broker):
    with pytest.raises(ScopeError):
        broker.extract(None, "payments")
    with pytest.raises(ScopeError):
        broker.extract(mint(CaseScope("ENG-T", "ENT01", "2026Q3"), "attacker-secret"), "payments")


def test_token_for_other_entity_gets_that_entity_only(broker):
    rows = broker.extract(tok(entity="ENT02"), "payments")["rows"]
    assert rows and all(r["entity"] == "ENT02" for r in rows)


def test_every_call_logged_and_erp_key_never_logged(broker):
    broker.extract(tok(), "payments", limit=3)
    with pytest.raises(ScopeError):
        broker.extract(tok(), "payments", entity="ENT02")
    decisions = [e["data"]["decision"] for e in broker.audit.entries]
    assert decisions == ["allow", "deny"]
    assert ERP_KEY not in json.dumps(broker.audit.entries)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_over_http_with_case_token_header(broker):
    """The broker as a real remote MCP server, called the way the runner calls it."""
    import uvicorn

    from audit.broker.server import build_app
    from audit.runner.broker_client import BrokerClient, BrokerError

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_app(broker), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    try:
        async def go():
            c = BrokerClient(f"http://127.0.0.1:{port}/mcp", tok())
            rows = await c.fetch_all("payments")
            assert len(rows) > 500
            with pytest.raises(BrokerError, match="entity"):
                await c.call("extract", {"table": "payments", "entity": "ENT02"})
            with pytest.raises(BrokerError, match="token"):
                await BrokerClient(f"http://127.0.0.1:{port}/mcp", "forged.token").call("list_tables", {})
        asyncio.run(go())
    finally:
        server.should_exit = True
        t.join(timeout=5)

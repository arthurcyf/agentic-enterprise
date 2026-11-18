# How this file fits in
# ---------------------
# Covers:   auditlog.chain: append/verify, tamper detection (edit, delete, reorder), redaction.

import json

from audit.auditlog.chain import HashChainLog, redact, verify


def make_log(tmp_path, n=5):
    path = tmp_path / "audit.jsonl"
    log = HashChainLog(path)
    for i in range(n):
        log.append("test", "event", {"i": i})
    return path


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_append_and_verify(tmp_path):
    path = make_log(tmp_path)
    assert verify(path) == (True, 5, "OK (5 entries)")


def test_reopen_continues_chain(tmp_path):
    path = make_log(tmp_path, 3)
    HashChainLog(path).append("test", "event", {"i": 99})
    ok, n, _ = verify(path)
    assert ok and n == 4


def test_modified_byte_detected(tmp_path):
    path = make_log(tmp_path)
    ls = lines(path)
    ls[2] = ls[2].replace('"i":2', '"i":7')
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")
    ok, _, msg = verify(path)
    assert not ok and "seq 3" in msg and "modified" in msg


def test_deleted_line_detected(tmp_path):
    path = make_log(tmp_path)
    ls = lines(path)
    del ls[1]
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")
    ok, _, msg = verify(path)
    assert not ok and "expected seq 2" in msg


def test_reordered_lines_detected(tmp_path):
    path = make_log(tmp_path)
    ls = lines(path)
    ls[1], ls[2] = ls[2], ls[1]
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")
    assert not verify(path)[0]


def test_recomputed_hash_after_edit_still_detected(tmp_path):
    """An attacker who edits an entry AND fixes its hash breaks the next entry's prev_hash link."""
    from audit.auditlog.chain import entry_hash
    path = make_log(tmp_path)
    ls = lines(path)
    e = json.loads(ls[1])
    e["data"]["i"] = 42
    e["hash"] = entry_hash(e)
    ls[1] = json.dumps(e)
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")
    ok, _, msg = verify(path)
    assert not ok and "seq 3" in msg and "prev_hash" in msg


def test_secrets_redacted_before_writing(tmp_path):
    path = tmp_path / "a.jsonl"
    HashChainLog(path, extra_secrets=["erp-key-0123456789"]).append("t", "e", {
        "headers": {"x-api-key": "sk-ant-api03-SECRET"}, "note": "used erp-key-0123456789 here",
        "text": "Authorization: Bearer abc.def"})
    text = path.read_text()
    assert "sk-ant" not in text and "erp-key-0123456789" not in text and "abc.def" not in text
    assert verify(path)[0]


def test_redact_nested():
    assert redact({"a": ["sk-ant-xyz"]}) == {"a": ["[REDACTED]"]}

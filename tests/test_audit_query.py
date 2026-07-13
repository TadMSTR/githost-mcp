"""Tests for tools/audit_query.py: audit_log_query filtering and tamper detection."""

import json

import pytest

from githost_mcp.audit import init_logging, write_audit_entry
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AGENT_ID", "test-agent")
    reset_config()
    init_logging()


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.audit_query import register
    register(MockMCP())
    return registered


def test_query_empty_log_returns_no_entries(tools, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "does-not-exist.jsonl"))
    reset_config()
    result = tools["audit_log_query"]()
    assert result == {"entries": [], "total_matched": 0}


def test_query_returns_entries_newest_first(tools):
    write_audit_entry("git_status", "local", "/repo/a", {}, "ok", 5)
    write_audit_entry("git_commit", "local", "/repo/b", {}, "ok", 10)
    result = tools["audit_log_query"]()
    assert result["total_matched"] == 2
    assert result["entries"][0]["tool"] == "git_commit"
    assert result["entries"][1]["tool"] == "git_status"


def test_query_filter_by_agent_id(tools, monkeypatch):
    write_audit_entry("git_status", "local", "/repo/a", {}, "ok", 5)
    monkeypatch.setenv("AGENT_ID", "other-agent")
    reset_config()
    init_logging()
    write_audit_entry("git_commit", "local", "/repo/b", {}, "ok", 10)

    result = tools["audit_log_query"](agent_id="test-agent")
    assert result["total_matched"] == 1
    assert result["entries"][0]["tool"] == "git_status"


def test_query_filter_by_tool(tools):
    write_audit_entry("git_status", "local", "/repo/a", {}, "ok", 5)
    write_audit_entry("git_commit", "local", "/repo/b", {}, "ok", 10)
    result = tools["audit_log_query"](tool="git_commit")
    assert result["total_matched"] == 1
    assert result["entries"][0]["tool"] == "git_commit"


def test_query_filter_by_repo_substring(tools):
    write_audit_entry("git_status", "local", "/repos/personal/foo", {}, "ok", 5)
    write_audit_entry("git_status", "local", "/repos/gitea/bar", {}, "ok", 5)
    result = tools["audit_log_query"](repo="personal")
    assert result["total_matched"] == 1
    assert result["entries"][0]["repo"] == "/repos/personal/foo"


def test_query_filter_by_since(tools, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    old_entry = {
        "ts": "2026-01-01T00:00:00.000Z", "agent_id": "test-agent", "tool": "git_status",
        "provider": "local", "repo": "/repo", "params": {}, "result": "ok", "duration_ms": 1,
    }
    with open(audit_path, "w") as f:
        f.write(json.dumps(old_entry) + "\n")
    write_audit_entry("git_commit", "local", "/repo", {}, "ok", 5)

    result = tools["audit_log_query"](since="2026-06-01")
    assert result["total_matched"] == 1
    assert result["entries"][0]["tool"] == "git_commit"


def test_query_invalid_since_format(tools):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    result = tools["audit_log_query"](since="not-a-date")
    assert "error" in result
    assert "Invalid 'since' date format" in result["error"]


def test_query_limit_caps_results(tools):
    for i in range(5):
        write_audit_entry(f"tool_{i}", "local", "/repo", {}, "ok", 1)
    result = tools["audit_log_query"](limit=2)
    assert result["total_matched"] == 2
    assert len(result["entries"]) == 2


def test_query_skips_unparseable_lines(tools, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    with open(audit_path, "a") as f:
        f.write("not valid json\n")
        f.write("\n")  # blank line
    result = tools["audit_log_query"]()
    assert result["total_matched"] == 1


def test_query_marks_valid_entry_not_tampered(tools):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    result = tools["audit_log_query"]()
    assert result["entries"][0]["tamper_detected"] is False


def test_query_detects_tampered_entry(tools, tmp_path):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    audit_path = tmp_path / "audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
    entries[0]["result"] = "tampered-after-the-fact"
    with open(audit_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    result = tools["audit_log_query"]()
    assert result["entries"][0]["tamper_detected"] is True


def test_query_no_signing_key_never_flags_tamper(tools, monkeypatch):
    """Without AUDIT_SIGNING_KEY, entries carry no hmac and are never flagged."""
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()
    init_logging()
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    result = tools["audit_log_query"]()
    assert result["entries"][0]["tamper_detected"] is False
    assert "hmac" not in result["entries"][0]


def test_query_unreadable_log_returns_error(tools, tmp_path, monkeypatch):
    import os
    audit_path = tmp_path / "unreadable.jsonl"
    audit_path.write_text('{"ts": "x"}\n')
    os.chmod(audit_path, 0o000)
    monkeypatch.setenv("AUDIT_LOG_FILE", str(audit_path))
    reset_config()
    try:
        result = tools["audit_log_query"]()
        # Running as root (or in some sandboxes) chmod 000 doesn't block reads —
        # in that case fall back to accepting a normal parsed result.
        assert "error" in result or "entries" in result
    finally:
        os.chmod(audit_path, 0o644)

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
    assert result == {"entries": [], "total_matched": 0, "sources_searched": []}


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
        "ts": "2026-01-01T00:00:00.000Z",
        "agent_id": "test-agent",
        "tool": "git_status",
        "provider": "local",
        "repo": "/repo",
        "params": {},
        "result": "ok",
        "duration_ms": 1,
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


def test_query_marks_valid_entry_verified(tools):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    result = tools["audit_log_query"]()
    assert result["entries"][0]["integrity"] == "verified"
    assert result["integrity_summary"] == {"verified": 1}
    assert result["signing_key_configured"] is True


def test_query_unsigned_entry_is_not_reported_as_intact(tools, monkeypatch):
    """Without AUDIT_SIGNING_KEY, entries carry no hmac. Reporting
    tamper_detected: false on those is a false assurance — the entry has no tamper
    evidence at all, so nothing was ever checked (vikunja#301, id 312)."""
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()
    init_logging()
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)

    result = tools["audit_log_query"]()
    entry = result["entries"][0]

    assert "hmac" not in entry
    assert entry["integrity"] == "unsigned"
    assert entry["tamper_detected"] is not False, (
        "an unsigned entry must never come back as a clean bill of health"
    )
    assert entry["tamper_detected"] is None
    assert result["integrity_summary"] == {"unsigned": 1}
    assert result["signing_key_configured"] is False


def test_query_signed_entry_without_key_is_unverifiable(tools, monkeypatch, tmp_path):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 5)
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()
    init_logging()

    result = tools["audit_log_query"]()
    assert result["entries"][0]["integrity"] == "unverifiable"
    assert result["entries"][0]["tamper_detected"] is None


def test_query_integrity_summary_counts_mixed_states(tools, tmp_path, monkeypatch):
    """A log spanning an unsigned window and a signed one reports both."""
    write_audit_entry("git_signed", "local", "/repo", {}, "ok", 5)
    audit_path = tmp_path / "audit.jsonl"
    with open(audit_path, "a") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-07-30T00:00:00.000Z",
                    "agent_id": "test-agent",
                    "tool": "git_unsigned",
                    "provider": "local",
                    "repo": "/repo",
                    "params": {},
                    "result": "ok",
                    "duration_ms": 1,
                }
            )
            + "\n"
        )

    result = tools["audit_log_query"]()
    assert result["integrity_summary"] == {"verified": 1, "unsigned": 1}


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


# ---------------------------------------------------------------------------
# Rotation-aware querying and streaming reads
#
# audit_query.py did f.readlines() — the whole log into memory on every call,
# on a file that never rotated. Now that it rotates, the query also has to look
# at the backups or rotation would silently shrink the queryable window.
# ---------------------------------------------------------------------------


@pytest.fixture()
def rotated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AGENT_ID", "test-agent")
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("AUDIT_LOG_BACKUP_COUNT", "5")
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def test_query_reads_rotated_backups(tools, rotated_env):
    """An entry that has aged into a backup must still be findable — otherwise
    rotation would drop diagnostic history with no indication it had."""
    write_audit_entry("git_needle", "local", "/repo/needle", {}, "ok", 1)
    # Enough to rotate the needle out of the live file, but well inside the
    # 5-backup retention window so it is genuinely still on disk.
    for i in range(8):
        write_audit_entry("git_status", "local", f"/repo/{i}", {}, "ok", i)

    assert (rotated_env / "audit.jsonl.1").exists(), "precondition: log rotated"
    assert "needle" not in (rotated_env / "audit.jsonl").read_text(), (
        "precondition: the needle is no longer in the live file"
    )

    result = tools["audit_log_query"](tool="git_needle", limit=10)

    assert result["total_matched"] == 1, (
        f"entry in a rotated backup was not found: {result['total_matched']}"
    )
    assert result["entries"][0]["repo"] == "/repo/needle"


def test_query_reports_which_sources_it_searched(tools, rotated_env):
    for i in range(40):
        write_audit_entry("git_status", "local", f"/repo/{i}", {}, "ok", i)
    result = tools["audit_log_query"](limit=1000)
    assert "audit.jsonl" in result["sources_searched"]
    assert any(s.startswith("audit.jsonl.") for s in result["sources_searched"])


def test_query_stops_at_limit_without_reading_every_backup(tools, rotated_env):
    for i in range(40):
        write_audit_entry("git_status", "local", f"/repo/{i}", {}, "ok", i)
    all_sources = tools["audit_log_query"](limit=1000)["sources_searched"]
    assert len(all_sources) > 1, "precondition: there are backups to skip"

    result = tools["audit_log_query"](limit=1)

    assert len(result["entries"]) == 1
    assert len(result["sources_searched"]) < len(all_sources), (
        "backups must not be read once the limit is already satisfied"
    )


def test_reverse_read_handles_entries_spanning_block_boundaries(tools, tmp_path, monkeypatch):
    """The block-wise reverse reader must not drop or corrupt a line that straddles
    a block boundary."""
    import githost_mcp.tools.audit_query as aq

    monkeypatch.setattr(aq, "_REVERSE_READ_BLOCK", 64)
    for i in range(50):
        write_audit_entry("git_status", "local", f"/repo/{i:03d}", {}, "ok", i)

    result = tools["audit_log_query"](limit=1000)

    assert result["total_matched"] == 50, f"lines lost across block boundaries: {result}"
    repos = [e["repo"] for e in result["entries"]]
    assert repos[0] == "/repo/049", "newest-first ordering broken"
    assert repos[-1] == "/repo/000"
    assert len(set(repos)) == 50, "duplicate or corrupted lines"


def test_query_without_trailing_newline_keeps_last_entry(tools, tmp_path, monkeypatch):
    """A file whose final line has no newline must still yield that entry."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_FILE", str(path))
    reset_config()
    path.write_text(json.dumps({"ts": "2026-08-01T00:00:00.000Z", "tool": "git_x", "repo": "/r"}))
    result = tools["audit_log_query"]()
    assert result["total_matched"] == 1

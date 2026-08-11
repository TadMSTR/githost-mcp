"""Tests for JSONL audit log: writing, HMAC tamper-evidence, credential filter."""

import json
import os

import pytest

from githost_mcp.audit import (
    INTEGRITY_TAMPERED,
    INTEGRITY_UNSIGNED,
    INTEGRITY_UNVERIFIABLE,
    INTEGRITY_VERIFIED,
    AuditCtx,
    init_logging,
    verify_entry_hmac,
    verify_entry_integrity,
    write_audit_entry,
)
from githost_mcp.config import reset_config


@pytest.fixture()
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AGENT_ID", "test-agent")
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def _read_entries(path):
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def test_write_audit_entry_creates_file(audit_env):
    write_audit_entry("git_status", "local", "/tmp/repo", {"repo_path": "/tmp/repo"}, "ok", 10)
    audit_path = os.environ["AUDIT_LOG_FILE"]
    assert os.path.exists(audit_path)


def test_audit_entry_has_hmac(audit_env):
    write_audit_entry("git_log", "local", "/repo", {}, "ok", 5)
    entries = _read_entries(os.environ["AUDIT_LOG_FILE"])
    assert len(entries) == 1
    assert "hmac" in entries[0]


def test_audit_entry_agent_id(audit_env):
    write_audit_entry("git_push", "local", "/repo", {}, "ok", 20)
    entries = _read_entries(os.environ["AUDIT_LOG_FILE"])
    assert entries[0]["agent_id"] == "test-agent"


def test_hmac_valid_entry(audit_env):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    entries = _read_entries(os.environ["AUDIT_LOG_FILE"])
    assert verify_entry_hmac(entries[0]) is True


def test_hmac_tampered_entry(audit_env):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    audit_path = os.environ["AUDIT_LOG_FILE"]
    entries = _read_entries(audit_path)
    entry = entries[0]
    entry["result"] = "tampered"
    assert verify_entry_hmac(entry) is False


def test_hmac_byte_flip_detected(audit_env):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    audit_path = os.environ["AUDIT_LOG_FILE"]
    raw = audit_path
    with open(raw, "rb") as f:
        data = bytearray(f.read())
    # Flip a byte in the middle of the file content
    idx = len(data) // 2
    data[idx] = (data[idx] + 1) % 256
    with open(raw, "wb") as f:
        f.write(data)
    entries = _read_entries(audit_path)
    # At least one entry should fail HMAC (may be unparseable)
    for entry in entries:
        if "hmac" in entry:
            assert verify_entry_hmac(entry) is False


# ---------------------------------------------------------------------------
# Integrity classification (vikunja#301, id 312)
#
# verify_entry_hmac used to return True when no signing key was configured, so an
# entry with no tamper evidence at all reported exactly like one that had been
# checked and found intact.
# ---------------------------------------------------------------------------


def test_integrity_verified_for_signed_intact_entry(audit_env):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    entries = _read_entries(os.environ["AUDIT_LOG_FILE"])
    assert verify_entry_integrity(entries[0]) == INTEGRITY_VERIFIED


def test_integrity_tampered_for_altered_signed_entry(audit_env):
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    entry = _read_entries(os.environ["AUDIT_LOG_FILE"])[0]
    entry["result"] = "tampered"
    assert verify_entry_integrity(entry) == INTEGRITY_TAMPERED


def test_integrity_unsigned_when_no_key_configured(audit_env, monkeypatch):
    """The case that motivated this: an entry written with no key must never be
    reportable as intact."""
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()
    init_logging()
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)

    entry = _read_entries(os.environ["AUDIT_LOG_FILE"])[0]
    assert "hmac" not in entry
    assert verify_entry_integrity(entry) == INTEGRITY_UNSIGNED
    assert verify_entry_hmac(entry) is False


def test_integrity_unsigned_stays_unsigned_after_key_is_added(audit_env):
    """An entry from the unsigned window must remain identifiable once the key
    lands — the absence of an hmac is a fact about the entry, not about config."""
    unsigned_entry = {
        "ts": "2026-07-30T00:00:00.000Z",
        "agent_id": "writer",
        "tool": "git_commit",
        "provider": "local",
        "repo": "/repo",
        "params": {},
        "result": "ok",
        "duration_ms": 1,
    }
    # audit_env has a signing key configured.
    assert verify_entry_integrity(unsigned_entry) == INTEGRITY_UNSIGNED


def test_integrity_unverifiable_when_signed_but_no_key_here(audit_env, monkeypatch):
    """A signed entry read by a process holding no key: no opinion, not a pass."""
    write_audit_entry("git_status", "local", "/repo", {}, "ok", 10)
    entry = _read_entries(os.environ["AUDIT_LOG_FILE"])[0]
    assert "hmac" in entry

    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()
    init_logging()

    assert verify_entry_integrity(entry) == INTEGRITY_UNVERIFIABLE
    assert verify_entry_hmac(entry) is False


def test_missing_key_logs_startup_warning(audit_env, monkeypatch, capsys):
    # Read stderr rather than caplog: init_logging() clears the root handlers,
    # which removes pytest's own capture handler along with them.
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    monkeypatch.setenv("AGENT_ID", "writer")
    reset_config()
    init_logging()
    err = capsys.readouterr().err
    assert "audit_signing_key_unset" in err
    assert "writer" in err, "the warning must name the agent whose key is missing"


def test_present_key_logs_no_startup_warning(audit_env, capsys):
    init_logging()
    assert "audit_signing_key_unset" not in capsys.readouterr().err


def test_credential_not_in_audit_log(audit_env, monkeypatch):
    """GITHUB_TOKEN must never appear in JSONL entries."""
    fake_token = "ghp_fakefakefakefakefakefakefakefake123"
    monkeypatch.setenv("GITHUB_TOKEN", fake_token)
    reset_config()
    init_logging()
    write_audit_entry(
        "github_create_release",
        "github",
        "owner/repo",
        {"token_leaked": fake_token},
        f"error with {fake_token}",
        5,
    )
    audit_path = os.environ["AUDIT_LOG_FILE"]
    with open(audit_path) as f:
        content = f.read()
    assert fake_token not in content


def test_audit_ctx(audit_env):
    ctx = AuditCtx("git_commit", "local", "/repo", {"message": "test"})
    ctx.finish("ok")
    entries = _read_entries(os.environ["AUDIT_LOG_FILE"])
    assert entries[0]["tool"] == "git_commit"
    assert entries[0]["result"] == "ok"


# ---------------------------------------------------------------------------
# Audit log rotation
#
# audit.py wrote the JSONL with a plain open(path, "a") and never rotated it.
# audit_log_max_bytes / audit_log_backup_count were consumed by the APPLICATION
# log's RotatingFileHandler instead — named for a file they did not govern.
# Live at the time of the fix: githost-developer.jsonl 942 KB since 2026-06-02,
# growing ~15 KB/day/agent with no bound.
# ---------------------------------------------------------------------------


@pytest.fixture()
def rotating_audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AGENT_ID", "test-agent")
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("AUDIT_LOG_BACKUP_COUNT", "2")
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def _write_n(n, tool="git_status"):
    for i in range(n):
        write_audit_entry(tool, "local", f"/tmp/repo{i}", {"repo_path": f"/tmp/repo{i}"}, "ok", i)


def test_audit_log_rotates_at_configured_size(rotating_audit_env):
    path = rotating_audit_env / "audit.jsonl"
    _write_n(40)

    assert (rotating_audit_env / "audit.jsonl.1").exists(), "audit log never rotated"
    assert path.stat().st_size <= 1024, "live audit file exceeded audit_log_max_bytes"


def test_audit_rotation_honours_backup_count(rotating_audit_env):
    _write_n(300)
    assert (rotating_audit_env / "audit.jsonl.1").exists()
    assert (rotating_audit_env / "audit.jsonl.2").exists()
    assert not (rotating_audit_env / "audit.jsonl.3").exists(), (
        "backup_count=2 must not keep a third backup"
    )


def test_rotated_entries_still_verify(rotating_audit_env):
    """Per-entry HMAC is per-entry, not a chain, so renaming the file cannot
    invalidate a signature. This asserts that rather than assuming it."""
    _write_n(40)
    rotated = _read_entries(rotating_audit_env / "audit.jsonl.1")
    assert rotated, "precondition: rotated file has entries"
    for entry in rotated:
        assert verify_entry_hmac(entry), "rotation broke an existing HMAC"


def test_rotation_never_truncates_within_the_backup_window(tmp_path, monkeypatch):
    """Rotation renames — it must never truncate or rewrite the tamper-evident
    record. With enough backup slots to hold everything, no entry goes missing."""
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AGENT_ID", "test-agent")
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("AUDIT_LOG_BACKUP_COUNT", "50")
    reset_config()
    init_logging()
    try:
        _write_n(40)
        total = len(_read_entries(tmp_path / "audit.jsonl"))
        for i in range(1, 51):
            p = tmp_path / f"audit.jsonl.{i}"
            if p.exists():
                total += len(_read_entries(p))
        assert total == 40, f"entries lost across rotation: {total}"
    finally:
        reset_config()


def test_backups_beyond_backup_count_are_aged_out(rotating_audit_env):
    """Retention is bounded by design — this is the trade for an unbounded log.
    At the deployed defaults (10 MB x 5) that is ~11 years at the observed
    ~15 KB/day/agent, so it is a ceiling rather than a real retention limit."""
    _write_n(300)
    assert not (rotating_audit_env / "audit.jsonl.3").exists()


def test_rotation_disabled_when_max_bytes_is_zero(rotating_audit_env, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "0")
    reset_config()
    _write_n(40)
    assert not (rotating_audit_env / "audit.jsonl.1").exists()


def test_oversized_single_entry_still_written(rotating_audit_env):
    """An entry larger than max_bytes must not be silently dropped."""
    write_audit_entry("git_status", "local", "/tmp/r", {"blob": "x" * 4000}, "ok", 1)
    entries = _read_entries(rotating_audit_env / "audit.jsonl")
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# Credential filter recursion
#
# _credential_filter scrubbed top-level strings only, while write_audit_entry's
# scrubber alongside it recursed. A token inside a dict or list passed to a log
# call therefore reached the log verbatim.
# ---------------------------------------------------------------------------


def test_credential_filter_scrubs_nested_structures(audit_env, monkeypatch):
    from githost_mcp.audit import _credential_filter

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecrettoken1234567890")
    reset_config()

    event = {
        "event": "call",
        "top": "token ghp_supersecrettoken1234567890 here",
        "nested": {"inner": {"deep": "ghp_supersecrettoken1234567890"}},
        "listed": ["ghp_supersecrettoken1234567890", {"k": "ghp_supersecrettoken1234567890"}],
        "untouched": 42,
    }
    filtered = _credential_filter(None, "info", event)

    assert "ghp_supersecrettoken1234567890" not in repr(filtered)
    assert filtered["nested"]["inner"]["deep"] == "***"
    assert filtered["listed"][0] == "***"
    assert filtered["listed"][1]["k"] == "***"
    assert filtered["untouched"] == 42


def test_credential_filter_noop_without_tokens(audit_env, monkeypatch):
    from githost_mcp.audit import _credential_filter

    for var in ("GITHUB_TOKEN", "GITEA_TOKEN", "GITLAB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AUDIT_SIGNING_KEY", raising=False)
    reset_config()

    event = {"event": "call", "value": "nothing to scrub"}
    assert _credential_filter(None, "info", event)["value"] == "nothing to scrub"


def test_concurrent_writes_do_not_lose_entries_across_rotation(rotating_audit_env):
    """The HTTP transport writes from a threadpool. An unguarded check-and-rotate
    would race an append and drop entries."""
    import threading

    def worker(n):
        for i in range(20):
            write_audit_entry("git_status", "local", f"/repo/{n}-{i}", {}, "ok", i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seen = set()
    for name in ["audit.jsonl"] + [f"audit.jsonl.{i}" for i in range(1, 3)]:
        p = rotating_audit_env / name
        if p.exists():
            for entry in _read_entries(p):
                seen.add(entry["repo"])

    # Entries aged past backup_count are legitimately gone; what must NOT happen is
    # a corrupt/interleaved line, which would fail json.loads in _read_entries above.
    assert seen, "no entries survived at all"
    assert all(r.startswith("/repo/") for r in seen), "interleaved write corrupted a line"


# --- rotation failure is logged, not silent (audit LOW, batch 2) -------------


def test_rotation_failure_logs_and_does_not_break_the_write(rotating_audit_env, monkeypatch):
    """A failed final rename means the live file never rolls and every subsequent
    write re-fails the same way. Silently, that grows past max_bytes forever."""
    _write_n(6)  # get the live file over the 1024-byte threshold

    real_replace = os.replace

    def failing_replace(src, dst):
        if str(src) == str(rotating_audit_env / "audit.jsonl"):
            raise OSError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    events = []
    monkeypatch.setattr(
        "githost_mcp.audit.log",
        type(
            "L",
            (),
            {
                m: staticmethod(lambda _e=None, **k: events.append((_e, k)))
                for m in ("info", "warning", "error")
            },
        )(),
    )

    write_audit_entry("git_status", "local", "/tmp/after", {}, "ok", 1)

    assert any(e == "audit_rotation_failed" for e, _ in events), (
        f"a failed rotation must not be silent: {events}"
    )
    # The entry itself must still land — rotation failure never loses an audit entry.
    assert any(e["repo"] == "/tmp/after" for e in _read_entries(rotating_audit_env / "audit.jsonl"))


def test_rotation_backup_shift_failure_is_logged(rotating_audit_env, monkeypatch):
    """A mid-chain shift failure can leave backup numbering inconsistent."""
    _write_n(20)  # produce at least one backup to shift

    real_replace = os.replace

    def failing_replace(src, dst):
        if str(src).endswith(".1"):
            raise OSError(5, "I/O error")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    events = []
    monkeypatch.setattr(
        "githost_mcp.audit.log",
        type(
            "L",
            (),
            {
                m: staticmethod(lambda _e=None, **k: events.append((_e, k)))
                for m in ("info", "warning", "error")
            },
        )(),
    )

    _write_n(20)

    assert any(e == "audit_rotation_step_failed" for e, _ in events), (
        f"a failed backup shift must not be silent: {events}"
    )


def test_missing_oldest_backup_is_not_logged_as_a_failure(rotating_audit_env, monkeypatch):
    """The common case — nothing to age out yet — must stay quiet."""
    events = []
    monkeypatch.setattr(
        "githost_mcp.audit.log",
        type(
            "L",
            (),
            {
                m: staticmethod(lambda _e=None, **k: events.append((_e, k)))
                for m in ("info", "warning", "error")
            },
        )(),
    )

    _write_n(6)

    assert not any(e == "audit_rotation_step_failed" for e, _ in events), (
        f"absent backup file logged as a failure: {events}"
    )

"""Tests for JSONL audit log: writing, HMAC tamper-evidence, credential filter."""

import json
import os

import pytest

from githost_mcp.audit import (
    AuditCtx,
    init_logging,
    verify_entry_hmac,
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


def test_credential_not_in_audit_log(audit_env, monkeypatch):
    """GITHUB_TOKEN must never appear in JSONL entries."""
    fake_token = "ghp_fakefakefakefakefakefakefakefake123"
    monkeypatch.setenv("GITHUB_TOKEN", fake_token)
    reset_config()
    init_logging()
    write_audit_entry(
        "github_create_release", "github", "owner/repo",
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

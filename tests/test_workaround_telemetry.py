"""Reason-carrying audit records, the denial counter, and the metrics producer.

Three defects are under test here, and each has a test that fails if the defect
returns:

1. The audit record carried the exception *type* and not its message, so every
   allowlist rejection, bad repo format and missing remote landed in one
   indistinguishable `error:ValueError` bucket.
2. A pre-flight guard (`_REPO_FMT_ERR` and friends) returned before AuditCtx was
   constructed, so those rejections produced no record and no metric at all.
3. `emit_tool_event()` had no production call site, so `githost_tool_calls_total`
   had never incremented and the metrics ports served zero `githost_*` series.
"""

import json
import os

import pytest

from githost_mcp.audit import (
    INTEGRITY_VERIFIED,
    AuditCtx,
    audit_rejection,
    init_logging,
    verify_entry_integrity,
    write_audit_entry,
)
from githost_mcp.config import reset_config
from githost_mcp.errors import (
    REASON_CLASSES,
    BranchNameInvalid,
    InvalidArgument,
    NoSuchRemote,
    PathNotAllowed,
    RemoteNameInvalid,
    RemoteUrlRejected,
    RepoFormatInvalid,
    RepoNotFound,
    WriteGlobDenied,
    classify_reason,
)

# A token long enough to clear the >4 char scrub floor, in the shape a real one has.
FAKE_TOKEN = "ghp_0123456789abcdef0123456789abcdef0123"


@pytest.fixture()
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AGENT_ID", "test-agent")
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def _entries():
    out = []
    with open(os.environ["AUDIT_LOG_FILE"]) as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 1. The reason field
# ---------------------------------------------------------------------------


def test_reason_is_written_to_the_audit_record(audit_env):
    AuditCtx("git_push", "local", "/repo", {}).finish(
        "error:ValueError", ValueError("no such remote 'upstream'")
    )
    (entry,) = _entries()
    assert entry["reason"] == "no such remote 'upstream'"


def test_successful_call_carries_no_reason_field(audit_env):
    AuditCtx("git_status", "local", "/repo", {}).finish("ok")
    (entry,) = _entries()
    assert "reason" not in entry


def test_allowlist_and_repo_format_rejections_are_distinguishable(audit_env):
    """The plan's phase-1 acceptance criterion, stated as a test.

    Before this build these produced, respectively, `error:ValueError` with no
    detail — and *nothing at all*, because the repo-format guard returned before
    AuditCtx existed.
    """
    AuditCtx("git_push", "local", "/etc/passwd", {}).finish(
        "error:PathNotAllowed",
        PathNotAllowed("Path '/etc/passwd' is not under any allowed root"),
    )
    audit_rejection(
        "gitea_pr_merge", "gitea", "not-a-repo", "bad_repo_format", "repo must be in 'owner/repo'"
    )

    allowlist, repo_fmt = _entries()
    assert allowlist["reason"] != repo_fmt["reason"]
    assert "not under any allowed root" in allowlist["reason"]
    assert "owner/repo" in repo_fmt["reason"]


def test_reason_is_scrubbed_of_a_configured_token(audit_env):
    AuditCtx("git_push", "local", "/repo", {}).finish(
        "error:GitCommandError",
        RuntimeError(f"failed to push to https://x-access-token:{FAKE_TOKEN}@github.com/o/r.git"),
    )
    (entry,) = _entries()
    assert FAKE_TOKEN not in entry["reason"]
    assert "***" in entry["reason"]


def test_reason_is_scrubbed_of_an_unknown_url_credential(audit_env):
    """A PAT a human pasted into a git remote is not in our config, so the
    token-value scrub cannot see it. It is redacted by URL shape instead."""
    AuditCtx("git_pull", "local", "/repo", {}).finish(
        "error:GitCommandError",
        RuntimeError("fatal: could not read from https://ted:hunter2secret@gitea.example/o/r"),
    )
    (entry,) = _entries()
    assert "hunter2secret" not in entry["reason"]
    assert "***@gitea.example" in entry["reason"]


def test_reason_does_not_leak_into_the_result_field(audit_env):
    """`result` is a Prometheus label. Exception messages carry repo paths and
    branch names, so putting one there is unbounded cardinality."""
    AuditCtx("git_push", "local", "/repo", {}).finish(
        "error:ValueError", ValueError("branch feature/some-very-specific-name is invalid")
    )
    (entry,) = _entries()
    assert entry["result"] == "error:ValueError"
    assert "feature/some-very-specific-name" not in entry["result"]


def test_reason_accepts_a_plain_string(audit_env):
    AuditCtx("git_push", "local", "/repo", {}).finish(
        "error:PushRejected", "rejected: non-fast-forward"
    )
    (entry,) = _entries()
    assert entry["reason"] == "rejected: non-fast-forward"


# ---------------------------------------------------------------------------
# HMAC compatibility
# ---------------------------------------------------------------------------


def test_record_with_reason_still_verifies(audit_env):
    write_audit_entry("git_push", "local", "/repo", {}, "error:X", 5, reason="boom")
    (entry,) = _entries()
    assert verify_entry_integrity(entry) == INTEGRITY_VERIFIED


def test_record_without_reason_still_verifies(audit_env):
    """Records written before this change carry no `reason` key. The HMAC is
    computed over whatever keys the entry has, so they must still verify."""
    write_audit_entry("git_push", "local", "/repo", {}, "ok", 5)
    (entry,) = _entries()
    assert "reason" not in entry
    assert verify_entry_integrity(entry) == INTEGRITY_VERIFIED


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WriteGlobDenied("/r", ["x.py"], "policy"), "write_glob_denied"),
        (RemoteUrlRejected("bad url"), "remote_url_rejected"),
        (RemoteNameInvalid("bad name"), "remote_name_invalid"),
        (BranchNameInvalid("bad branch"), "branch_invalid"),
        (PathNotAllowed("outside roots"), "not_in_allowed_roots"),
        (NoSuchRemote("no remote"), "no_such_remote"),
        (RepoNotFound("not a repo"), "repo_not_found"),
        (RepoFormatInvalid("bad format"), "bad_repo_format"),
        (InvalidArgument("bad arg"), "invalid_argument"),
    ],
)
def test_classify_by_exception_type(exc, expected):
    assert classify_reason(f"error:{type(exc).__name__}", exc) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("error:PushRejected", "push_rejected"),
        ("error:FetchRejected", "fetch_rejected"),
        ("denied:write_glob", "write_glob_denied"),
        ("denied:remote_url", "remote_url_rejected"),
        ("denied:identity_undetermined", "identity_undetermined"),
        ("denied:bad_repo_format", "bad_repo_format"),
        ("denied:invalid_argument", "invalid_argument"),
    ],
)
def test_classify_by_result_string(result, expected):
    assert classify_reason(result) == expected


def test_unrecognised_reason_is_counted_as_other_not_dropped():
    assert classify_reason("error:SomethingNew", RuntimeError("?")) == "other"
    assert classify_reason("denied:a_class_we_never_defined") == "other"


def test_every_classification_is_a_vocabulary_member():
    """Bounded cardinality is the invariant. classify_reason() must not be able to
    return a label value outside the closed set, whatever it is handed."""
    samples = [
        ("ok", None),
        ("error:ValueError", ValueError("x")),
        ("error:PushRejected", None),
        ("denied:write_glob", WriteGlobDenied("/r", ["a"], "s")),
        ("anything at all", RuntimeError("x")),
        ("denied:made_up", None),
    ]
    for result, exc in samples:
        assert classify_reason(result, exc) in REASON_CLASSES


def test_exception_type_wins_over_result_string():
    """A generic result string must not override a type we can name precisely."""
    assert classify_reason("error:ValueError", PathNotAllowed("x")) == "not_in_allowed_roots"


# ---------------------------------------------------------------------------
# 3. The metrics producer
# ---------------------------------------------------------------------------


def test_finish_emits_a_tool_event(audit_env, monkeypatch):
    """Regression test for the dead producer: finish() wrote the JSONL and returned,
    so no metric was ever produced. A counter nothing increments exports no series
    at all, which reads identically to a healthy-but-idle server."""
    calls = []
    monkeypatch.setattr(
        "githost_mcp.audit.emit_tool_event_sync",
        lambda *a: calls.append(a),
    )
    AuditCtx("git_push", "local", "/srv/repos/myrepo", {}).finish("ok")
    assert len(calls) == 1
    tool, provider, repo_basename, result, _duration, reason_class = calls[0]
    assert (tool, provider, result) == ("git_push", "local", "ok")
    assert repo_basename == "myrepo"
    assert reason_class is None


def test_finish_emits_reason_class_for_a_denial(audit_env, monkeypatch):
    calls = []
    monkeypatch.setattr("githost_mcp.audit.emit_tool_event_sync", lambda *a: calls.append(a))
    AuditCtx("git_add", "local", "/srv/repos/myrepo", {}).finish(
        "denied:write_glob", WriteGlobDenied("/srv/repos/myrepo", ["secrets.env"], "policy")
    )
    assert calls[0][-1] == "write_glob_denied"


def test_ok_prefixed_results_are_not_counted_as_denials(audit_env, monkeypatch):
    calls = []
    monkeypatch.setattr("githost_mcp.audit.emit_tool_event_sync", lambda *a: calls.append(a))
    AuditCtx("git_branch_delete_remote", "local", "/repo", {}).finish("ok:already_absent")
    assert calls[0][-1] is None


def test_telemetry_failure_cannot_fail_the_tool_call(audit_env, monkeypatch):
    def boom(*a):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("githost_mcp.audit.emit_tool_event_sync", boom)
    AuditCtx("git_status", "local", "/repo", {}).finish("ok")  # must not raise
    assert _entries()[0]["result"] == "ok"


def test_denied_counter_increments_only_for_denials(audit_env, monkeypatch):
    """Phase 2 acceptance, against the real Prometheus objects."""
    from prometheus_client import CollectorRegistry, Counter

    from githost_mcp import observability as obs

    registry = CollectorRegistry()
    denied = Counter(
        "githost_tool_denied_total",
        "test",
        ["tool", "agent_id", "reason_class"],
        registry=registry,
    )
    monkeypatch.setattr(obs, "_prom_tool_denied", denied)
    monkeypatch.setattr(obs, "_prom_tool_calls", None)

    def value(**labels):
        return registry.get_sample_value("githost_tool_denied_total", labels) or 0.0

    labels = {"tool": "git_add", "agent_id": "test-agent", "reason_class": "write_glob_denied"}

    obs.emit_tool_event_sync("git_add", "local", "repo", "ok", 5, None)
    assert value(**labels) == 0.0, "a successful call must not increment the denial counter"

    obs.emit_tool_event_sync(
        "git_add", "local", "repo", "denied:write_glob", 5, "write_glob_denied"
    )
    assert value(**labels) == 1.0


def test_audit_rejection_records_and_returns_the_error(audit_env):
    out = audit_rejection("gitea_pr_list", "gitea", "bad repo", "bad_repo_format", "nope")
    assert out == {"error": "nope"}
    (entry,) = _entries()
    assert entry["tool"] == "gitea_pr_list"
    assert entry["result"] == "denied:bad_repo_format"
    assert entry["reason"] == "nope"


def test_audit_rejection_accepts_an_error_dict(audit_env):
    out = audit_rejection("github_pr_get", "github", "o/r", "bad_repo_format", {"error": "nope"})
    assert out == {"error": "nope"}
    assert _entries()[0]["reason"] == "nope"

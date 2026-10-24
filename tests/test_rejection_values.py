"""Rejection records keep the rejected value (agent-build-workflow-2026-09 part 4, Phase 2).

Before 0.15.0 an enum rejection wrote `params: {"repo": ...}` and nothing else, so the
largest denial group on forge — a wrong `method` on github_issue_write / github_actions,
34 calls in a month — recorded that agents guessed wrong but never what they guessed.
"""

import json
import os

import pytest

from githost_mcp.audit import (
    MAX_REJECTED_VALUE_CHARS,
    audit_rejection,
    bound_rejected_value,
    init_logging,
    reject_enum,
)
from githost_mcp.config import reset_config
from githost_mcp.errors import (
    REASON_CLASSES,
    InvalidArgument,
    MissingArgument,
    ProtectedBranch,
    classify_reason,
)

FAKE_TOKEN = "ghp_0123456789abcdef0123456789abcdef0123"


@pytest.fixture(autouse=True)
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AGENT_ID", "test-agent")
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fakefakefake1234567890abcdef")
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "gitea-fake-token-1234567890")
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def _entries():
    with open(os.environ["AUDIT_LOG_FILE"]) as f:
        return [json.loads(line) for line in f if line.strip()]


def _tools(module):
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    module.register(MockMCP())
    return registered


VALID = frozenset({"create", "update", "add_comment"})


# --- the record ------------------------------------------------------------


def test_invalid_value_is_recorded():
    reject_enum("github_issue_write", "github", "o/r", "method", "comment", VALID)
    (entry,) = _entries()
    assert entry["result"] == "denied:invalid_argument"
    assert entry["params"] == {
        "repo": "o/r",
        "argument": "method",
        "rejected_value": "comment",
    }


def test_empty_value_is_missing_not_invalid():
    reject_enum("github_issue_write", "github", "o/r", "method", "", VALID)
    (entry,) = _entries()
    assert entry["result"] == "denied:missing_argument"
    assert entry["params"]["rejected_value"] == ""


def test_absent_value_is_recorded_as_null():
    reject_enum("github_issue_write", "github", "o/r", "method", None, VALID)
    (entry,) = _entries()
    assert entry["result"] == "denied:missing_argument"
    assert entry["params"]["argument"] == "method"
    assert entry["params"]["rejected_value"] is None


def test_message_names_the_value_and_the_options():
    out = reject_enum("t", "github", "o/r", "method", "list", VALID)
    assert out["error"] == "method 'list' is not valid; must be one of: add_comment, create, update"


def test_hint_is_appended():
    out = reject_enum("t", "github", "o/r", "method", "list", VALID, hint="use other_tool")
    assert out["error"].endswith(". use other_tool")


def test_audit_rejection_without_argument_keeps_the_old_shape():
    audit_rejection("github_pr_get", "github", "bad", "bad_repo_format", "nope")
    (entry,) = _entries()
    assert entry["params"] == {"repo": "bad"}


# --- bounding and scrubbing ---------------------------------------------------


def test_value_at_the_bound_is_not_truncated():
    value = "a" * MAX_REJECTED_VALUE_CHARS
    assert bound_rejected_value(value) == (value, False)


def test_value_over_the_bound_is_truncated_and_flagged():
    reject_enum("t", "github", "o/r", "method", "a" * (MAX_REJECTED_VALUE_CHARS + 1), VALID)
    (entry,) = _entries()
    assert entry["params"]["rejected_value"] == "a" * MAX_REJECTED_VALUE_CHARS
    assert entry["params"]["rejected_value_truncated"] is True


def test_short_value_carries_no_truncation_flag():
    reject_enum("t", "github", "o/r", "method", "short", VALID)
    (entry,) = _entries()
    assert "rejected_value_truncated" not in entry["params"]


def test_scrub_happens_before_the_bound():
    """A configured token straddling the 64-char cut must not leave a prefix behind.

    Bound-then-scrub would cut the token in half; the half no longer matches the
    configured value, so nothing masks it, and a partial secret lands in a signed log.
    """
    pad = "p" * (MAX_REJECTED_VALUE_CHARS - len(FAKE_TOKEN) // 2)
    reject_enum("t", "github", "o/r", "method", pad + FAKE_TOKEN, VALID)
    (entry,) = _entries()
    for field in (entry["params"]["rejected_value"], entry["reason"]):
        assert FAKE_TOKEN not in field
        assert FAKE_TOKEN[: len(FAKE_TOKEN) // 2] not in field


def test_unknown_url_credential_is_scrubbed():
    reject_enum("t", "github", "o/r", "method", "https://u:hunter2secret@h/x", VALID)
    (entry,) = _entries()
    assert "hunter2secret" not in entry["params"]["rejected_value"]
    assert "hunter2secret" not in entry["reason"]


# --- vocabulary ----------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (MissingArgument("comment is required"), "missing_argument"),
        (ProtectedBranch("main"), "protected_branch"),
        (InvalidArgument("bad"), "invalid_argument"),
    ],
)
def test_new_types_classify(exc, expected):
    assert classify_reason(f"error:{type(exc).__name__}", exc) == expected
    assert expected in REASON_CLASSES


# --- end to end through the tools ------------------------------------------------


def test_github_actions_list_points_at_the_workflow_tools():
    from githost_mcp.tools import github

    out = _tools(github)["github_actions"]("o/r", "list")
    assert "github_workflow_list" in out["error"]
    assert "github_workflow_status" in out["error"]
    (entry,) = _entries()
    assert entry["tool"] == "github_actions"
    assert entry["params"]["rejected_value"] == "list"


def test_github_actions_empty_method_is_missing():
    from githost_mcp.tools import github

    _tools(github)["github_actions"]("o/r", "")
    (entry,) = _entries()
    assert entry["result"] == "denied:missing_argument"


def test_github_issue_write_points_at_issue_read():
    from githost_mcp.tools import github

    out = _tools(github)["github_issue_write"]("o/r", "get")
    assert "github_issue_read" in out["error"]


async def test_gitea_issue_write_points_at_issue_read():
    from githost_mcp.tools import gitea

    out = await _tools(gitea)["gitea_issue_write"]("o/r", "list")
    assert "gitea_issue_read" in out["error"]


def test_gitlab_issue_write_points_at_issue_read():
    from githost_mcp.tools import gitlab

    out = _tools(gitlab)["gitlab_issue_write"]("g/p", "comments")
    assert "gitlab_issue_read" in out["error"]


def test_read_tools_carry_no_sibling_hint():
    from githost_mcp.tools import github

    out = _tools(github)["github_issue_read"]("o/r", "create")
    assert "use github" not in out["error"]


def test_missing_method_specific_argument_is_classified():
    """`comment is required for add_comment` was a bare ValueError, counted as `other`."""
    from unittest.mock import MagicMock, patch

    from githost_mcp.tools import github

    with patch("githost_mcp.tools.github.get_github", return_value=MagicMock()):
        out = _tools(github)["github_issue_write"]("o/r", "add_comment", issue_number=1)
    assert "comment is required" in out["error"]
    (entry,) = _entries()
    assert entry["result"] == "error:MissingArgument"

"""github_pr_update: edit fields, and draft -> ready without losing the PR (#925)."""

from unittest.mock import MagicMock, patch

import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config
from githost_mcp.errors import MissingArgument
from githost_mcp.security import BranchNameInvalid
from githost_mcp.tools.github import pr_edit_kwargs


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fakefakefake123456789012345678901")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()
    import githost_mcp._providers.github_client as gc

    gc._client = None


@pytest.fixture()
def update():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.github import register

    register(MockMCP())
    return registered["github_pr_update"]


def _pr(draft: bool):
    pr = MagicMock()
    pr.number = 7
    pr.title = "t"
    pr.base.ref = "main"
    pr.draft = draft
    pr.html_url = "https://github.com/o/r/pull/7"
    return pr


def _gh(before, after):
    repo = MagicMock()
    repo.get_pull.side_effect = [before, after]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def test_kwargs_only_carry_passed_fields():
    assert pr_edit_kwargs("t", None, None, False) == {"title": "t"}
    assert pr_edit_kwargs(None, "", None, False) == {"body": ""}


def test_kwargs_ready_only_is_not_an_edit():
    assert pr_edit_kwargs(None, None, None, True) == {}


def test_kwargs_nothing_to_do_is_refused():
    with pytest.raises(MissingArgument):
        pr_edit_kwargs(None, None, None, False)


def test_kwargs_bad_base_is_refused():
    with pytest.raises(BranchNameInvalid):
        pr_edit_kwargs(None, None, "-x", False)


def test_draft_to_ready(update):
    before, after = _pr(draft=True), _pr(draft=False)
    with patch("githost_mcp.tools.github.get_github", return_value=_gh(before, after)):
        out = update("o/r", 7, ready_for_review=True)
    before.mark_ready_for_review.assert_called_once_with()
    before.edit.assert_not_called()
    assert out["number"] == 7
    assert (out["was_draft"], out["draft"], out["marked_ready"]) == (True, False, True)


def test_already_ready_is_left_alone(update):
    before, after = _pr(draft=False), _pr(draft=False)
    with patch("githost_mcp.tools.github.get_github", return_value=_gh(before, after)):
        out = update("o/r", 7, ready_for_review=True)
    before.mark_ready_for_review.assert_not_called()
    assert out["marked_ready"] is False and out["draft"] is False


def test_still_draft_after_the_mutation_is_an_error(update):
    before, after = _pr(draft=True), _pr(draft=True)
    with patch("githost_mcp.tools.github.get_github", return_value=_gh(before, after)):
        out = update("o/r", 7, ready_for_review=True)
    assert "still a draft" in out["error"]


def test_edit_fields_without_touching_draft(update):
    before, after = _pr(draft=True), _pr(draft=True)
    with patch("githost_mcp.tools.github.get_github", return_value=_gh(before, after)):
        out = update("o/r", 7, title="new", base="develop")
    before.edit.assert_called_once_with(title="new", base="develop")
    before.mark_ready_for_review.assert_not_called()
    assert out["updated_fields"] == ["base", "title"]


def test_nothing_to_update_makes_no_api_call(update):
    gh = MagicMock()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        out = update("o/r", 7)
    assert "nothing to update" in out["error"]
    gh.get_repo.assert_not_called()


def test_bad_repo_is_rejected(update):
    assert "error" in update("not-a-repo", 7, title="x")

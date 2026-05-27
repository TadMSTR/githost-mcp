"""Tests for local git tools using real temporary repositories."""

import os

import git
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "test")
    reset_config()
    init_logging()


@pytest.fixture()
def repo_path(tmp_path):
    repo = git.Repo.init(tmp_path / "repo")
    r = repo
    r.config_writer().set_value("user", "name", "Test").release()
    r.config_writer().set_value("user", "email", "t@test.com").release()
    (tmp_path / "repo" / "file.txt").write_text("hello")
    r.index.add(["file.txt"])
    r.index.commit("Initial")
    return tmp_path / "repo"


# Import tool functions via the register pattern
@pytest.fixture()
def tools(repo_path, monkeypatch):
    """Returns a dict of tool callables by registering on a mock mcp."""
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.git_local import register
    register(MockMCP())
    return registered, repo_path


def test_git_status(tools):
    fns, path = tools
    result = fns["git_status"](str(path))
    assert "branch" in result
    assert "staged" in result
    assert result["is_dirty"] is False


def test_git_log(tools):
    fns, path = tools
    result = fns["git_log"](str(path))
    assert len(result["commits"]) >= 1
    assert result["commits"][0]["message"] == "Initial"


def test_git_diff_clean(tools):
    fns, path = tools
    result = fns["git_diff"](str(path))
    assert result["patches"] == []


def test_git_show(tools):
    fns, path = tools
    result = fns["git_show"](str(path), "HEAD")
    assert "sha" in result
    assert result["message"] == "Initial"


def test_git_branch_list(tools):
    fns, path = tools
    result = fns["git_branch"](str(path), action="list")
    assert "branches" in result
    assert "active" in result


def test_git_branch_create_delete(tools):
    fns, path = tools
    result = fns["git_branch"](str(path), action="create", branch_name="feature-x")
    assert result.get("created") == "feature-x"
    result = fns["git_branch"](str(path), action="delete", branch_name="feature-x")
    assert result.get("deleted") == "feature-x"


def test_git_add_and_commit(tools, tmp_path):
    fns, path = tools
    (path / "new.txt").write_text("new file")
    add_result = fns["git_add"](str(path), ["new.txt"])
    assert "staged" in add_result
    commit_result = fns["git_commit"](str(path), "Add new file")
    assert "sha" in commit_result


def test_git_commit_appends_agent_id(tools, tmp_path):
    fns, path = tools
    (path / "agent.txt").write_text("by agent")
    fns["git_add"](str(path), ["agent.txt"])
    fns["git_commit"](str(path), "Agent commit")
    import git as gitmod
    repo = gitmod.Repo(str(path))
    msg = repo.head.commit.message
    assert "agent-id: test" in msg


def test_git_tag(tools):
    fns, path = tools
    result = fns["git_tag"](str(path), "v0.1.0", message="Test tag")
    assert result.get("tag") == "v0.1.0"
    assert "sha" in result


def test_write_blocked_outside_allowed_roots(tools, tmp_path, monkeypatch):
    fns, path = tools
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "/nonexistent/root")
    reset_config()
    result = fns["git_add"](str(path), ["file.txt"])
    assert "error" in result

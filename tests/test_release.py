"""Tests for release orchestration: dry-run and rollback."""

import git
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AGENT_ID", "test")
    reset_config()
    init_logging()


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.release import register
    register(MockMCP())
    return registered


@pytest.fixture()
def clean_repo(tmp_path):
    repo = git.Repo.init(tmp_path / "proj")
    r = repo
    r.config_writer().set_value("user", "name", "Test").release()
    r.config_writer().set_value("user", "email", "t@t.com").release()
    (tmp_path / "proj" / "file.txt").write_text("v1")
    r.index.add(["file.txt"])
    r.index.commit("Initial")
    return tmp_path / "proj"


@pytest.mark.asyncio
async def test_dry_run_returns_plan(tools, clean_repo):
    fns = tools
    result = await fns["release"](
        str(clean_repo), "1.2.3", targets=["github", "pypi"], dry_run=True
    )
    assert result["dry_run"] is True
    assert result["tag"] == "v1.2.3"
    assert result["version"] == "1.2.3"
    assert "github" in result["targets"]


@pytest.mark.asyncio
async def test_dirty_repo_blocked(tools, clean_repo):
    fns = tools
    (clean_repo / "dirty.txt").write_text("dirty")
    import git as g
    repo = g.Repo(str(clean_repo))
    repo.index.add(["dirty.txt"])
    result = await fns["release"](str(clean_repo), "1.0.0")
    assert "error" in result
    assert "dirty" in result["error"].lower()


@pytest.mark.asyncio
async def test_allowed_roots_enforced(tools, clean_repo, monkeypatch):
    fns = tools
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "/nonexistent/path")
    reset_config()
    result = await fns["release"](str(clean_repo), "1.0.0")
    assert "error" in result

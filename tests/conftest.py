"""Shared test fixtures."""

import git
import pytest


@pytest.fixture()
def tmp_repo(tmp_path):
    """Create a temporary git repository with an initial commit."""
    repo = git.Repo.init(tmp_path)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    (tmp_path / "README.md").write_text("# Test repo\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return repo, tmp_path


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    """Reset config singleton between tests.

    Also strips AGENT_ID/AGENT_MANIFEST_PATH so a real manifest on the host
    machine can't leak into tests that don't opt into the manifest-fallback
    path explicitly.
    """
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_MANIFEST_PATH", raising=False)
    from githost_mcp.config import reset_config

    reset_config()
    yield
    reset_config()

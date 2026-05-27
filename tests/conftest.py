"""Shared test fixtures."""

import os
import pytest
import git


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
def reset_config():
    """Reset config singleton between tests."""
    from githost_mcp.config import reset_config
    reset_config()
    yield
    reset_config()

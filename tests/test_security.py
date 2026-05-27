"""Tests for path allowlist validation and path traversal rejection."""

import os
import pytest

from githost_mcp.security import validate_write_path


@pytest.fixture()
def allowed_env(tmp_path, monkeypatch):
    allowed = str(tmp_path / "repos")
    os.makedirs(allowed, exist_ok=True)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", allowed)
    from githost_mcp.config import reset_config
    reset_config()
    yield allowed, tmp_path


def test_path_under_allowed_root_passes(allowed_env):
    allowed, tmp = allowed_env
    repo_path = os.path.join(allowed, "myrepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_write_path(repo_path)  # should not raise


def test_path_outside_allowed_root_blocked(allowed_env):
    allowed, tmp = allowed_env
    outside = str(tmp / "other" / "repo")
    os.makedirs(outside, exist_ok=True)
    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_write_path(outside)


def test_traversal_blocked(allowed_env):
    allowed, tmp = allowed_env
    traversal = os.path.join(allowed, "../../../etc/passwd")
    with pytest.raises(ValueError):
        validate_write_path(traversal)


def test_unset_allowed_roots_blocks_all(monkeypatch):
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    from githost_mcp.config import reset_config
    reset_config()
    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_write_path("/tmp/any/path")


def test_empty_allowed_roots_blocks_all(monkeypatch):
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "")
    from githost_mcp.config import reset_config
    reset_config()
    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_write_path("/tmp/any/path")


def test_symlink_traversal_blocked(allowed_env, tmp_path):
    """A symlink pointing outside allowed roots must be blocked."""
    allowed, tmp = allowed_env
    target = tmp / "secret"
    target.mkdir()
    link = tmp / "repos" / "sneaky_link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        validate_write_path(str(link / "subdir"))

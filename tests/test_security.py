"""Tests for path allowlist validation and path traversal rejection."""

import os

import pytest

from githost_mcp.security import (
    _PM2_IPC_ENV_VARS,
    clean_env,
    validate_read_path,
    validate_write_path,
)


@pytest.fixture()
def allowed_env(tmp_path, monkeypatch):
    allowed = str(tmp_path / "repos")
    os.makedirs(allowed, exist_ok=True)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", allowed)
    from githost_mcp.config import reset_config

    reset_config()
    yield allowed, tmp_path


def test_path_under_allowed_root_passes(allowed_env):
    allowed, _tmp = allowed_env
    repo_path = os.path.join(allowed, "myrepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_write_path(repo_path)  # should not raise


def test_path_outside_allowed_root_blocked(allowed_env):
    _allowed, tmp = allowed_env
    outside = str(tmp / "other" / "repo")
    os.makedirs(outside, exist_ok=True)
    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_write_path(outside)


def test_traversal_blocked(allowed_env):
    allowed, _tmp = allowed_env
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
    _allowed, tmp = allowed_env
    target = tmp / "secret"
    target.mkdir()
    link = tmp / "repos" / "sneaky_link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        validate_write_path(str(link / "subdir"))


def test_read_path_under_allowed_root_passes(allowed_env):
    allowed, _tmp = allowed_env
    repo_path = os.path.join(allowed, "myrepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_read_path(repo_path)  # should not raise


def test_read_path_outside_allowed_root_blocked(allowed_env):
    _allowed, tmp = allowed_env
    outside = str(tmp / "other" / "repo")
    os.makedirs(outside, exist_ok=True)
    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_read_path(outside)


def test_read_unset_allowed_roots_blocks_all(monkeypatch):
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    from githost_mcp.config import reset_config

    reset_config()
    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_read_path("/tmp/any/path")


@pytest.fixture()
def manifest_allowed_env(tmp_path, monkeypatch):
    """Populate allowed_repo_roots via the manifest-fallback path instead of env."""
    import yaml

    allowed = str(tmp_path / "repos")
    os.makedirs(allowed, exist_ok=True)
    manifest_path = tmp_path / "developer-agent.yml"
    with open(manifest_path, "w") as f:
        yaml.safe_dump({"workspace_access": [{"path": allowed, "git_backed": True}]}, f)

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", str(manifest_path))
    from githost_mcp.config import reset_config

    reset_config()
    yield allowed, tmp_path


def test_manifest_sourced_path_under_allowed_root_passes(manifest_allowed_env):
    """validate_write_path doesn't care whether roots came from env or manifest."""
    allowed, _tmp = manifest_allowed_env
    repo_path = os.path.join(allowed, "myrepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_write_path(repo_path)  # should not raise
    validate_read_path(repo_path)  # should not raise


def test_manifest_sourced_path_outside_allowed_root_blocked(manifest_allowed_env):
    _allowed, tmp = manifest_allowed_env
    outside = str(tmp / "other" / "repo")
    os.makedirs(outside, exist_ok=True)
    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_write_path(outside)


# --- clean_env (GHOST-11) -----------------------------------------------------


def test_clean_env_strips_pm2_vars(monkeypatch):
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    monkeypatch.setenv("KEEP_ME", "yes")

    env = clean_env()

    for var in _PM2_IPC_ENV_VARS:
        assert var not in env
    assert env["KEEP_ME"] == "yes"

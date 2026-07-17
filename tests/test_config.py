"""Tests for allowlist resolution: explicit env var vs. manifest-aware fallback."""

import pytest

from githost_mcp.config import get_config, reset_config


def _write_manifest(path, workspace_access):
    import yaml

    with open(path, "w") as f:
        yaml.safe_dump({"workspace_access": workspace_access}, f)


@pytest.fixture()
def manifest_path(tmp_path):
    return str(tmp_path / "developer-agent.yml")


def test_explicit_env_wins_over_manifest(tmp_path, manifest_path, monkeypatch):
    env_root = str(tmp_path / "env-repos")
    manifest_root = str(tmp_path / "manifest-repos")
    _write_manifest(manifest_path, [{"path": manifest_root, "git_backed": True}])

    monkeypatch.setenv("ALLOWED_REPO_ROOTS", env_root)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [env_root]
    assert config.allowlist_source == "env"


def test_manifest_fallback_when_env_unset(tmp_path, manifest_path, monkeypatch):
    git_root = str(tmp_path / "repos" / "personal")
    non_git_root = str(tmp_path / "memory" / "shared")
    _write_manifest(
        manifest_path,
        [
            {"path": git_root, "git_backed": True, "access": "readwrite"},
            {"path": non_git_root, "git_backed": False, "access": "readwrite"},
        ],
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_path}"


def test_manifest_fallback_empty_env_var(tmp_path, manifest_path, monkeypatch):
    """An explicitly-empty ALLOWED_REPO_ROOTS is treated the same as unset."""
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True}])

    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "")
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_path}"


def test_no_env_no_manifest_fails_closed(tmp_path, monkeypatch):
    missing_path = str(tmp_path / "does-not-exist.yml")
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", missing_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"

    from githost_mcp.security import validate_write_path

    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_write_path("/tmp/any/path")


def test_no_agent_id_no_manifest_attempted(tmp_path, monkeypatch):
    """Without AGENT_ID set, no default manifest path is derived at all."""
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_MANIFEST_PATH", raising=False)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"


def test_malformed_manifest_yaml_fails_closed(tmp_path, manifest_path, monkeypatch):
    with open(manifest_path, "w") as f:
        f.write("workspace_access: [{path: unterminated\n")

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    # Must not raise — falls back to the fail-closed "none" source.
    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"


def test_manifest_with_no_workspace_access_key_fails_closed(tmp_path, manifest_path, monkeypatch):
    import yaml

    with open(manifest_path, "w") as f:
        yaml.safe_dump({"agent_type": "developer"}, f)

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"


def test_transport_defaults_to_stdio_loopback_no_auth(monkeypatch):
    monkeypatch.delenv("TRANSPORT", raising=False)
    monkeypatch.delenv("HTTP_PORT", raising=False)
    monkeypatch.delenv("GITHOST_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("GITHOST_MCP_ALLOW_NONLOOPBACK", raising=False)
    reset_config()

    config = get_config()
    assert config.transport == "stdio"
    assert config.http_host == "127.0.0.1"
    assert config.http_port is None
    assert config.allow_nonloopback is False
    assert config.auth_token == ""


def test_transport_http_reads_host_port_and_token(monkeypatch):
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "s3cr3t-token-value")
    reset_config()

    config = get_config()
    assert config.transport == "http"
    assert config.http_host == "127.0.0.1"
    assert config.http_port == 8620
    assert config.auth_token == "s3cr3t-token-value"


def test_allow_nonloopback_only_true_when_flag_is_exactly_one(monkeypatch):
    monkeypatch.setenv("GITHOST_MCP_ALLOW_NONLOOPBACK", "true")
    reset_config()
    assert get_config().allow_nonloopback is False

    monkeypatch.setenv("GITHOST_MCP_ALLOW_NONLOOPBACK", "1")
    reset_config()
    assert get_config().allow_nonloopback is True


def test_default_manifest_path_derived_from_agent_id(tmp_path, monkeypatch):
    """When AGENT_MANIFEST_PATH is unset, it's derived from AGENT_ID."""
    fake_home = tmp_path / "home"
    manifests_dir = fake_home / ".claude" / "manifests"
    manifests_dir.mkdir(parents=True)
    git_root = str(tmp_path / "repos" / "personal")
    manifest_file = manifests_dir / "developer-agent.yml"
    _write_manifest(str(manifest_file), [{"path": git_root, "git_backed": True}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.delenv("AGENT_MANIFEST_PATH", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("HOME", str(fake_home))
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_file}"

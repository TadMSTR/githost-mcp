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
    # access: readwrite so this test proves env precedence, rather than passing
    # because the access filter dropped the manifest entry anyway.
    _write_manifest(
        manifest_path, [{"path": manifest_root, "git_backed": True, "access": "readwrite"}]
    )

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
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])

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


# --- access: filter on the manifest fallback (M-2) ----------------------------
#
# allowed_repo_roots is one list consulted by both validate_read_path() and
# validate_write_path(), so "not readwrite" means no githost-mcp access at all.


@pytest.mark.parametrize(
    "access",
    ["readonly", "read-only", "rw", "READWRITE", "", None],
)
def test_manifest_entry_excluded_unless_access_is_readwrite(
    tmp_path, manifest_path, monkeypatch, access
):
    """Anything that is not exactly `readwrite` fails closed, including absent."""
    git_root = str(tmp_path / "repos" / "personal")
    entry = {"path": git_root, "git_backed": True}
    if access is not None:
        entry["access"] = access
    _write_manifest(manifest_path, [entry])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"


def test_manifest_readwrite_entry_included(tmp_path, manifest_path, monkeypatch):
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    assert get_config().allowed_repo_roots == [git_root]


def test_readonly_entry_dropped_from_mixed_manifest(tmp_path, manifest_path, monkeypatch):
    """A readonly entry alongside readwrite ones is the realistic shape."""
    rw_root = str(tmp_path / "repos" / "personal")
    ro_root = str(tmp_path / "appdata")
    _write_manifest(
        manifest_path,
        [
            {"path": rw_root, "git_backed": True, "access": "readwrite"},
            {"path": ro_root, "git_backed": True, "access": "readonly"},
        ],
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [rw_root]
    assert ro_root not in config.allowed_repo_roots


def test_readonly_git_backed_entry_rejects_writes_and_reads(tmp_path, manifest_path, monkeypatch):
    """The negative test M-2 describes: readonly + git_backed must not grant write.

    Exercises the validators rather than just the parsed list, so a future change
    that reintroduces the entry downstream still fails here.
    """
    import os

    ro_root = tmp_path / "appdata"
    repo_under_ro = ro_root / "somerepo"
    repo_under_ro.mkdir(parents=True)
    _write_manifest(
        manifest_path, [{"path": str(ro_root), "git_backed": True, "access": "readonly"}]
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    from githost_mcp.security import validate_read_path, validate_write_path

    # No readwrite entries at all -> allowlist is empty -> fail closed.
    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_write_path(str(repo_under_ro))
    with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS is not set"):
        validate_read_path(str(repo_under_ro))

    assert os.path.isdir(repo_under_ro)  # the path exists; access is what's denied


def test_readonly_entry_blocked_when_other_roots_are_allowed(tmp_path, manifest_path, monkeypatch):
    """Same as above but with a non-empty allowlist, so the error path differs."""
    rw_root = tmp_path / "repos"
    ro_root = tmp_path / "appdata"
    (rw_root / "ok").mkdir(parents=True)
    (ro_root / "denied").mkdir(parents=True)
    _write_manifest(
        manifest_path,
        [
            {"path": str(rw_root), "git_backed": True, "access": "readwrite"},
            {"path": str(ro_root), "git_backed": True, "access": "readonly"},
        ],
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    from githost_mcp.security import validate_read_path, validate_write_path

    validate_write_path(str(rw_root / "ok"))  # should not raise

    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_write_path(str(ro_root / "denied"))
    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_read_path(str(ro_root / "denied"))


def test_non_git_backed_readwrite_entry_still_excluded(tmp_path, manifest_path, monkeypatch):
    """git_backed remains an independent condition — access alone isn't enough."""
    _write_manifest(
        manifest_path,
        [{"path": str(tmp_path / "memory"), "git_backed": False, "access": "readwrite"}],
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    assert get_config().allowed_repo_roots == []


def test_skipped_entry_is_logged(tmp_path, manifest_path, monkeypatch):
    """A narrowed allowlist must be diagnosable, not silent.

    Stubs the module-level logger rather than reconfiguring structlog globally:
    audit.py configures structlog with cache_logger_on_first_use=True, so by the
    time this test runs in a full-suite pass config.py's bound logger is already
    cached and a late structlog.configure() would not reach it.
    """
    import githost_mcp.config as config_mod

    captured = []

    class _RecordingLogger:
        def warning(self, event, **kw):
            captured.append((event, kw))

        def info(self, event, **kw):
            pass

    ro_root = str(tmp_path / "appdata")
    _write_manifest(manifest_path, [{"path": ro_root, "git_backed": True, "access": "readonly"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(config_mod, "log", _RecordingLogger())

    reset_config()
    get_config()

    skipped = [kw for event, kw in captured if event == "manifest_allowlist_entry_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["entry_path"] == ro_root
    assert skipped[0]["access"] == "readonly"


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
    _write_manifest(
        str(manifest_file), [{"path": git_root, "git_backed": True, "access": "readwrite"}]
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.delenv("AGENT_MANIFEST_PATH", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("HOME", str(fake_home))
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_file}"


def test_explicit_manifest_path_never_falls_back_to_default(tmp_path, monkeypatch):
    """An explicit AGENT_MANIFEST_PATH suppresses the ~/.claude default entirely.

    This is the property the whole allowlist cutover rests on (vikunja #271, id
    282). The default path is a symlink into a git working tree that five agents
    hold readwrite access to; production points AGENT_MANIFEST_PATH at a deployed,
    root-owned copy instead. If a refactor ever made the default a *fallback* for
    an unreadable explicit path, every agent would silently resume reading the
    agent-writable file and the decoupling would be undone with no visible symptom
    — the allowlist would still be populated, just from the wrong place.

    So: make the default path exist and grant something, point AGENT_MANIFEST_PATH
    at a file that does not exist, and assert the result is empty rather than the
    default's roots. Hermetic — it does not depend on a real manifest being present
    on the host, which is what makes it meaningful on a CI runner too.
    """
    fake_home = tmp_path / "home"
    manifests_dir = fake_home / ".claude" / "manifests"
    manifests_dir.mkdir(parents=True)
    default_root = str(tmp_path / "repos" / "should-not-be-read")
    _write_manifest(
        str(manifests_dir / "developer-agent.yml"),
        [{"path": default_root, "git_backed": True, "access": "readwrite"}],
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGENT_MANIFEST_PATH", str(tmp_path / "deployed" / "nope.yml"))
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowlist_source == "none"
    assert default_root not in config.allowed_repo_roots


def test_explicit_manifest_path_wins_over_readable_default(tmp_path, monkeypatch):
    """Both paths readable — the explicit one is authoritative, not merged."""
    fake_home = tmp_path / "home"
    manifests_dir = fake_home / ".claude" / "manifests"
    manifests_dir.mkdir(parents=True)
    default_root = str(tmp_path / "repos" / "working-tree")
    _write_manifest(
        str(manifests_dir / "developer-agent.yml"),
        [{"path": default_root, "git_backed": True, "access": "readwrite"}],
    )

    deployed = tmp_path / "deployed"
    deployed.mkdir()
    deployed_file = deployed / "developer-agent.yml"
    deployed_root = str(tmp_path / "repos" / "deployed-copy")
    _write_manifest(
        str(deployed_file), [{"path": deployed_root, "git_backed": True, "access": "readwrite"}]
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGENT_MANIFEST_PATH", str(deployed_file))
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [deployed_root]
    assert default_root not in config.allowed_repo_roots
    assert config.allowlist_source == f"manifest:{deployed_file}"

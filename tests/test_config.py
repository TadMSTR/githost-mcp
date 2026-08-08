"""Tests for allowlist resolution: explicit env var vs. manifest-aware fallback."""

import pytest

from githost_mcp.config import get_config, load_config, reset_config


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
    assert config.allowed_write_roots == [env_root]
    # The env escape hatch applies to both lists, not write-only.
    assert config.allowed_read_roots == [env_root]
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

    with pytest.raises(ValueError, match="Write operations are disabled"):
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


# --- access: filter on the manifest fallback (M-2, revised for the read/write split) --
#
# allowed_read_roots and allowed_write_roots are now separate lists.
# access: readwrite grants both; access: readonly grants read only (Phase 1 —
# previously it granted nothing at all, see CHANGELOG). Anything else, including
# absent, grants neither.


@pytest.mark.parametrize(
    "access",
    ["read-only", "rw", "READWRITE", "", None],
)
def test_manifest_entry_excluded_unless_recognized_access(
    tmp_path, manifest_path, monkeypatch, access
):
    """Anything that is not exactly `readwrite` or `readonly` fails closed, including absent."""
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
    assert config.allowed_read_roots == []
    assert config.allowed_write_roots == []
    assert config.allowlist_source == "none"


def test_manifest_readwrite_entry_included(tmp_path, manifest_path, monkeypatch):
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_repo_roots == [git_root]
    assert config.allowed_write_roots == [git_root]
    assert config.allowed_read_roots == [git_root]


def test_manifest_readonly_entry_grants_read_only(tmp_path, manifest_path, monkeypatch):
    """Phase 1 behaviour change: access: readonly now grants read, still not write."""
    ro_root = str(tmp_path / "appdata")
    _write_manifest(manifest_path, [{"path": ro_root, "git_backed": True, "access": "readonly"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    reset_config()

    config = get_config()
    assert config.allowed_read_roots == [ro_root]
    assert config.allowed_write_roots == []
    assert config.allowed_repo_roots == []  # deprecated alias tracks write, not read
    assert config.allowlist_source == f"manifest:{manifest_path}"


def test_readonly_entry_alongside_readwrite_in_mixed_manifest(tmp_path, manifest_path, monkeypatch):
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
    assert config.allowed_write_roots == [rw_root]
    assert ro_root not in config.allowed_write_roots
    assert set(config.allowed_read_roots) == {rw_root, ro_root}


def test_readonly_git_backed_entry_grants_read_denies_write(tmp_path, manifest_path, monkeypatch):
    """The M-2 property, updated for Phase 1: readonly + git_backed grants read, not write.

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

    validate_read_path(str(repo_under_ro))  # should not raise — this is the Phase 1 fix

    # No write-granting entries at all -> write allowlist is empty -> fail closed.
    with pytest.raises(ValueError, match="Write operations are disabled"):
        validate_write_path(str(repo_under_ro))

    assert os.path.isdir(repo_under_ro)  # the path exists; write is what's denied


def test_readonly_entry_write_blocked_when_other_roots_are_allowed(
    tmp_path, manifest_path, monkeypatch
):
    """Same as above but with a non-empty write allowlist, so the error path differs."""
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
    validate_read_path(str(ro_root / "denied"))  # should not raise — readonly grants read

    with pytest.raises(ValueError, match="not under any allowed root"):
        validate_write_path(str(ro_root / "denied"))


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

    config = get_config()
    assert config.allowed_repo_roots == []
    assert config.allowed_read_roots == []


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

    bad_root = str(tmp_path / "appdata")
    _write_manifest(manifest_path, [{"path": bad_root, "git_backed": True, "access": "rw"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(config_mod, "log", _RecordingLogger())

    reset_config()
    get_config()

    skipped = [kw for event, kw in captured if event == "manifest_allowlist_entry_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["entry_path"] == bad_root
    assert skipped[0]["access"] == "rw"


def test_readonly_entry_is_not_logged_as_skipped(tmp_path, manifest_path, monkeypatch):
    """readonly is now a recognized, granted access level — it must not log as skipped."""
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
    assert skipped == []


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


# --- workspace-policy.yml loader (Phase 1) -----------------------------------
#
# Resolution order is env > policy > manifest > empty. Phase 1 ships with the
# policy file absent in production (sysadmin deploys it in Phase 2), so the
# "file missing" case above (falls through to manifest) is the live behaviour
# today — these tests cover the loader itself and the precedence.


def _write_policy(path, data):
    import yaml

    with open(path, "w") as f:
        yaml.safe_dump(data, f)


@pytest.fixture()
def policy_path(tmp_path):
    return str(tmp_path / "workspace-policy.yml")


def test_missing_policy_file_falls_back_to_manifest(tmp_path, manifest_path, monkeypatch):
    """Phase 1's critical requirement: absent policy file must not break anything."""
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(tmp_path / "does-not-exist.yml"))
    reset_config()

    config = get_config()
    assert config.allowed_write_roots == [git_root]
    assert config.allowed_read_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_path}"


def test_policy_grants_read_to_agents_listed_regardless_of_write_roots(
    tmp_path, policy_path, manifest_path, monkeypatch
):
    """default_read: all means every agent listed in `agents:` reads every root,
    even one whose own write_roots is empty (e.g. research, security)."""
    root_a = str(tmp_path / "repos" / "gitea")
    root_b = str(tmp_path / "repos" / "personal")
    _write_policy(
        policy_path,
        {
            "version": 1,
            "roots": [{"path": root_a}, {"path": root_b}],
            "default_read": "all",
            "agents": {"research": {"write_roots": []}},
            "explicit_agents": {},
        },
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "research")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)  # absent — must not matter
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert set(config.allowed_read_roots) == {root_a, root_b}
    assert config.allowed_write_roots == []
    assert config.allowlist_source == f"policy:{policy_path}"


def test_policy_scopes_write_roots_per_agent(tmp_path, policy_path, monkeypatch):
    root_a = str(tmp_path / "repos" / "gitea")
    root_b = str(tmp_path / "repos" / "personal")
    _write_policy(
        policy_path,
        {
            "version": 1,
            "roots": [{"path": root_a}, {"path": root_b}],
            "default_read": "all",
            "agents": {
                "writer": {
                    "write_roots": [root_a, root_b],
                    "write_globs": ["docs/**", "**/*.md"],
                    "write_globs_deny": ["**/AGENT_WORKSPACE.md"],
                }
            },
            "explicit_agents": {},
        },
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "writer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert set(config.allowed_read_roots) == {root_a, root_b}
    assert set(config.allowed_write_roots) == {root_a, root_b}
    assert config.write_globs == ["docs/**", "**/*.md"]
    assert config.write_globs_deny == ["**/AGENT_WORKSPACE.md"]


def test_policy_agent_not_listed_gets_nothing_even_with_manifest_present(
    tmp_path, policy_path, manifest_path, monkeypatch
):
    """A present, parseable policy is authoritative — it must not fall through to
    the manifest just because this specific agent has no entry in it. Otherwise
    the whole point of centralizing grants (explicit, itemized) is defeated."""
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])
    _write_policy(
        policy_path,
        {
            "version": 1,
            "roots": [{"path": git_root}],
            "default_read": "all",
            "agents": {"developer": {"write_roots": [git_root]}},
            "explicit_agents": {},
        },
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "harlock")  # not in agents: or explicit_agents:
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert config.allowed_read_roots == []
    assert config.allowed_write_roots == []
    assert config.allowlist_source == f"policy:{policy_path}"  # not the manifest


def test_policy_explicit_agents_itemized_grant_does_not_inherit_default_read(
    tmp_path, policy_path, monkeypatch
):
    root_a = str(tmp_path / "repos" / "gitea")
    root_b = str(tmp_path / "repos" / "third-party")
    narrow_root = str(tmp_path / "repos" / "third-party" / "one-repo")
    _write_policy(
        policy_path,
        {
            "version": 1,
            "roots": [{"path": root_a}, {"path": root_b}],
            "default_read": "all",
            "agents": {},
            "explicit_agents": {"jobsearch": {"read_roots": [narrow_root]}},
        },
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "jobsearch")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    # Itemized grant only — does NOT inherit default_read: all across every root.
    assert config.allowed_read_roots == [narrow_root]
    assert config.allowed_write_roots == []


def test_malformed_policy_falls_back_to_manifest(tmp_path, policy_path, manifest_path, monkeypatch):
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])
    with open(policy_path, "w") as f:
        f.write("agents: [{unterminated\n")

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert config.allowed_write_roots == [git_root]
    assert config.allowlist_source == f"manifest:{manifest_path}"


def test_malformed_policy_logs_error_not_warning(tmp_path, policy_path, manifest_path, monkeypatch):
    """githost-workspace-policy-2026-08 audit LOW: a policy file that exists but fails to
    load must log louder than the ordinary (file-absent) fallback case, since it silently
    resurrects the broader manifest allowlist. log.error, distinct event name."""
    import githost_mcp.config as config_mod

    captured = []

    class _RecordingLogger:
        def error(self, event, **kw):
            captured.append(("error", event, kw))

        def warning(self, event, **kw):
            captured.append(("warning", event, kw))

        def info(self, event, **kw):
            pass

    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])
    with open(policy_path, "w") as f:
        f.write("agents: [{unterminated\n")

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    monkeypatch.setattr(config_mod, "log", _RecordingLogger())

    reset_config()
    get_config()

    errors = [(event, kw) for level, event, kw in captured if level == "error"]
    assert len(errors) == 1
    assert errors[0][0] == "policy_load_failed_present"
    assert errors[0][1]["path"] == policy_path
    warnings_for_policy = [
        kw for level, event, kw in captured if level == "warning" and "policy" in event
    ]
    assert warnings_for_policy == []


def test_empty_policy_fails_closed_on_both_lists(tmp_path, policy_path, manifest_path, monkeypatch):
    """A policy that loads but has no roots/agents at all -> empty, and still
    authoritative (does not fall through to the manifest)."""
    git_root = str(tmp_path / "repos" / "personal")
    _write_manifest(manifest_path, [{"path": git_root, "git_backed": True, "access": "readwrite"}])
    _write_policy(policy_path, {"version": 1, "roots": [], "agents": {}, "explicit_agents": {}})

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", manifest_path)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert config.allowed_read_roots == []
    assert config.allowed_write_roots == []
    assert config.allowlist_source == f"policy:{policy_path}"


def test_env_wins_over_policy(tmp_path, policy_path, monkeypatch):
    env_root = str(tmp_path / "env-repos")
    policy_root = str(tmp_path / "policy-repos")
    _write_policy(
        policy_path,
        {
            "version": 1,
            "roots": [{"path": policy_root}],
            "default_read": "all",
            "agents": {"developer": {"write_roots": [policy_root]}},
            "explicit_agents": {},
        },
    )

    monkeypatch.setenv("ALLOWED_REPO_ROOTS", env_root)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", policy_path)
    reset_config()

    config = get_config()
    assert config.allowed_read_roots == [env_root]
    assert config.allowed_write_roots == [env_root]
    assert config.allowlist_source == "env"


def test_workspace_policy_path_env_overrides_default_location(tmp_path, monkeypatch):
    custom_path = str(tmp_path / "custom-policy.yml")
    git_root = str(tmp_path / "repos" / "personal")
    _write_policy(
        custom_path,
        {
            "version": 1,
            "roots": [{"path": git_root}],
            "default_read": "all",
            "agents": {"developer": {"write_roots": [git_root]}},
            "explicit_agents": {},
        },
    )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", custom_path)
    reset_config()

    config = get_config()
    assert config.allowed_write_roots == [git_root]
    assert config.allowlist_source == f"policy:{custom_path}"


# --- integer env parsing ----------------------------------------------------
#
# get_config() runs at import time (server.py), so a malformed value used to
# kill the process with a bare int() traceback naming neither the variable nor
# the value.


@pytest.mark.parametrize(
    "var", ["METRICS_PORT", "HTTP_PORT", "AUDIT_LOG_MAX_BYTES", "AUDIT_LOG_BACKUP_COUNT"]
)
def test_malformed_int_env_names_the_variable(monkeypatch, var):
    monkeypatch.setenv(var, "not-a-number")
    reset_config()
    with pytest.raises(ValueError) as exc:
        load_config()
    assert var in str(exc.value), f"error does not name the variable: {exc.value}"
    assert "not-a-number" in str(exc.value), f"error does not show the bad value: {exc.value}"


def test_log_rotation_settings_default_to_audit_values(monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("AUDIT_LOG_BACKUP_COUNT", "3")
    monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
    reset_config()
    config = load_config()
    assert config.log_max_bytes == 2048
    assert config.log_backup_count == 3


def test_log_rotation_settings_are_independently_tunable(monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("LOG_MAX_BYTES", "4096")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "9")
    reset_config()
    config = load_config()
    assert config.audit_log_max_bytes == 2048
    assert config.log_max_bytes == 4096
    assert config.log_backup_count == 9

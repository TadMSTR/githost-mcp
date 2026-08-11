"""Tests for path allowlist validation and path traversal rejection."""

import os

import pytest

from githost_mcp.security import (
    _PM2_IPC_ENV_VARS,
    RemoteUrlRejected,
    WriteGlobDenied,
    clean_env,
    validate_read_path,
    validate_remote_name,
    validate_remote_url,
    validate_write_globs,
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
    with pytest.raises(ValueError, match="Write operations are disabled"):
        validate_write_path("/tmp/any/path")


def test_empty_allowed_roots_blocks_all(monkeypatch):
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "")
    from githost_mcp.config import reset_config

    reset_config()
    with pytest.raises(ValueError, match="Write operations are disabled"):
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
    with pytest.raises(ValueError, match="Read operations are disabled"):
        validate_read_path("/tmp/any/path")


@pytest.fixture()
def manifest_allowed_env(tmp_path, monkeypatch):
    """Populate allowed_repo_roots via the manifest-fallback path instead of env."""
    import yaml

    allowed = str(tmp_path / "repos")
    os.makedirs(allowed, exist_ok=True)
    manifest_path = tmp_path / "developer-agent.yml"
    with open(manifest_path, "w") as f:
        yaml.safe_dump(
            {"workspace_access": [{"path": allowed, "git_backed": True, "access": "readwrite"}]},
            f,
        )

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


# --- read/write split (workspace-policy Phase 1) ------------------------------


@pytest.fixture()
def readonly_manifest_env(tmp_path, monkeypatch):
    """A manifest with a single access: readonly entry."""
    import yaml

    ro_root = str(tmp_path / "appdata")
    os.makedirs(ro_root, exist_ok=True)
    manifest_path = tmp_path / "developer-agent.yml"
    with open(manifest_path, "w") as f:
        yaml.safe_dump(
            {"workspace_access": [{"path": ro_root, "git_backed": True, "access": "readonly"}]},
            f,
        )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("AGENT_MANIFEST_PATH", str(manifest_path))
    from githost_mcp.config import reset_config

    reset_config()
    yield ro_root, tmp_path


def test_readonly_root_allows_read_denies_write(readonly_manifest_env):
    ro_root, _tmp = readonly_manifest_env
    repo_path = os.path.join(ro_root, "somerepo")
    os.makedirs(repo_path, exist_ok=True)

    validate_read_path(repo_path)  # should not raise

    # No write-granting entries at all -> write allowlist is empty -> fail closed.
    with pytest.raises(ValueError, match="Write operations are disabled"):
        validate_write_path(repo_path)


def test_glob_enforcement_flag_is_true():
    """Phase 3 (githost-workspace-policy-2026-08) implemented glob enforcement in
    git_add/git_commit. This flag must stay True — flipping it back to False without
    also removing enforcement reopens the audit's MEDIUM (see the flag's docstring)."""
    from githost_mcp.security import _GLOB_ENFORCEMENT_IMPLEMENTED

    assert _GLOB_ENFORCEMENT_IMPLEMENTED is True


def test_write_allowed_when_globs_granted_now_that_enforcement_exists(tmp_path, monkeypatch):
    """Pre-Phase-3 regression guard, updated: an agent whose grant carries write_globs
    is no longer blanket-denied by validate_write_path() now that validate_write_globs()
    exists to enforce the actual scope — the repo-root check and the glob check are two
    separate gates."""
    import yaml

    write_root = str(tmp_path / "repos" / "gitea")
    os.makedirs(write_root, exist_ok=True)
    policy_path = tmp_path / "workspace-policy.yml"
    with open(policy_path, "w") as f:
        yaml.safe_dump(
            {
                "version": 1,
                "roots": [{"path": write_root}],
                "default_read": "all",
                "agents": {
                    "writer": {
                        "write_roots": [write_root],
                        "write_globs": ["docs/**"],
                    }
                },
                "explicit_agents": {},
            },
            f,
        )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "writer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(policy_path))
    from githost_mcp.config import reset_config

    reset_config()

    repo_path = os.path.join(write_root, "somerepo")
    os.makedirs(repo_path, exist_ok=True)

    validate_read_path(repo_path)  # read is unaffected
    validate_write_path(repo_path)  # should not raise — root check only, glob is separate


def test_write_allowed_when_no_globs_granted(tmp_path, monkeypatch):
    """An agent with write_roots but no write_globs (e.g. sysadmin/developer, unrestricted
    within their roots) must not be caught by the glob-enforcement guard."""
    import yaml

    write_root = str(tmp_path / "repos" / "gitea")
    os.makedirs(write_root, exist_ok=True)
    policy_path = tmp_path / "workspace-policy.yml"
    with open(policy_path, "w") as f:
        yaml.safe_dump(
            {
                "version": 1,
                "roots": [{"path": write_root}],
                "default_read": "all",
                "agents": {"developer": {"write_roots": [write_root]}},
                "explicit_agents": {},
            },
            f,
        )

    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(policy_path))
    from githost_mcp.config import reset_config

    reset_config()

    repo_path = os.path.join(write_root, "somerepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_write_path(repo_path)  # should not raise


def test_readwrite_root_implies_read(manifest_allowed_env):
    """A root granted readwrite passes both validators, not just validate_write_path."""
    allowed, _tmp = manifest_allowed_env
    repo_path = os.path.join(allowed, "myrepo")
    os.makedirs(repo_path, exist_ok=True)
    validate_write_path(repo_path)  # should not raise
    validate_read_path(repo_path)  # should not raise


# --- write_globs enforcement (workspace-policy Phase 3, vikunja#349) ----------


def test_write_globs_no_scope_is_unrestricted():
    """No write_globs and no write_globs_deny configured (e.g. sysadmin/developer) ->
    every path passes. Absence of write_globs must not mean deny-everything."""
    from githost_mcp.config import reset_config

    reset_config()  # picks up autouse fixture defaults: no policy, no manifest
    validate_write_globs("/any/repo", ["src/anything.py", "docs/x.md"])  # should not raise


def test_write_globs_allow_match_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    validate_write_globs(str(tmp_path), ["docs/x.md"])  # should not raise


def test_write_globs_no_allow_match_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    with pytest.raises(WriteGlobDenied, match=r"src/x\.py"):
        validate_write_globs(str(tmp_path), ["src/x.py"])


def test_write_globs_deny_beats_allow(tmp_path, monkeypatch):
    """docs/AGENT_WORKSPACE.md matches the **/*.md allow glob, but the deny list must
    still win — without it, **/*.md would let writer edit the policy governing it."""
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(
        tmp_path,
        monkeypatch,
        write_globs=["**/*.md"],
        write_globs_deny=["**/AGENT_WORKSPACE.md"],
    )
    validate_write_globs(str(tmp_path), ["docs/readme.md"])  # allowed
    with pytest.raises(WriteGlobDenied, match=r"AGENT_WORKSPACE\.md"):
        validate_write_globs(str(tmp_path), ["docs/AGENT_WORKSPACE.md"])


def test_write_globs_mixed_set_denied_wholesale(tmp_path, monkeypatch):
    """One bad path in a batch rejects the whole call — callers must not be able to
    partially stage/commit around the scope by mixing in-scope and out-of-scope paths."""
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    with pytest.raises(WriteGlobDenied) as exc_info:
        validate_write_globs(str(tmp_path), ["docs/x.md", "src/x.py"])
    assert exc_info.value.denied_paths == ["src/x.py"]


def test_write_globs_traversal_denied_even_when_it_textually_matches_allow(tmp_path, monkeypatch):
    """Audit MEDIUM (githost-workspace-policy-2026-08 Phase 3): fnmatch has no
    path-segment awareness, so 'docs/../src/exploit.py' textually matches 'docs/**'
    ('**' is just wildcards matching '..' and '/' like any other characters) while
    resolving outside the declared scope entirely. Must be denied outright,
    independent of the glob match."""
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    with pytest.raises(WriteGlobDenied, match=r"docs/\.\./src/exploit\.py"):
        validate_write_globs(str(tmp_path), ["docs/../src/exploit.py"])


def test_write_globs_absolute_path_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    with pytest.raises(WriteGlobDenied):
        validate_write_globs(str(tmp_path), ["/etc/passwd"])


def test_write_globs_dot_segments_normalized_before_matching(tmp_path, monkeypatch):
    """A harmless './' segment must not spuriously fail matching post-normalization."""
    monkeypatch.setenv("AGENT_ID", "writer")
    _set_writer_policy(tmp_path, monkeypatch, write_globs=["docs/**"])
    validate_write_globs(str(tmp_path), ["docs/./x.md"])  # should not raise


def _set_writer_policy(tmp_path, monkeypatch, write_globs=None, write_globs_deny=None):
    """Load a workspace policy granting writer write_roots=[tmp_path] with the given
    glob scope, and reset config so it takes effect."""
    import yaml

    policy_path = tmp_path / "workspace-policy.yml"
    with open(policy_path, "w") as f:
        yaml.safe_dump(
            {
                "version": 1,
                "roots": [{"path": str(tmp_path)}],
                "default_read": "all",
                "agents": {
                    "writer": {
                        "write_roots": [str(tmp_path)],
                        "write_globs": write_globs or [],
                        "write_globs_deny": write_globs_deny or [],
                    }
                },
                "explicit_agents": {},
            },
            f,
        )
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(policy_path))
    from githost_mcp.config import reset_config

    reset_config()


# --- clean_env (GHOST-11) -----------------------------------------------------


def test_clean_env_strips_pm2_vars(monkeypatch):
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    monkeypatch.setenv("KEEP_ME", "yes")

    env = clean_env()

    for var in _PM2_IPC_ENV_VARS:
        assert var not in env
    assert env["KEEP_ME"] == "yes"


# ---------------------------------------------------------------------------
# SC-14 — credential scrubbing of caller-facing strings
# (githost-mcp-reliability-batch-2026-07 audit, MEDIUM)
# ---------------------------------------------------------------------------


def test_redact_url_credentials_strips_userinfo():
    from githost_mcp.security import redact_url_credentials

    assert (
        redact_url_credentials("failed to push to https://ted:ghp_SECRET123@github.com/o/r.git")
        == "failed to push to https://***@github.com/o/r.git"
    )
    # Token-as-username (no colon) is the common GitHub PAT remote form.
    assert (
        redact_url_credentials("https://ghp_SECRET123@github.com/o/r.git")
        == "https://***@github.com/o/r.git"
    )


def test_redact_url_credentials_leaves_scp_style_remotes_readable():
    """Every forge remote is scp-style and carries no credential — keep it diagnosable."""
    from githost_mcp.security import redact_url_credentials

    text = "failed to push to git@gitea.example-forge.test:host-forge/stacks.git"
    assert redact_url_credentials(text) == text


def test_scrub_catches_unconfigured_credential_that_mask_alone_misses(monkeypatch):
    """The audit's nuance: mask_credentials() only replaces *known configured* tokens,
    so a hand-added PAT in a remote survives it. scrub() must catch it by shape."""
    from githost_mcp.config import reset_config
    from githost_mcp.security import mask_credentials, scrub

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_CONFIGURED")
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()

    text = "remote: https://ted:ghp_HANDADDED_NOT_IN_CONFIG@github.com/o/r.git rejected"

    assert "ghp_HANDADDED_NOT_IN_CONFIG" in mask_credentials(text), (
        "precondition: mask_credentials alone does not catch an unconfigured token"
    )
    assert "ghp_HANDADDED_NOT_IN_CONFIG" not in scrub(text)
    assert "***@github.com" in scrub(text)


# ---------------------------------------------------------------------------
# Remote URL / name validation (git_remote, vikunja#189 id 200)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "http://gitea.internal/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git://github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "git@gitea.example-forge.test:host-forge/component-registry.git",
    ],
)
def test_validate_remote_url_accepts_supported_forms(url):
    validate_remote_url(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://user:token@github.com/owner/repo.git",
        "https://ghp_tokenonlynouser@github.com/owner/repo.git",
        "http://user:pw@host/r.git",
        "ssh://user:pw@host/r.git",
        "user:pw@github.com:owner/repo.git",
    ],
)
def test_validate_remote_url_refuses_embedded_credentials(url):
    with pytest.raises(RemoteUrlRejected):
        validate_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c 'id > /tmp/pwned'",
        "fd::7/repo",
        "file:///srv/repo.git",
        "/absolute/local/path",
        "./relative/path",
        "",
        "--upload-pack=evil",
        "-u",
    ],
)
def test_validate_remote_url_refuses_unsupported_or_option_shaped(url):
    with pytest.raises(RemoteUrlRejected):
        validate_remote_url(url)


def test_validate_remote_url_error_does_not_echo_the_credential():
    """The rejection message reaches the caller and the audit log — it must not
    carry the token it is rejecting."""
    with pytest.raises(RemoteUrlRejected) as excinfo:
        validate_remote_url("https://ghp_sekrit_token_value@github.com/o/r.git")
    assert "ghp_sekrit_token_value" not in str(excinfo.value)


@pytest.mark.parametrize("name", ["origin", "fork", "up-stream", "a.b_c", "team/fork"])
def test_validate_remote_name_accepts_plain_names(name):
    validate_remote_name(name)


@pytest.mark.parametrize("name", ["", "-u", "--upload-pack=evil", "has space", "semi;colon"])
def test_validate_remote_name_refuses_option_shaped_or_odd(name):
    with pytest.raises(ValueError):
        validate_remote_name(name)

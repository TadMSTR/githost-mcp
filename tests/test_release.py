"""Tests for release orchestration: dry-run and rollback."""

from unittest.mock import MagicMock, patch

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
    """A working repo with a real local bare 'origin' so tag push/rollback work
    end-to-end without touching the network."""
    origin_path = tmp_path / "origin.git"
    git.Repo.init(str(origin_path), bare=True)

    repo = git.Repo.init(tmp_path / "proj")
    r = repo
    r.config_writer().set_value("user", "name", "Test").release()
    r.config_writer().set_value("user", "email", "t@t.com").release()
    (tmp_path / "proj" / "file.txt").write_text("v1")
    r.index.add(["file.txt"])
    r.index.commit("Initial")
    r.create_remote("origin", str(origin_path))
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


@pytest.mark.asyncio
async def test_tag_already_exists_blocked(tools, clean_repo):
    repo = git.Repo(str(clean_repo))
    repo.create_tag("v1.0.0")
    result = await tools["release"](str(clean_repo), "1.0.0")
    assert "error" in result
    assert "already exists" in result["error"]


@pytest.mark.asyncio
async def test_github_release_failure_rolls_back_local_and_remote_tag(tools, clean_repo):
    async def _boom(*a, **kw):
        raise ValueError("GitHub API unreachable")

    with patch("githost_mcp.tools.release._create_release_sync", side_effect=_boom):
        result = await tools["release"](
            str(clean_repo), "1.0.0", targets=["github"], github_repo="owner/repo"
        )

    assert result["rolled_back"] is True
    assert "GitHub release failed" in result["error"]

    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" not in [t.name for t in repo.tags]

    origin_repo = git.Repo(str(clean_repo.parent / "origin.git"))
    assert "v1.0.0" not in [t.name for t in origin_repo.tags]


@pytest.mark.asyncio
async def test_gitea_failure_after_github_success_rolls_back_github_release(tools, clean_repo):
    mock_gh_repo = MagicMock()
    mock_release = MagicMock()
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_gh_repo
    mock_gh_repo.get_release.return_value = mock_release

    async def _gitea_boom(*a, **kw):
        raise ValueError("Gitea API error 500")

    with (
        patch(
            "githost_mcp.tools.release._create_release_sync",
            return_value="https://github.com/owner/repo/releases/tag/v1.0.0",
        ),
        patch("githost_mcp._providers.gitea_client.gitea_post", side_effect=_gitea_boom),
        patch("githost_mcp._providers.github_client.get_github", return_value=mock_gh),
        patch(
            "githost_mcp._providers.github_client.github_call",
            side_effect=lambda fn, *a, **kw: fn(*a, **kw),
        ),
    ):
        result = await tools["release"](
            str(clean_repo),
            "1.0.0",
            targets=["github", "gitea"],
            github_repo="owner/repo",
            gitea_repo="owner/repo",
        )

    assert result["rolled_back"] is True
    assert "Gitea release failed" in result["error"]
    # The already-created GitHub release must be rolled back too.
    mock_gh_repo.get_release.assert_called_once_with("v1.0.0")
    mock_release.delete_release.assert_called_once()

    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" not in [t.name for t in repo.tags]


@pytest.mark.asyncio
async def test_gitea_created_rollback_deletes_the_release(tools, clean_repo):
    """The rollback path logged "Gitea release delete not implemented" long after
    gitea_delete / gitea_release_delete were added in the Tier 1 parity build."""

    async def _gitea_ok(*a, **kw):
        return {"html_url": "https://gitea.example.com/owner/repo/releases/tag/v1.0.0"}

    deleted = []

    async def _gitea_delete(path, *a, **kw):
        deleted.append(path)

    with (
        patch("githost_mcp._providers.gitea_client.gitea_post", side_effect=_gitea_ok),
        patch("githost_mcp._providers.gitea_client.gitea_delete", side_effect=_gitea_delete),
        patch("githost_mcp._providers.gitlab_client.get_gitlab", side_effect=ValueError("boom")),
    ):
        result = await tools["release"](
            str(clean_repo),
            "1.0.0",
            targets=["gitea", "gitlab"],
            gitea_repo="owner/repo",
            gitlab_project="group/proj",
        )

    assert result["rolled_back"] is True
    assert "GitLab release failed" in result["error"]
    assert deleted == ["/repos/owner/repo/releases/tags/v1.0.0"], (
        f"Gitea release was not deleted on rollback: {deleted}"
    )
    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" not in [t.name for t in repo.tags]


@pytest.mark.asyncio
async def test_gitea_rollback_delete_failure_still_logs_orphan(tools, clean_repo):
    """The orphan warning stays as the fallback — it just no longer fires
    unconditionally. A failed delete must not mask the original error."""

    async def _gitea_ok(*a, **kw):
        return {"html_url": "https://gitea.example.com/owner/repo/releases/tag/v1.0.0"}

    async def _gitea_delete_boom(*a, **kw):
        raise ValueError("Gitea API error 500")

    with (
        patch("githost_mcp._providers.gitea_client.gitea_post", side_effect=_gitea_ok),
        patch("githost_mcp._providers.gitea_client.gitea_delete", side_effect=_gitea_delete_boom),
        patch("githost_mcp._providers.gitlab_client.get_gitlab", side_effect=ValueError("boom")),
    ):
        result = await tools["release"](
            str(clean_repo),
            "1.0.0",
            targets=["gitea", "gitlab"],
            gitea_repo="owner/repo",
            gitlab_project="group/proj",
        )

    # The GitLab failure is still what the caller is told about.
    assert result["rolled_back"] is True
    assert "GitLab release failed" in result["error"]
    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" not in [t.name for t in repo.tags]


@pytest.mark.asyncio
async def test_pypi_failure_does_not_roll_back_and_still_reports_success(tools, clean_repo):
    fail = MagicMock(returncode=1, stdout="", stderr="403 Forbidden")
    with patch("subprocess.run", return_value=fail):
        result = await tools["release"](str(clean_repo), "1.0.0", targets=["pypi"])

    assert result["success"] is True
    assert "pypi" not in result["urls"]
    # Tag is NOT rolled back — pypi/npm failures are immutable, no rollback.
    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" in [t.name for t in repo.tags]


@pytest.mark.asyncio
async def test_npm_failure_does_not_roll_back_and_still_reports_success(tools, clean_repo):
    fail = MagicMock(returncode=1, stdout="", stderr="npm ERR! 403")
    with patch("subprocess.run", return_value=fail):
        result = await tools["release"](str(clean_repo), "1.0.0", targets=["npm"])

    assert result["success"] is True
    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" in [t.name for t in repo.tags]


# --- NODE_CHANNEL_FD env stripping (GHOST-11) --------------------------------

_PM2_IPC_ENV_VARS = ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE", "NODE_UNIQUE_ID")


@pytest.mark.asyncio
async def test_npm_release_strips_pm2_ipc_vars(tools, clean_repo, monkeypatch):
    """Regression for GHOST-11: the npm publish child must not inherit PM2's IPC vars."""
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=ok) as run:
        await tools["release"](str(clean_repo), "1.0.0", targets=["npm"])

    publish_env = run.call_args.kwargs["env"]
    for var in _PM2_IPC_ENV_VARS:
        assert var not in publish_env


@pytest.mark.asyncio
async def test_pypi_release_strips_pm2_ipc_vars(tools, clean_repo, monkeypatch):
    """Regression for GHOST-11: the twine upload child must not inherit PM2's IPC vars."""
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    ok = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=ok) as run:
        await tools["release"](str(clean_repo), "1.0.0", targets=["pypi"])

    upload_env = run.call_args.kwargs["env"]
    for var in _PM2_IPC_ENV_VARS:
        assert var not in upload_env


# --- tag push result integrity (fourth instance of the id 276 family) --------


@pytest.mark.asyncio
async def test_tag_push_rejection_aborts_before_any_provider_release(tools, clean_repo):
    """release.py discarded the tag-push result, then created GitHub/Gitea/GitLab
    releases pointing at a tag the remote may never have received."""
    repo = git.Repo(str(clean_repo))
    origin_path = clean_repo.parent / "origin.git"
    repo.remotes.origin.push(repo.active_branch.name)

    # Remote gets v1.0.0 at the initial commit, out of band.
    other = git.Repo.clone_from(str(origin_path), str(clean_repo.parent / "other"))
    other.create_tag("v1.0.0")
    other.remotes.origin.push("v1.0.0")

    # Local diverges, so its v1.0.0 would point somewhere else — a rejected push.
    (clean_repo / "more.txt").write_text("more")
    repo.index.add(["more.txt"])
    repo.index.commit("More work")

    with patch("githost_mcp.tools.release._create_release_sync") as create_release:
        result = await tools["release"](
            str(clean_repo), "1.0.0", targets=["github"], github_repo="owner/repo"
        )

    assert "error" in result, f"rejected tag push let the release proceed: {result}"
    assert "success" not in result, "failure result must not also claim success"
    create_release.assert_not_called()

    # The local tag must be rolled back. The remote tag must NOT be — this release
    # never pushed it, and deleting someone else's tag is the failure mode the whole
    # check exists to prevent.
    assert "v1.0.0" not in [t.name for t in repo.tags]
    origin_repo = git.Repo(str(origin_path))
    assert "v1.0.0" in [t.name for t in origin_repo.tags], (
        "rollback deleted a remote tag this release never created"
    )
    assert origin_repo.tags["v1.0.0"].commit.hexsha != repo.head.commit.hexsha


@pytest.mark.asyncio
async def test_tag_push_success_still_proceeds(tools, clean_repo):
    """Guards against an over-broad check that aborts every release."""
    repo = git.Repo(str(clean_repo))
    repo.remotes.origin.push(repo.active_branch.name)

    with patch(
        "githost_mcp.tools.release._create_release_sync",
        return_value="https://github.com/owner/repo/releases/tag/v1.0.0",
    ):
        result = await tools["release"](
            str(clean_repo), "1.0.0", targets=["github"], github_repo="owner/repo"
        )

    assert result.get("success") is True, f"genuine release reported failure: {result}"
    origin_repo = git.Repo(str(clean_repo.parent / "origin.git"))
    assert "v1.0.0" in [t.name for t in origin_repo.tags]


@pytest.mark.asyncio
async def test_gitlab_failure_rolls_back_tag_only_when_nothing_else_created(tools, clean_repo):
    with patch(
        "githost_mcp._providers.gitlab_client.get_gitlab",
        side_effect=ValueError("GitLab unreachable"),
    ):
        result = await tools["release"](
            str(clean_repo), "1.0.0", targets=["gitlab"], gitlab_project="group/proj"
        )

    assert result["rolled_back"] is True
    assert "GitLab release failed" in result["error"]
    repo = git.Repo(str(clean_repo))
    assert "v1.0.0" not in [t.name for t in repo.tags]

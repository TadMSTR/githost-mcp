"""Tests for local git tools using real temporary repositories."""

import pathlib

import git
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "test")
    reset_config()
    init_logging()


@pytest.fixture()
def repo_path(tmp_path):
    repo = git.Repo.init(tmp_path / "repo")
    r = repo
    r.config_writer().set_value("user", "name", "Test").release()
    r.config_writer().set_value("user", "email", "t@test.com").release()
    (tmp_path / "repo" / "file.txt").write_text("hello")
    r.index.add(["file.txt"])
    r.index.commit("Initial")
    return tmp_path / "repo"


# Import tool functions via the register pattern
@pytest.fixture()
def tools(repo_path, monkeypatch):
    """Returns a dict of tool callables by registering on a mock mcp."""
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.git_local import register

    register(MockMCP())
    return registered, repo_path


def test_git_status(tools):
    fns, path = tools
    result = fns["git_status"](str(path))
    assert "branch" in result
    assert "staged" in result
    assert result["is_dirty"] is False


def test_git_log(tools):
    fns, path = tools
    result = fns["git_log"](str(path))
    assert len(result["commits"]) >= 1
    assert result["commits"][0]["message"] == "Initial"


def test_git_diff_clean(tools):
    fns, path = tools
    result = fns["git_diff"](str(path))
    assert result["patches"] == []


def test_git_show(tools):
    fns, path = tools
    result = fns["git_show"](str(path), "HEAD")
    assert "sha" in result
    assert result["message"] == "Initial"


def test_git_branch_list(tools):
    fns, path = tools
    result = fns["git_branch"](str(path), action="list")
    assert "branches" in result
    assert "active" in result


def test_git_branch_create_delete(tools):
    fns, path = tools
    result = fns["git_branch"](str(path), action="create", branch_name="feature-x")
    assert result.get("created") == "feature-x"
    result = fns["git_branch"](str(path), action="delete", branch_name="feature-x")
    assert result.get("deleted") == "feature-x"


def test_git_add_and_commit(tools, tmp_path):
    fns, path = tools
    (path / "new.txt").write_text("new file")
    add_result = fns["git_add"](str(path), ["new.txt"])
    assert "staged" in add_result
    commit_result = fns["git_commit"](str(path), "Add new file")
    assert "sha" in commit_result


def test_git_commit_appends_agent_id(tools, tmp_path):
    fns, path = tools
    (path / "agent.txt").write_text("by agent")
    fns["git_add"](str(path), ["agent.txt"])
    fns["git_commit"](str(path), "Agent commit")
    import git as gitmod

    repo = gitmod.Repo(str(path))
    msg = repo.head.commit.message
    assert "agent-id: test" in msg


def test_git_tag(tools):
    fns, path = tools
    result = fns["git_tag"](str(path), "v0.1.0", message="Test tag")
    assert result.get("tag") == "v0.1.0"
    assert "sha" in result


def test_write_blocked_outside_allowed_roots(tools, tmp_path, monkeypatch):
    fns, path = tools
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "/nonexistent/root")
    reset_config()
    result = fns["git_add"](str(path), ["file.txt"])
    assert "error" in result


def test_read_blocked_outside_allowed_roots(tools, tmp_path, monkeypatch):
    fns, path = tools
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", "/nonexistent/root")
    reset_config()
    for fn_name in ("git_status", "git_log", "git_diff", "git_show"):
        kwargs = {"repo_path": str(path)}
        if fn_name == "git_show":
            kwargs["ref"] = "HEAD"
        result = fns[fn_name](**kwargs)
        assert "error" in result, f"{fn_name} should be blocked outside allowed roots"
    result = fns["git_branch"](str(path), action="list")
    assert "error" in result, "git_branch list should be blocked outside allowed roots"


def test_git_log_limit_capped(tools):
    fns, path = tools
    result = fns["git_log"](str(path), limit=9999)
    # Repo has 1 commit; verify the call didn't error (cap applied internally)
    assert "commits" in result
    assert len(result["commits"]) <= 200


def test_git_commit_agent_identity(tools, tmp_path, monkeypatch):
    fns, path = tools
    monkeypatch.setenv("GIT_AGENT_NAME", "sysadmin-agent")
    monkeypatch.setenv("GIT_AGENT_EMAIL", "sysadmin@forge")
    reset_config()
    (path / "id_test.txt").write_text("identity")
    fns["git_add"](str(path), ["id_test.txt"])
    fns["git_commit"](str(path), "Identity test commit")
    repo = git.Repo(str(path))
    commit = repo.head.commit
    assert commit.author.name == "sysadmin-agent"
    assert commit.author.email == "sysadmin@forge"
    assert commit.committer.name == "sysadmin-agent"
    assert commit.committer.email == "sysadmin@forge"


def test_git_commit_agent_id_default_identity(tools, tmp_path, monkeypatch):
    """When GIT_AGENT_NAME/EMAIL not set, AGENT_ID derives defaults."""
    fns, path = tools
    monkeypatch.delenv("GIT_AGENT_NAME", raising=False)
    monkeypatch.delenv("GIT_AGENT_EMAIL", raising=False)
    monkeypatch.setenv("AGENT_ID", "developer")
    reset_config()
    (path / "default_id.txt").write_text("default")
    fns["git_add"](str(path), ["default_id.txt"])
    fns["git_commit"](str(path), "Default identity commit")
    repo = git.Repo(str(path))
    commit = repo.head.commit
    assert commit.author.name == "developer-agent"
    assert commit.author.email == "developer@forge"


# ---------------------------------------------------------------------------
# git_push result integrity (vikunja #265, id 276)
#
# These run against a real local bare remote — a mocked PushInfo would happily
# accept whatever shape the code produces, which is how the bug shipped.
# ---------------------------------------------------------------------------


def _commit_file(repo: git.Repo, name: str, content: str) -> None:
    (pathlib.Path(repo.working_tree_dir) / name).write_text(content)
    repo.index.add([name])
    repo.index.commit(f"Add {name}")


@pytest.fixture()
def bare_remote(tmp_path, repo_path):
    """A bare repo wired up as `origin` for repo_path, with the branch already pushed."""
    bare = git.Repo.init(tmp_path / "remote.git", bare=True)
    local = git.Repo(str(repo_path))
    origin = local.create_remote("origin", str(tmp_path / "remote.git"))
    branch = local.active_branch.name
    origin.push(branch)
    return bare, branch


def test_git_push_success_reports_pushed(tools, bare_remote):
    """A genuine push must still report success — guards against a filter that
    reports failure for everything and passes a one-sided rejection test."""
    fns, path = tools
    _bare, branch = bare_remote
    local = git.Repo(str(path))
    _commit_file(local, "pushed.txt", "new work")

    result = fns["git_push"](str(path), branch=branch)

    assert "error" not in result, f"genuine push reported failure: {result}"
    assert result["pushed"] == branch


def test_git_push_rejected_reports_failure(tools, bare_remote):
    """A non-fast-forward rejection must report failure, not {'pushed': ...}."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))

    # Advance the remote out of band via a second clone, then diverge locally.
    other = git.Repo.clone_from(bare.git_dir, str(path.parent / "other"))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    _commit_file(other, "theirs.txt", "remote work")
    other.remotes.origin.push(branch)

    _commit_file(local, "ours.txt", "local work")

    result = fns["git_push"](str(path), branch=branch)

    assert "error" in result, f"rejected push reported success: {result}"
    assert "pushed" not in result, "failure result must not also claim a push landed"
    # The remote must genuinely not have moved to our commit.
    assert bare.refs[branch].commit.hexsha != local.head.commit.hexsha


def test_git_push_rejection_surfaces_reason(tools, bare_remote):
    """PushInfo.summary holds the human-readable reason; losing it is why the
    current failure mode is undiagnosable. Decoded flags must be present too."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))

    other = git.Repo.clone_from(bare.git_dir, str(path.parent / "other2"))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    _commit_file(other, "theirs.txt", "remote work")
    other.remotes.origin.push(branch)

    _commit_file(local, "ours.txt", "local work")

    result = fns["git_push"](str(path), branch=branch)

    assert "REJECTED" in result.get("flags", []), (
        f"decoded flags must name REJECTED, got {result.get('flags')}"
    )
    assert result.get("summary"), "PushInfo.summary must be surfaced, not discarded"


def test_git_push_failure_does_not_leak_remote_url_credentials(tools, bare_remote, monkeypatch):
    """SC-14: PushInfo.summary is the remote's raw text. If the remote URL carries a
    credential, it must not reach the caller. Audit finding, MEDIUM."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))

    other = git.Repo.clone_from(bare.git_dir, str(path.parent / "other3"))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    _commit_file(other, "theirs.txt", "remote work")
    other.remotes.origin.push(branch)

    _commit_file(local, "ours.txt", "local work")

    # Simulate the remote's rejection text naming a credential-bearing URL.
    import githost_mcp.tools.git_local as gl

    real_scrub = gl.scrub
    leaked = "https://ted:ghp_LEAKED_TOKEN@github.com/o/r.git"

    class FakeInfo:
        flags = git.remote.PushInfo.REJECTED | git.remote.PushInfo.ERROR
        summary = f"[rejected] main -> main (non-fast-forward) to {leaked}"

    monkeypatch.setattr(
        type(local.remotes[0]), "push", lambda self, *a, **kw: [FakeInfo()], raising=False
    )
    monkeypatch.setattr(gl, "scrub", real_scrub)

    result = fns["git_push"](str(path), branch=branch)

    assert "error" in result
    blob = repr(result)
    assert "ghp_LEAKED_TOKEN" not in blob, f"credential reached the caller: {blob}"
    assert "***@github.com" in blob, f"expected redacted userinfo, got: {blob}"


def test_git_push_sets_upstream(tools, bare_remote):
    """A push that leaves no upstream makes `git rev-list @{u}..HEAD` error rather
    than confirm. Either set upstream or say it wasn't set."""
    fns, path = tools
    _bare, _branch = bare_remote
    local = git.Repo(str(path))
    local.create_head("feature-upstream").checkout()
    _commit_file(local, "feature.txt", "feature work")

    result = fns["git_push"](str(path), branch="feature-upstream")

    assert "error" not in result, f"push failed: {result}"
    assert result.get("upstream_set") is True, f"upstream not reported as set: {result}"
    assert local.active_branch.tracking_branch() is not None, (
        "tracking branch not configured — downstream `@{u}` verification will error"
    )

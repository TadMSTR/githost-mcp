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


# ---------------------------------------------------------------------------
# git_branch / git_push result honesty (vikunja #289, id 300)
#
# Two defects that compounded into "work silently did not land while every tool
# call reported success" during venv-install-standardization-2026-07.
# ---------------------------------------------------------------------------


def test_git_branch_create_reports_unchanged_active_branch(tools):
    """create does not check out. Nothing in the old return value said so, and a
    following git_commit therefore landed on the wrong branch."""
    fns, path = tools
    local = git.Repo(str(path))
    before = local.active_branch.name

    result = fns["git_branch"](str(path), action="create", branch_name="feature-x")

    assert result["created"] == "feature-x"
    assert result["active_branch"] == before, (
        "create must report the branch the repo is actually on"
    )
    assert local.active_branch.name == before, "create must not silently check out"


def test_git_branch_list_still_reports_active(tools):
    fns, path = tools
    result = fns["git_branch"](str(path), action="list")
    assert result["active"] == git.Repo(str(path)).active_branch.name


def test_git_push_returns_pushed_sha(tools, bare_remote):
    """A caller could not distinguish "pushed your work" from "pushed a branch that
    does not contain your work"."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))
    _commit_file(local, "work.txt", "real work")

    result = fns["git_push"](str(path), branch=branch)

    assert "error" not in result
    assert result["pushed_sha"] == local.head.commit.hexsha
    assert bare.refs[branch].commit.hexsha == result["pushed_sha"], (
        "pushed_sha must match what the remote actually now holds"
    )


def test_git_push_sha_exposes_a_push_that_omits_your_commit(tools, bare_remote):
    """The id 300 scenario exactly: the push succeeds, but the branch pushed sits at
    an older tip than HEAD. pushed_sha != HEAD is the caller's only signal."""
    fns, path = tools
    _bare, branch = bare_remote
    local = git.Repo(str(path))

    # A feature branch is created at the current tip but never checked out...
    stale_tip = local.head.commit.hexsha
    local.create_head("feature-stale")
    # ...and the real work lands on the original branch instead.
    _commit_file(local, "elsewhere.txt", "the commit that matters")
    assert local.active_branch.name == branch
    assert local.head.commit.hexsha != stale_tip

    result = fns["git_push"](str(path), branch="feature-stale")

    assert "error" not in result, f"push genuinely succeeded, should not error: {result}"
    assert result["pushed_sha"] == stale_tip
    assert result["pushed_sha"] != local.head.commit.hexsha, (
        "the caller must be able to see the pushed branch omits the new commit"
    )


# ---------------------------------------------------------------------------
# git_pull result integrity (vikunja #274, id 285)
#
# git_pull returned {"flags": ["4"]} — a stringified bitmask, no error check, and
# FetchInfo.note discarded. Note the realistic failure modes (diverged history,
# clobbering tags) make GitPython raise rather than return error-flagged
# FetchInfo; the mask itself is covered as a unit in tests/test_gitflags.py.
# ---------------------------------------------------------------------------


def test_git_pull_success_decodes_flags(tools, bare_remote):
    """Flags must be names, not a stringified bitmask — "4" tells a caller nothing."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))

    other = git.Repo.clone_from(bare.git_dir, str(path.parent / "puller"))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    _commit_file(other, "theirs.txt", "remote work")
    other.remotes.origin.push(branch)

    # `git pull <remote>` needs an upstream to know which branch to merge.
    local.remotes.origin.fetch()
    local.heads[branch].set_tracking_branch(local.remotes.origin.refs[branch])

    result = fns["git_pull"](str(path))

    assert "error" not in result, f"clean pull reported failure: {result}"
    assert result["flags"], "flags must not be empty for a pull that moved the branch"
    for flag in result["flags"]:
        assert not flag.isdigit(), f"flags must be decoded names, got raw bitmask {flag!r}"
    assert (path / "theirs.txt").exists(), "the pull did not actually land"


def test_git_pull_diverged_reports_error(tools, bare_remote):
    """A pull that cannot merge must report an error, not report ok."""
    fns, path = tools
    bare, branch = bare_remote
    local = git.Repo(str(path))

    other = git.Repo.clone_from(bare.git_dir, str(path.parent / "divergent"))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    _commit_file(other, "file.txt", "remote edit")
    other.remotes.origin.push(branch)

    _commit_file(local, "file.txt", "local edit")

    result = fns["git_pull"](str(path))

    assert "error" in result, f"unmergeable pull reported success: {result}"
    assert "remote" not in result or "flags" not in result, (
        "failure result must not also carry a success-shaped payload"
    )


def test_git_pull_error_flags_report_failure(tools, bare_remote, monkeypatch):
    """Defence in depth: if git ever returns error-flagged FetchInfo without
    raising, the tool must not report ok."""
    fns, path = tools
    local = git.Repo(str(path))

    class FakeFetchInfo:
        flags = git.remote.FetchInfo.ERROR
        note = "would clobber existing tag"

    monkeypatch.setattr(
        type(local.remotes[0]), "pull", lambda self, *a, **kw: [FakeFetchInfo()], raising=False
    )

    result = fns["git_pull"](str(path))

    assert "error" in result, f"error-flagged fetch reported success: {result}"
    assert "would clobber existing tag" in result["error"], (
        "FetchInfo.note carries the reason and must be surfaced"
    )


# ---------------------------------------------------------------------------
# git_tag push result integrity — third instance of the id 276 family.
#
# git_tag(push=True) discarded the PushInfo entirely and set "pushed": True
# unconditionally. Same real-bare-remote treatment as git_push above.
# ---------------------------------------------------------------------------


def _tag_remote_out_of_band(bare, path, clone_name: str, tag_name: str) -> None:
    """Put `tag_name` on the bare remote from a separate clone, so a local tag of
    the same name at a different commit will be rejected."""
    other = git.Repo.clone_from(bare.git_dir, str(path.parent / clone_name))
    other.config_writer().set_value("user", "name", "Other").release()
    other.config_writer().set_value("user", "email", "o@test.com").release()
    other.create_tag(tag_name)
    other.remotes.origin.push(tag_name)


def test_git_tag_push_success_reports_pushed(tools, bare_remote):
    """A genuine tag push must still report success — guards against a check that
    fails everything and passes a one-sided rejection test."""
    fns, path = tools
    bare, _branch = bare_remote

    result = fns["git_tag"](str(path), "v1.0.0", message="Release", push=True)

    assert "error" not in result, f"genuine tag push reported failure: {result}"
    assert result["pushed"] is True
    assert "v1.0.0" in [t.name for t in bare.tags], "tag did not reach the remote"


def test_git_tag_push_rejected_reports_failure(tools, bare_remote):
    """A tag the remote already holds at a different sha is rejected. The tool must
    not claim {"pushed": True}."""
    fns, path = tools
    bare, _branch = bare_remote
    local = git.Repo(str(path))

    _tag_remote_out_of_band(bare, path, "tagother", "v1.0.0")
    _commit_file(local, "ours.txt", "local work")

    result = fns["git_tag"](str(path), "v1.0.0", push=True)

    assert "error" in result, f"rejected tag push reported success: {result}"
    assert "pushed" not in result, "failure result must not also claim a push landed"
    # The remote genuinely still points at the old commit.
    assert bare.tags["v1.0.0"].commit.hexsha != local.head.commit.hexsha


def test_git_tag_push_failure_reports_local_tag_needs_cleanup(tools, bare_remote):
    """The local tag is already created when the push fails. The caller has to know
    that, or it is left with local state the remote does not have."""
    fns, path = tools
    bare, _branch = bare_remote
    local = git.Repo(str(path))

    _tag_remote_out_of_band(bare, path, "tagother2", "v2.0.0")
    _commit_file(local, "ours.txt", "local work")

    result = fns["git_tag"](str(path), "v2.0.0", push=True)

    assert "error" in result
    assert "v2.0.0" in [t.name for t in local.tags], "precondition: local tag was created"
    assert result.get("local_tag_created") is True, (
        f"caller cannot know the local tag needs cleanup: {result}"
    )
    assert "REJECTED" in result.get("flags", []), (
        f"decoded flags must name REJECTED, got {result.get('flags')}"
    )


def test_git_tag_without_push_unchanged(tools):
    """push=False must keep its existing shape — no pushed key, no error."""
    fns, path = tools
    result = fns["git_tag"](str(path), "v0.9.9", message="Local only")
    assert result["tag"] == "v0.9.9"
    assert "pushed" not in result
    assert "error" not in result


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


# ---------------------------------------------------------------------------
# write_globs enforcement in git_add / git_commit (workspace-policy Phase 3,
# vikunja#349). Unit coverage of validate_write_globs() itself lives in
# tests/test_security.py — these exercise it wired into the actual tools.
# ---------------------------------------------------------------------------


@pytest.fixture()
def writer_tools(repo_path, tmp_path, monkeypatch):
    """Like `tools`, but resolves the writer identity through a workspace policy
    granting write_roots=[tmp_path], write_globs=["docs/**"], and
    write_globs_deny=["**/AGENT_WORKSPACE.md"] — mirrors writer's real grant shape."""
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
                        "write_globs": ["docs/**"],
                        "write_globs_deny": ["**/AGENT_WORKSPACE.md"],
                    }
                },
                "explicit_agents": {},
            },
            f,
        )
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "writer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(policy_path))
    reset_config()

    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.git_local import register

    register(MockMCP())
    return registered, repo_path


def test_git_add_allowed_within_write_glob(writer_tools):
    fns, path = writer_tools
    (path / "docs").mkdir()
    (path / "docs" / "x.md").write_text("docs")
    result = fns["git_add"](str(path), ["docs/x.md"])
    assert "error" not in result, f"in-scope path denied: {result}"
    assert "docs/x.md" in result["staged"]


def test_git_add_denied_outside_write_glob(writer_tools):
    fns, path = writer_tools
    (path / "src").mkdir()
    (path / "src" / "x.py").write_text("code")
    result = fns["git_add"](str(path), ["src/x.py"])
    assert "error" in result, "out-of-scope path must be denied"
    assert "src/x.py" in result["error"]


def test_git_add_denied_by_deny_list_beating_allow(writer_tools):
    """docs/AGENT_WORKSPACE.md matches the docs/** allow glob but must still be
    denied — without the deny list, writer could edit the policy governing it."""
    fns, path = writer_tools
    (path / "docs").mkdir()
    (path / "docs" / "AGENT_WORKSPACE.md").write_text("policy")
    result = fns["git_add"](str(path), ["docs/AGENT_WORKSPACE.md"])
    assert "error" in result
    assert "AGENT_WORKSPACE.md" in result["error"]


def test_git_commit_allowed_within_write_glob(writer_tools):
    fns, path = writer_tools
    (path / "docs").mkdir()
    (path / "docs" / "x.md").write_text("docs")
    fns["git_add"](str(path), ["docs/x.md"])
    result = fns["git_commit"](str(path), "docs update")
    assert "sha" in result, f"in-scope commit denied: {result}"


def test_git_commit_enforces_full_staged_set_not_just_last_git_add(writer_tools):
    """git_commit must reject based on everything staged, not just the paths the most
    recent git_add call itself validated — closes the bypass the plan calls out:
    git_commit commits whatever is staged regardless of what staged it."""
    fns, path = writer_tools
    (path / "docs").mkdir()
    (path / "docs" / "x.md").write_text("docs")
    fns["git_add"](str(path), ["docs/x.md"])  # allowed, stages fine

    (path / "src").mkdir()
    (path / "src" / "x.py").write_text("code")
    # Stage the out-of-scope file through the repo directly, simulating any staging
    # path other than this tool's own git_add call.
    local = git.Repo(str(path))
    local.git.add("--", "src/x.py")

    result = fns["git_commit"](str(path), "mixed commit")
    assert "error" in result, (
        f"mixed staged set with an out-of-scope path must be rejected: {result}"
    )
    assert "src/x.py" in result["error"]
    # Nothing must have been committed — rejection is wholesale, not partial.
    assert local.head.commit.message.strip() == "Initial"


def test_git_commit_unrestricted_for_agent_without_write_globs(tools):
    """sysadmin/developer-style agents (env-sourced roots, no write_globs) are
    unaffected by glob enforcement — the `tools` fixture uses ALLOWED_REPO_ROOTS."""
    fns, path = tools
    (path / "anything.py").write_text("code")
    fns["git_add"](str(path), ["anything.py"])
    result = fns["git_commit"](str(path), "unrestricted commit")
    assert "sha" in result


@pytest.fixture()
def writer_tools_fresh_repo(tmp_path, monkeypatch):
    """Like `writer_tools`, but the repo has no commits yet — exercises
    `_staged_paths()`'s `repo.index.entries` fallback (used when `repo.head.is_valid()`
    is False) instead of the `index.diff("HEAD")` branch every other fixture in this
    file goes through, since `repo_path`/`writer_tools` both pre-seed an initial commit.
    """
    import yaml

    repo_dir = tmp_path / "fresh"
    repo = git.Repo.init(repo_dir)
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "t@test.com").release()

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
                        "write_globs": ["docs/**"],
                        "write_globs_deny": ["**/AGENT_WORKSPACE.md"],
                    }
                },
                "explicit_agents": {},
            },
            f,
        )
    monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
    monkeypatch.setenv("AGENT_ID", "writer")
    monkeypatch.setenv("WORKSPACE_POLICY_PATH", str(policy_path))
    reset_config()

    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.git_local import register

    register(MockMCP())
    return registered, repo_dir


def test_git_commit_initial_commit_enforces_write_globs(writer_tools_fresh_repo):
    """Audit LOW (githost-workspace-policy-2026-08 Phase 3): `_staged_paths()`'s
    `repo.index.entries` fallback — used on a repo's very first commit, before any HEAD
    exists — had zero test coverage. Confirms it enforces scope correctly on the
    initial commit, not just subsequent ones."""
    fns, path = writer_tools_fresh_repo
    (path / "docs").mkdir()
    (path / "docs" / "x.md").write_text("docs")
    (path / "src").mkdir()
    (path / "src" / "y.py").write_text("code")

    local = git.Repo(str(path))
    local.git.add("--", "docs/x.md", "src/y.py")

    result = fns["git_commit"](str(path), "initial commit")
    assert "error" in result, (
        f"out-of-scope path in the initial commit must be denied: {result}"
    )
    assert "src/y.py" in result["error"]
    assert not local.head.is_valid(), "the denied initial commit must not have landed"

"""git_worktree, git_push_force_with_lease and `~` paths, against real repositories.

Each test gets a bare `origin` with `main` and `feat`, and a clone of it inside the
allowed root. Worktrees land under GITHOST_WORKTREE_ROOT, also inside the root.
"""

import json
import os
from unittest.mock import patch

import git
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config
from githost_mcp.errors import InvalidArgument, MissingArgument
from githost_mcp.tools.git_local import (
    parse_worktree_porcelain,
    protected_branch_reason,
    remote_default_branch,
    validate_expected_sha,
    validate_ref_arg,
    validate_worktree_name,
    worktree_destination,
)


def _commit(repo: git.Repo, message: str) -> str:
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@t")
    repo.git.commit("--allow-empty", "-m", message)
    return repo.head.commit.hexsha


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(root))
    monkeypatch.setenv("GITHOST_WORKTREE_ROOT", str(root / "_worktrees"))
    monkeypatch.setenv("AGENT_ID", "test")
    # CI runners have no global git identity; forge does, which hid this locally.
    for var, value in (
        ("GIT_AUTHOR_NAME", "T"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "T"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
    ):
        monkeypatch.setenv(var, value)

    origin = git.Repo.init(tmp_path / "origin.git", bare=True, initial_branch="main")
    seed = git.Repo.clone_from(origin.git_dir, tmp_path / "seed")
    _commit(seed, "initial")
    seed.git.push("origin", "HEAD:refs/heads/main")
    seed.git.checkout("-b", "feat")
    _commit(seed, "feature")
    seed.git.push("origin", "feat")

    clone = git.Repo.clone_from(origin.git_dir, root / "proj")
    clone.git.checkout("feat")
    reset_config()
    init_logging()
    yield {"root": root, "clone": clone, "path": str(root / "proj"), "origin": origin}
    reset_config()


@pytest.fixture()
def tools(env):
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.git_local import register

    register(MockMCP())
    return registered


def _entries():
    with open(os.environ["AUDIT_LOG_FILE"]) as f:
        return [json.loads(line) for line in f if line.strip()]


# --- helpers -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "fix-1", "p4.scratch", "x" * 64])
def test_worktree_name_accepted(name):
    assert validate_worktree_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["-x", "a/b", "../up", "a..b", "UPPER", "x" * 65, ".hidden", "a b", "_x", "scratch\n"],
)
def test_worktree_name_refused(name):
    with pytest.raises(InvalidArgument):
        validate_worktree_name(name)


@pytest.mark.parametrize("name", [None, ""])
def test_worktree_name_missing(name):
    with pytest.raises(MissingArgument):
        validate_worktree_name(name)


def test_destination_is_root_repo_name(tmp_path):
    assert worktree_destination(str(tmp_path), "proj", "x") == str(tmp_path / "proj-x")


def test_destination_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert worktree_destination("~/wt", "proj", "x") == str(tmp_path / "wt" / "proj-x")


@pytest.mark.parametrize("value", ["-x", "--upload-pack=evil", "a b", "a\tb", "", "a\x00b"])
def test_ref_arg_refused(value):
    with pytest.raises(InvalidArgument):
        validate_ref_arg("base", value)


@pytest.mark.parametrize("value", ["origin/main", "v1.2.0", "a" * 40, "HEAD~1"])
def test_ref_arg_accepted(value):
    assert validate_ref_arg("base", value) == value


def test_expected_sha_accepts_full_and_lowercases():
    assert validate_expected_sha("A" * 40) == "a" * 40


@pytest.mark.parametrize(
    "sha", ["a" * 39, "a" * 41, "g" * 40, "abc1234", " " + "a" * 39, "a" * 40 + "\n"]
)
def test_expected_sha_refused(sha):
    with pytest.raises(InvalidArgument):
        validate_expected_sha(sha)


@pytest.mark.parametrize("sha", [None, ""])
def test_expected_sha_missing(sha):
    with pytest.raises(MissingArgument):
        validate_expected_sha(sha)


@pytest.mark.parametrize(
    "branch,default,refused",
    [
        ("main", None, True),
        ("master", None, True),
        ("main", "trunk", True),
        ("trunk", "trunk", True),
        ("feat", "main", False),
        ("feat", None, False),
        ("main-fix", "main", False),
    ],
)
def test_protected_branch_reason(branch, default, refused):
    assert (protected_branch_reason(branch, default) is not None) is refused


def test_parse_porcelain():
    text = (
        "worktree /r\nHEAD aaa\nbranch refs/heads/main\n\n"
        "worktree /w/r-x\nHEAD bbb\ndetached\n\n"
        "worktree /w/r-y\nHEAD ccc\nbranch refs/heads/y\nlocked reason here\n"
    )
    parsed = parse_worktree_porcelain(text)
    assert parsed[0] == {"path": "/r", "head": "aaa", "branch": "main"}
    assert parsed[1]["detached"] is True and parsed[1]["branch"] is None
    assert parsed[2]["locked"] == "reason here"


def test_default_branch_from_remote_when_no_local_symref(env):
    clone = env["clone"]
    clone.git.symbolic_ref("-d", "refs/remotes/origin/HEAD")
    assert remote_default_branch(clone, "origin") == "main"


def test_default_branch_falls_back_to_local_symref_when_remote_unreachable(env):
    clone = env["clone"]
    err = git.GitCommandError("ls-remote", 128)
    with patch.object(type(clone.git), "ls_remote", side_effect=err):
        assert remote_default_branch(clone, "origin") == "main"


def test_default_branch_rename_beats_a_stale_local_symref(env):
    """`git fetch` never updates origin/HEAD, so after a rename the local value is stale."""
    env["origin"].git.symbolic_ref("HEAD", "refs/heads/feat")
    assert env["clone"].git.symbolic_ref("refs/remotes/origin/HEAD").endswith("/main")
    assert remote_default_branch(env["clone"], "origin") == "feat"


# --- git_worktree ----------------------------------------------------------------


def test_worktree_add_commit_push_remove(tools, env):
    out = tools["git_worktree"](env["path"], "add", name="scratch", branch="wip/x")
    assert out["path"] == str(env["root"] / "_worktrees" / "proj-scratch")
    assert out["base"] == "origin/main"
    wt = out["path"]
    with open(os.path.join(wt, "new.txt"), "w") as f:
        f.write("x")
    assert "staged" in tools["git_add"](wt, ["new.txt"])
    assert "sha" in tools["git_commit"](wt, "add file")
    assert tools["git_push"](wt, branch="wip/x").get("pushed") == "wip/x"
    # The main checkout never moved.
    assert env["clone"].active_branch.name == "feat"

    listed = tools["git_worktree"](env["path"], "list")["worktrees"]
    assert [e["under_worktree_root"] for e in listed] == [False, True]

    assert tools["git_worktree"](env["path"], "remove", name="scratch")["removed"] == wt
    assert not os.path.exists(wt)
    assert "wip/x" in [h.name for h in env["clone"].heads]


def test_worktree_remove_refuses_a_dirty_tree(tools, env):
    wt = tools["git_worktree"](env["path"], "add", name="dirty", branch="d")["path"]
    with open(os.path.join(wt, "untracked.txt"), "w") as f:
        f.write("x")
    out = tools["git_worktree"](env["path"], "remove", name="dirty")
    assert "error" in out
    assert os.path.exists(os.path.join(wt, "untracked.txt"))


def test_worktree_add_refuses_an_existing_destination(tools, env):
    tools["git_worktree"](env["path"], "add", name="dup", branch="b1")
    out = tools["git_worktree"](env["path"], "add", name="dup", branch="b2")
    # Our refusal, before git runs — not git's own "already exists".
    assert "Pick another name" in out["error"]


def test_worktree_add_with_explicit_base(tools, env):
    out = tools["git_worktree"](env["path"], "add", name="fb", branch="b", base="origin/feat")
    assert out["head_sha"] == env["clone"].commit("origin/feat").hexsha


def test_worktree_add_requires_branch(tools, env):
    assert "branch is required" in tools["git_worktree"](env["path"], "add", name="n")["error"]


def test_worktree_remove_refuses_an_unregistered_name(tools, env):
    out = tools["git_worktree"](env["path"], "remove", name="never")
    assert "No worktree named" in out["error"]


def test_worktree_root_outside_write_roots_is_refused(tools, env, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHOST_WORKTREE_ROOT", str(tmp_path / "elsewhere"))
    reset_config()
    out = tools["git_worktree"](env["path"], "add", name="x", branch="b")
    assert "not under any allowed root" in out["error"]
    assert not (tmp_path / "elsewhere").exists()


def test_worktree_bad_method_is_recorded(tools, env):
    out = tools["git_worktree"](env["path"], "create")
    assert "must be one of: add, list, remove" in out["error"]
    assert _entries()[-1]["params"]["rejected_value"] == "create"


def test_worktree_from_a_linked_worktree_names_the_main_repo(tools, env):
    wt = tools["git_worktree"](env["path"], "add", name="one", branch="b1")["path"]
    out = tools["git_worktree"](wt, "add", name="two", branch="b2")
    assert out["path"] == str(env["root"] / "_worktrees" / "proj-two")


# --- git_push_force_with_lease ----------------------------------------------------


def _rewrite_feat(env) -> tuple[str, str]:
    clone = env["clone"]
    old = clone.commit("origin/feat").hexsha
    clone.git.commit("--amend", "--allow-empty", "-m", "feature, rebased")
    return old, clone.head.commit.hexsha


def test_lease_push_with_the_right_sha(tools, env):
    old, new = _rewrite_feat(env)
    out = tools["git_push_force_with_lease"](env["path"], "feat", old)
    assert out["pushed_sha"] == new and out["replaced_sha"] == old
    assert env["origin"].commit("feat").hexsha == new


def test_lease_push_with_a_stale_sha_is_refused(tools, env):
    old, _new = _rewrite_feat(env)
    out = tools["git_push_force_with_lease"](env["path"], "feat", "f" * 40)
    assert "error" in out and "pushed" not in out
    assert "no longer at" in out["error"]
    assert env["origin"].commit("feat").hexsha == old
    assert _entries()[-1]["result"] == "error:PushRejected"


@pytest.mark.parametrize("branch", ["main", "master"])
def test_lease_push_to_a_protected_name_never_reaches_the_network(tools, env, branch):
    # No local origin/HEAD: any default-branch lookup would have to ask the remote, so
    # only the by-name check that runs first can produce this refusal.
    env["clone"].git.symbolic_ref("-d", "refs/remotes/origin/HEAD")
    with (
        patch.object(git.Remote, "push", side_effect=AssertionError("pushed")),
        patch.object(type(env["clone"].git), "ls_remote", side_effect=AssertionError("net")),
    ):
        out = tools["git_push_force_with_lease"](env["path"], branch, "a" * 40)
    assert "always protected" in out["error"]
    assert _entries()[-1]["result"] == "denied:protected_branch"


def test_lease_push_to_the_remote_default_is_refused(tools, env):
    # The remote renamed its default to `feat`; the clone's origin/HEAD still says main.
    env["origin"].git.symbolic_ref("HEAD", "refs/heads/feat")
    with patch.object(git.Remote, "push", side_effect=AssertionError("pushed")):
        out = tools["git_push_force_with_lease"](env["path"], "feat", "a" * 40)
    assert "default branch" in out["error"]


def test_lease_push_refuses_when_the_default_is_unknown(tools, env):
    clone = env["clone"]
    clone.git.symbolic_ref("-d", "refs/remotes/origin/HEAD")
    with (
        patch("githost_mcp.tools.git_local.remote_default_branch", return_value=None),
        patch.object(git.Remote, "push", side_effect=AssertionError("pushed")),
    ):
        out = tools["git_push_force_with_lease"](env["path"], "feat", "a" * 40)
    assert "could not determine" in out["error"]


def test_lease_push_short_sha_is_refused(tools, env):
    out = tools["git_push_force_with_lease"](env["path"], "feat", "abc1234")
    assert "full 40-character" in out["error"]


def test_lease_push_unknown_local_branch(tools, env):
    out = tools["git_push_force_with_lease"](env["path"], "nope", "a" * 40)
    assert "No local branch" in out["error"]


def test_lease_push_sends_the_explicit_lease(tools, env):
    old, _new = _rewrite_feat(env)
    with patch.object(git.Remote, "push", wraps=env["clone"].remotes.origin.push) as push:
        tools["git_push_force_with_lease"](env["path"], "feat", old)
    assert push.call_args.kwargs["force_with_lease"] == f"refs/heads/feat:{old}"
    assert push.call_args.kwargs["refspec"] == "refs/heads/feat:refs/heads/feat"
    assert "force" not in push.call_args.kwargs


# --- `~` paths ---------------------------------------------------------------------


def test_tilde_repo_path_runs_git_on_the_expanded_path(tools, env, tmp_path, monkeypatch):
    """Validation expands `~`; git must run on the same expanded path, not the literal.

    The cwd is a writable directory with no `~` in it. If git ran on the literal string
    it would look for, or create, `<cwd>/~/proj`.
    """
    monkeypatch.setenv("HOME", str(env["root"]))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    reset_config()
    with open(os.path.join(env["path"], "t.txt"), "w") as f:
        f.write("x")
    assert tools["git_add"]("~/proj", ["t.txt"]) == {"staged": ["t.txt"]}
    assert "sha" in tools["git_commit"]("~/proj", "via tilde")
    assert env["clone"].head.commit.message.startswith("via tilde")
    assert not (cwd / "~").exists()


def test_env_var_in_repo_path_is_not_expanded_after_validation(tools, env, monkeypatch):
    """`<root>/$VAR` validates as a literal path inside the root.

    GitPython would then expandvars() it by default and open wherever $VAR points —
    here a repo outside every allowed root. The tool must open the validated path.
    """
    outside = env["root"].parent / "outside"
    git.Repo.init(outside)
    monkeypatch.setenv("GHX_ESCAPE", "../outside")
    out = tools["git_status"](f"{env['root']}/$GHX_ESCAPE")
    assert "error" in out and "branch" not in out


def test_registry_runs_on_the_expanded_path(env, tmp_path, monkeypatch):
    """pypi_publish builds paths with os.path.join, which does not expand `~`."""
    monkeypatch.setenv("HOME", str(env["root"]))
    monkeypatch.setenv("PYPI_TOKEN", "pypi-fake-fake-fake-fake")
    monkeypatch.chdir(tmp_path)
    reset_config()
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.registry import register

    register(MockMCP())
    out = registered["pypi_publish"]("~/proj")
    assert out["error"] == f"dist directory not found: {env['root'] / 'proj' / 'dist'}"


def test_worktree_add_refuses_a_default_branch_that_parses_as_an_option(tools, env):
    """The default branch name comes from the remote; it must not reach `git fetch` as a flag."""
    with (
        patch("githost_mcp.tools.git_local.remote_default_branch", return_value="--upload-pack=x"),
        patch.object(
            type(env["clone"].git), "fetch", side_effect=AssertionError("fetched"), create=True
        ),
    ):
        out = tools["git_worktree"](env["path"], "add", name="opt", branch="b")
    assert "must not start with '-'" in out["error"]


def test_lease_push_reports_what_the_remote_received(tools, env):
    """If the local branch moves between the sha read and the push, report what landed."""
    old, _new = _rewrite_feat(env)
    clone = env["clone"]
    real_push = git.Remote.push

    def push_then_move(self, *a, **k):
        clone.git.commit("--allow-empty", "-m", "raced in")  # local branch moves first
        return real_push(self, *a, **k)

    with patch.object(git.Remote, "push", push_then_move):
        out = tools["git_push_force_with_lease"](env["path"], "feat", old)
    assert out["pushed_sha"] == env["origin"].commit("feat").hexsha

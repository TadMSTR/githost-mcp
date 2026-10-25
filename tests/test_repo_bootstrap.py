"""git_clone, *_repo_get and *_repo_create (0.16.0, agent-build-workflow-2026-09 p4b).

The clone tests run git for real, against a local bare origin. run_clone() is patched
at the tool boundary only because the tool builds an SSH URL nobody can reach from CI;
run_clone() itself is tested against real git, timeout included.
"""

import dataclasses
import inspect
import json
import os
import time
from unittest.mock import MagicMock, patch

import git
import httpx
import pytest
import respx

from githost_mcp import bootstrap
from githost_mcp.audit import init_logging
from githost_mcp.bootstrap import (
    clone_args,
    clone_destination,
    clone_url,
    ensure_owned,
    ensure_whole_tree_writer,
    first_bad_segment,
    run_clone,
    validate_depth,
    validate_description,
    validate_segment,
)
from githost_mcp.config import get_config, reset_config
from githost_mcp.errors import (
    AlreadyExists,
    InvalidArgument,
    MissingArgument,
    NotOwned,
    WriteGlobDenied,
    classify_reason,
)


def _entries():
    with open(os.environ["AUDIT_LOG_FILE"]) as f:
        return [json.loads(line) for line in f if line.strip()]


def _register(module):
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    module.register(MockMCP())
    return registered


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "repos" / "personal").mkdir(parents=True)
    (home / "repos" / "gitea").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", f"{home}/repos/personal,{home}/repos/gitea")
    monkeypatch.setenv("AGENT_ID", "test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fakefakefake123456789012345678901")
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "gitea_fake_token_1234567890abcdef")
    monkeypatch.delenv("FORGE_OWNED_OWNERS", raising=False)
    for var, value in (
        ("GIT_AUTHOR_NAME", "T"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "T"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
    ):
        monkeypatch.setenv(var, value)
    reset_config()
    init_logging()
    import githost_mcp._providers.github_client as gc

    gc._client = None
    yield {"home": home, "tmp": tmp_path}
    reset_config()


@pytest.fixture()
def origin(tmp_path):
    """A bare repo with one commit on main."""
    bare = git.Repo.init(tmp_path / "origin.git", bare=True, initial_branch="main")
    seed = git.Repo.clone_from(bare.git_dir, tmp_path / "seed")
    with seed.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@t")
    seed.git.commit("--allow-empty", "-m", "initial")
    seed.git.push("origin", "HEAD:refs/heads/main")
    return bare


# ---------------------------------------------------------------------------
# Guards (undecorated, so mutation testing reaches them)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["githost-mcp", ".github", "TadMSTR", "a", "a_b.c-d", "x" * 100])
def test_validate_segment_accepts(value):
    assert validate_segment("repo", value) == value


@pytest.mark.parametrize(
    "value",
    [".", "..", "-x", "a/b", "a b", "x.git", "X.GIT", "x" * 101, "a$b", "~x", "a\nb", "é"],
)
def test_validate_segment_refuses(value):
    with pytest.raises(InvalidArgument):
        validate_segment("repo", value)


@pytest.mark.parametrize("value", ["", None])
def test_validate_segment_missing(value):
    with pytest.raises(MissingArgument):
        validate_segment("repo", value)


def test_first_bad_segment_reports_the_first_failure():
    assert first_bad_segment((("owner", "TadMSTR"), ("repo", "x"))) is None
    reason, argument, value, message = first_bad_segment((("owner", "ok"), ("repo", "..")))
    assert (reason, argument, value) == ("invalid_argument", "repo", "..")
    assert "repo" in message
    assert first_bad_segment((("owner", ""), ("repo", "..")))[:2] == ("missing_argument", "owner")


@pytest.mark.parametrize("depth", [None, 1, 50])
def test_validate_depth_accepts(depth):
    assert validate_depth(depth) == depth


@pytest.mark.parametrize("depth", [0, -1, True, "3", 1.5])
def test_validate_depth_refuses(depth):
    with pytest.raises(InvalidArgument):
        validate_depth(depth)


def test_validate_description_bound():
    assert validate_description(None) is None
    assert validate_description("x" * 350) == "x" * 350
    with pytest.raises(InvalidArgument):
        validate_description("x" * 351)


def test_ensure_owned_github_is_case_insensitive():
    ensure_owned("github", "tadmstr", ["TadMSTR"], "")
    with pytest.raises(NotOwned, match="operator step"):
        ensure_owned("github", "siteboon", ["TadMSTR"], "gitea.example.com")


def test_ensure_owned_gitea_needs_a_configured_host():
    ensure_owned("gitea", "anyone", ["TadMSTR"], "gitea.example.com")
    with pytest.raises(NotOwned):
        ensure_owned("gitea", "anyone", ["TadMSTR"], "")


def test_clone_url_is_scp_ssh():
    assert clone_url("github", "TadMSTR", "x", "g.example") == "git@github.com:TadMSTR/x.git"
    assert clone_url("gitea", "host-forge", "scripts", "g.example") == (
        "git@g.example:host-forge/scripts.git"
    )


def test_clone_destination_follows_the_checkout_convention(env):
    home = env["home"]
    assert clone_destination("github", "TadMSTR", "githost-mcp") == str(
        home / "repos/personal/githost-mcp"
    )
    assert clone_destination("gitea", "host-forge", "scripts") == str(
        home / "repos/gitea/host-forge-scripts"
    )
    # A name that already carries the org prefix is not doubled.
    assert clone_destination("gitea", "agent-platform", "agent-platform-knowledge-base") == str(
        home / "repos/gitea/agent-platform-knowledge-base"
    )
    # A shared prefix without the dash is not the org prefix.
    assert clone_destination("gitea", "agent", "agentx") == str(home / "repos/gitea/agent-agentx")


def test_clone_args_never_carry_an_unsafe_option():
    assert clone_args(None, None) == ["--no-recurse-submodules"]
    args = clone_args(5, "dev")
    assert args == ["--no-recurse-submodules", "--depth", "5", "--branch", "dev"]
    unsafe = set(git.Repo.unsafe_git_clone_options)
    assert not unsafe.intersection(args)


def test_ensure_whole_tree_writer():
    ensure_whole_tree_writer("/d", [], "policy")
    with pytest.raises(WriteGlobDenied):
        ensure_whole_tree_writer("/d", ["README*"], "policy")


def test_new_reason_classes_are_classified():
    assert classify_reason("error:NotOwned", NotOwned("x")) == "not_owned"
    assert classify_reason("error:AlreadyExists", AlreadyExists("x")) == "already_exists"
    assert classify_reason("denied:not_owned") == "not_owned"


# ---------------------------------------------------------------------------
# run_clone against real git
# ---------------------------------------------------------------------------


def test_run_clone_clones(origin, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    run_clone(origin.git_dir, str(dest), clone_args(None, None))
    assert git.Repo(dest).head.commit.message.strip() == "initial"


def test_run_clone_is_killed_at_the_timeout(tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    started = time.monotonic()
    with pytest.raises(git.GitCommandError, match="did not complete"):
        run_clone(
            "ssh://example.invalid/x.git",
            str(dest),
            clone_args(None, None),
            timeout=1,
            # git appends host and command to GIT_SSH_COMMAND; the "#" discards them.
            extra_env={"GIT_SSH_COMMAND": "sleep 30 #"},
        )
    assert time.monotonic() - started < 15


def test_run_clone_refuses_unsafe_option(tmp_path):
    with pytest.raises(git.exc.UnsafeOptionError):
        run_clone("git@github.com:a/b.git", str(tmp_path / "o"), ["-c", "core.x=y"])


def test_run_clone_refuses_remote_helper(tmp_path):
    with pytest.raises(git.exc.UnsafeProtocolError):
        run_clone("ext::sh -c id", str(tmp_path / "o"), [])


# ---------------------------------------------------------------------------
# git_clone
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_tools(env):
    from githost_mcp.tools import git_local

    return _register(git_local)


def _fake_clone(source):
    calls = []

    def fake(url, dest, args, timeout=bootstrap.CLONE_TIMEOUT_S, extra_env=None):
        calls.append({"url": url, "dest": dest, "args": args})
        git.Git().clone(*args, "--", str(source), dest)

    return fake, calls


def test_git_clone_has_no_url_or_destination_parameter(local_tools):
    params = set(inspect.signature(local_tools["git_clone"]).parameters)
    assert params == {"provider", "owner", "repo", "depth", "branch"}


def test_git_clone_github(local_tools, env, origin):
    fake, calls = _fake_clone(origin.git_dir)
    with patch("githost_mcp.tools.git_local.run_clone", side_effect=fake):
        result = local_tools["git_clone"]("github", "TadMSTR", "TadMSTR")
    dest = env["home"] / "repos/personal/TadMSTR"
    assert result["path"] == str(dest)
    assert result["branch"] == "main" and result["empty"] is False
    assert len(result["head_sha"]) == 40
    assert calls[0]["url"] == "git@github.com:TadMSTR/TadMSTR.git"
    assert "--no-recurse-submodules" in calls[0]["args"]
    assert (dest / ".git").is_dir()
    assert _entries()[-1]["result"] == "ok"


def test_git_clone_gitea_destination(local_tools, env, origin):
    fake, calls = _fake_clone(origin.git_dir)
    with patch("githost_mcp.tools.git_local.run_clone", side_effect=fake):
        result = local_tools["git_clone"]("gitea", "host-forge", "scripts", depth=1)
    assert result["path"] == str(env["home"] / "repos/gitea/host-forge-scripts")
    assert calls[0]["url"] == "git@gitea.example.com:host-forge/scripts.git"
    assert calls[0]["args"][1:3] == ["--depth", "1"]


def test_git_clone_empty_repo(local_tools, env, tmp_path):
    bare = git.Repo.init(tmp_path / "empty.git", bare=True)
    fake, _ = _fake_clone(bare.git_dir)
    with patch("githost_mcp.tools.git_local.run_clone", side_effect=fake):
        result = local_tools["git_clone"]("github", "TadMSTR", "new")
    assert result["empty"] is True and result["branch"] is None and result["head_sha"] is None


def test_git_clone_refuses_third_party_before_any_clone(local_tools, env):
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"]("github", "siteboon", "claudecodeui")
    assert "error" in result and "operator step" in result["error"]
    run.assert_not_called()
    assert not (env["home"] / "repos/personal/claudecodeui").exists()
    entry = _entries()[-1]
    assert entry["result"] == "denied:not_owned"
    assert entry["params"]["rejected_value"] == "siteboon"


def test_git_clone_honours_forge_owned_owners(local_tools, env, origin, monkeypatch):
    monkeypatch.setenv("FORGE_OWNED_OWNERS", "someorg")
    reset_config()
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"]("github", "TadMSTR", "x")
    assert _entries()[-1]["result"] == "denied:not_owned", result
    run.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "owner", "repo", "reason"),
    [
        ("gitlab", "TadMSTR", "x", "denied:invalid_argument"),
        ("", "TadMSTR", "x", "denied:missing_argument"),
        ("github", "..", "x", "denied:invalid_argument"),
        ("github", "TadMSTR", "../x", "denied:invalid_argument"),
        ("github", "TadMSTR", "", "denied:missing_argument"),
        ("github", "TadMSTR", "x.git", "denied:invalid_argument"),
    ],
)
def test_git_clone_preflight_rejections(local_tools, env, provider, owner, repo, reason):
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"](provider, owner, repo)
    assert "error" in result
    run.assert_not_called()
    assert _entries()[-1]["result"] == reason


def test_git_clone_refuses_existing_destination_and_leaves_it(local_tools, env):
    dest = env["home"] / "repos/personal/existing"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine")
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"]("github", "TadMSTR", "existing")
    assert "already exists" in result["error"]
    run.assert_not_called()
    assert (dest / "keep.txt").read_text() == "mine"
    assert _entries()[-1]["result"] == "error:AlreadyExists"


def test_git_clone_refuses_existing_symlink(local_tools, env, tmp_path):
    (env["home"] / "repos/personal/link").symlink_to(tmp_path / "nowhere")
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"]("github", "TadMSTR", "link")
    assert "error" in result
    run.assert_not_called()


def test_git_clone_failure_removes_only_what_it_created(local_tools, env):
    dest = env["home"] / "repos/personal/partial"

    def half_clone(url, d, args, **kw):
        (dest / "half-written").write_text("x")
        raise git.GitCommandError(["git", "clone"], 128, "fatal: connection lost")

    with patch("githost_mcp.tools.git_local.run_clone", side_effect=half_clone):
        result = local_tools["git_clone"]("github", "TadMSTR", "partial")
    assert "connection lost" in result["error"]
    assert not dest.exists()
    assert (env["home"] / "repos/personal").is_dir()


def test_git_clone_refuses_outside_write_roots(local_tools, env, monkeypatch):
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(env["home"] / "repos/gitea"))
    reset_config()
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        result = local_tools["git_clone"]("github", "TadMSTR", "x")
    assert "not under any allowed root" in result["error"]
    run.assert_not_called()


def test_git_clone_refuses_a_glob_scoped_agent(local_tools, env):
    scoped = dataclasses.replace(get_config(), write_globs=["README*", "**/*.md"])
    with (
        patch("githost_mcp.tools.git_local.get_config", return_value=scoped),
        patch("githost_mcp.security.get_config", return_value=scoped),
        patch("githost_mcp.tools.git_local.run_clone") as run,
    ):
        result = local_tools["git_clone"]("github", "TadMSTR", "x")
    assert "write_globs" in result["error"]
    run.assert_not_called()
    assert not (env["home"] / "repos/personal/x").exists()


def test_git_clone_refuses_bad_depth_and_branch(local_tools, env):
    with patch("githost_mcp.tools.git_local.run_clone") as run:
        assert "depth" in local_tools["git_clone"]("github", "TadMSTR", "x", depth=0)["error"]
        bad_branch = local_tools["git_clone"]("github", "TadMSTR", "x", branch="--upload-pack=x")
    assert "error" in bad_branch
    run.assert_not_called()


# ---------------------------------------------------------------------------
# GitHub repo_get / repo_create
# ---------------------------------------------------------------------------


@pytest.fixture()
def gh_tools(env):
    from githost_mcp.tools import github

    return _register(github)


def _gh_repo(full_name="TadMSTR/githost-mcp", visibility="public", empty=False):
    from github import GithubException

    r = MagicMock()
    r.full_name = full_name
    r.visibility = visibility
    r.default_branch = "main"
    r.archived = False
    r.fork = False
    r.parent = None
    r.delete_branch_on_merge = True
    r.ssh_url = f"git@github.com:{full_name}.git"
    r.html_url = f"https://github.com/{full_name}"
    if empty:
        r.get_commits.side_effect = GithubException(409, {"message": "Git Repository is empty."})
    else:
        r.get_commits.return_value = iter([MagicMock()])
    return r


def _unknown():
    from github import UnknownObjectException

    return UnknownObjectException(404, {"message": "Not Found"})


def test_github_repo_get(gh_tools):
    gh = MagicMock()
    gh.get_repo.return_value = _gh_repo()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_get"]("TadMSTR/githost-mcp")
    assert result["exists"] is True
    assert result["visibility"] == "public"
    assert result["delete_branch_on_merge"] is True
    assert result["empty"] is False and result["parent"] is None


def test_github_repo_get_absent_is_an_answer(gh_tools):
    gh = MagicMock()
    gh.get_repo.side_effect = _unknown()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_get"]("TadMSTR/nope")
    assert result == {"exists": False, "full_name": "TadMSTR/nope"}
    assert _entries()[-1]["result"] == "ok"


def test_github_repo_get_empty_and_fork(gh_tools):
    r = _gh_repo(empty=True)
    r.fork = True
    r.parent = MagicMock(full_name="upstream/thing")
    gh = MagicMock()
    gh.get_repo.return_value = r
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_get"]("TadMSTR/thing")
    assert result["empty"] is True and result["parent"] == "upstream/thing"


def test_github_repo_get_other_errors_are_errors(gh_tools):
    from github import GithubException

    gh = MagicMock()
    gh.get_repo.side_effect = GithubException(500, {"message": "boom"})
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_get"]("TadMSTR/x")
    assert "error" in result


def _creating_gh(existing=False, readback_visibility="private"):
    gh = MagicMock()
    user = MagicMock(login="TadMSTR")
    gh.get_user.return_value = user
    created = _gh_repo("TadMSTR/probe", readback_visibility, empty=True)
    user.create_repo.return_value = created
    if existing:
        gh.get_repo.return_value = _gh_repo("TadMSTR/probe")
    else:
        gh.get_repo.side_effect = [_unknown(), created]
    return gh, user


def test_github_repo_create(gh_tools):
    gh, user = _creating_gh()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_create"]("probe", "private", description="d")
    user.create_repo.assert_called_once_with(
        "probe", private=True, auto_init=False, description="d"
    )
    assert result["created"] is True
    assert result["visibility"] == "private" and result["empty"] is True
    assert "warning" not in result
    entry = _entries()[-1]
    assert entry["repo"] == "TadMSTR/probe" and entry["params"]["visibility"] == "private"


def test_github_repo_create_reports_the_provider_visibility(gh_tools):
    gh, _ = _creating_gh(readback_visibility="public")
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_create"]("probe", "private")
    assert result["visibility"] == "public"
    assert "warning" in result


def test_github_repo_create_refuses_existing(gh_tools):
    gh, user = _creating_gh(existing=True)
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_create"]("probe", "private")
    assert "already exists" in result["error"]
    user.create_repo.assert_not_called()
    assert _entries()[-1]["result"] == "error:AlreadyExists"


@pytest.mark.parametrize(
    ("name", "visibility", "reason"),
    [
        ("probe", "", "denied:missing_argument"),
        ("probe", None, "denied:missing_argument"),
        ("probe", "internal", "denied:invalid_argument"),
        ("probe", "PRIVATE", "denied:invalid_argument"),
        ("..", "private", "denied:invalid_argument"),
        ("a/b", "private", "denied:invalid_argument"),
        ("", "private", "denied:missing_argument"),
    ],
)
def test_github_repo_create_preflight(gh_tools, name, visibility, reason):
    gh = MagicMock()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_create"](name, visibility)
    assert "error" in result
    gh.get_user.assert_not_called()
    assert _entries()[-1]["result"] == reason


def test_github_repo_create_refuses_long_description(gh_tools):
    gh = MagicMock()
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = gh_tools["github_repo_create"]("probe", "private", description="x" * 351)
    assert "350" in result["error"]
    gh.get_user.assert_not_called()


def test_repo_create_visibility_has_no_default(gh_tools, env):
    from githost_mcp.tools import gitea

    gitea_tools = _register(gitea)
    for fn in (gh_tools["github_repo_create"], gitea_tools["gitea_repo_create"]):
        param = inspect.signature(fn).parameters["visibility"]
        assert param.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Gitea repo_get / repo_create
# ---------------------------------------------------------------------------

_API = "https://gitea.example.com/api/v1"


@pytest.fixture()
def gitea_tools(env):
    from githost_mcp.tools import gitea

    return _register(gitea)


def _gitea_repo(private=True, internal=False, empty=False):
    return {
        "full_name": "host-forge/scripts",
        "private": private,
        "internal": internal,
        "default_branch": "main",
        "empty": empty,
        "archived": False,
        "fork": False,
        "parent": None,
        "default_delete_branch_after_merge": False,
        "ssh_url": "ssh://git@gitea.example.com:2222/host-forge/scripts.git",
        "html_url": "https://gitea.example.com/host-forge/scripts",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private", "internal", "visibility"),
    [(True, False, "private"), (False, True, "internal"), (False, False, "public")],
)
async def test_gitea_repo_get(gitea_tools, private, internal, visibility):
    with respx.mock:
        respx.get(f"{_API}/repos/host-forge/scripts").mock(
            return_value=httpx.Response(200, json=_gitea_repo(private, internal))
        )
        result = await gitea_tools["gitea_repo_get"]("host-forge/scripts")
    assert result["exists"] is True and result["visibility"] == visibility
    assert result["delete_branch_on_merge"] is False


@pytest.mark.asyncio
async def test_gitea_repo_get_absent(gitea_tools):
    with respx.mock:
        respx.get(f"{_API}/repos/host-forge/nope").mock(
            return_value=httpx.Response(404, json={"message": "not found"})
        )
        result = await gitea_tools["gitea_repo_get"]("host-forge/nope")
    assert result == {"exists": False, "full_name": "host-forge/nope"}


@pytest.mark.asyncio
@pytest.mark.parametrize("repo", ["../admin", "host-forge/..", "nope", "a/b/c"])
async def test_gitea_repo_get_refuses_before_any_request(gitea_tools, repo):
    # A catch-all that would answer: an unmatched request raises inside the client, is
    # caught as an error, and is never recorded — so without it "no request was made"
    # cannot fail.
    with respx.mock(assert_all_called=False) as router:
        anything = router.route().mock(return_value=httpx.Response(200, json=_gitea_repo()))
        result = await gitea_tools["gitea_repo_get"](repo)
    assert not anything.called
    assert "error" in result


@pytest.mark.asyncio
async def test_gitea_repo_create(gitea_tools):
    with respx.mock:
        respx.get(f"{_API}/orgs/host-forge").mock(return_value=httpx.Response(200, json={}))
        lookups = respx.get(f"{_API}/repos/host-forge/probe").mock(
            side_effect=[
                httpx.Response(404, json={"message": "not found"}),
                httpx.Response(200, json=_gitea_repo(private=True, empty=True)),
            ]
        )
        post = respx.post(f"{_API}/orgs/host-forge/repos").mock(
            return_value=httpx.Response(
                201,
                json={"full_name": "host-forge/probe", "ssh_url": "s", "html_url": "h"},
            )
        )
        result = await gitea_tools["gitea_repo_create"]("host-forge", "probe", "private")
    body = json.loads(post.calls[0].request.content)
    assert body == {"name": "probe", "private": True, "auto_init": False}
    assert lookups.call_count == 2
    assert result["visibility"] == "private" and result["empty"] is True
    assert "warning" not in result


@pytest.mark.asyncio
async def test_gitea_repo_create_refuses_existing(gitea_tools):
    with respx.mock:
        respx.get(f"{_API}/orgs/host-forge").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{_API}/repos/host-forge/probe").mock(
            return_value=httpx.Response(200, json=_gitea_repo())
        )
        post = respx.post(f"{_API}/orgs/host-forge/repos")
        result = await gitea_tools["gitea_repo_create"]("host-forge", "probe", "public")
    assert "already exists" in result["error"]
    assert not post.called


@pytest.mark.asyncio
async def test_gitea_repo_create_refuses_a_user_owner(gitea_tools):
    with respx.mock:
        respx.get(f"{_API}/orgs/ted").mock(return_value=httpx.Response(404, json={}))
        post = respx.post(f"{_API}/orgs/ted/repos")
        result = await gitea_tools["gitea_repo_create"]("ted", "probe", "private")
    assert "not an organization" in result["error"]
    assert not post.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "name", "visibility"),
    [
        ("host-forge", "probe", ""),
        ("host-forge", "probe", "internal"),
        ("..", "probe", "private"),
        ("host-forge", "..", "private"),
    ],
)
async def test_gitea_repo_create_preflight(gitea_tools, owner, name, visibility):
    with respx.mock(assert_all_called=False) as router:
        anything = router.route().mock(return_value=httpx.Response(200, json=_gitea_repo()))
        result = await gitea_tools["gitea_repo_create"](owner, name, visibility)
    assert not anything.called
    assert "error" in result

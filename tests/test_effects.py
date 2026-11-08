"""Effect fields and correlation ids in the audit trail (vikunja#1426, Done-when 1-2).

Before 0.19.0 an audit entry recorded that a call succeeded, not what it changed:
`git_commit` wrote `{repo_path, identity}` and `git_push` wrote `branch: null` when the
branch defaulted. Nothing tied an entry to a dispatcher run or task.
"""

import asyncio
import contextlib
import inspect
import json
import socket

import git
import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

from githost_mcp import effects
from githost_mcp.audit import (
    INTEGRITY_TAMPERED,
    INTEGRITY_VERIFIED,
    AuditCtx,
    audit_rejection,
    init_logging,
    verify_entry_integrity,
)
from githost_mcp.config import reset_config
from githost_mcp.correlation import (
    CallScopeMiddleware,
    correlation_from_headers,
    valid_session_id,
    valid_uuid,
)

RUN = "11111111-2222-4333-8444-555555555555"
TASK = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
# Built at runtime so the source holds no detector-shaped literal.
TOKEN = "ghp_" + "0123456789abcdef" * 2 + "0123"


@pytest.fixture()
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "test")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "gitea_fake_token_1234567890abcdef")
    reset_config()
    init_logging()
    yield tmp_path
    reset_config()


def _entries(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


def _register(module):
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    module.register(MockMCP())
    return registered


# ---------------------------------------------------------------------------
# Closed world: every tool is classified
# ---------------------------------------------------------------------------


def _all_tools():
    from githost_mcp.tools import (
        audit_query,
        git_local,
        gitea,
        github,
        gitlab,
        registry,
        release,
        woodpecker,
    )

    tools = {}
    for module in (audit_query, git_local, gitea, github, gitlab, registry, release, woodpecker):
        tools |= _register(module)
    return tools


def _classification_problems(names):
    """Every way `names` (the registered tools) and the effects classes disagree."""
    problems = []
    unclassified = names - effects.EFFECT_TOOLS - effects.READ_TOOLS
    if unclassified:
        problems.append(
            f"classify these in effects.EFFECT_TOOLS or READ_TOOLS: {sorted(unclassified)}"
        )
    if effects.EFFECT_TOOLS & effects.READ_TOOLS:
        problems.append(f"in both classes: {sorted(effects.EFFECT_TOOLS & effects.READ_TOOLS)}")
    stale = (effects.EFFECT_TOOLS | effects.READ_TOOLS | effects.HITL_TOOLS) - names
    if stale:
        problems.append(f"classified but not registered: {sorted(stale)}")
    not_effect = effects.HITL_TOOLS - effects.EFFECT_TOOLS
    if not_effect:
        problems.append(f"in HITL_TOOLS but not EFFECT_TOOLS: {sorted(not_effect)}")
    return problems


def test_every_registered_tool_is_classified_exactly_once():
    assert _classification_problems(set(_all_tools())) == []


def test_an_unclassified_tool_is_named():
    """The control for the test above: a new verb nobody classified must fail it, by name."""
    problems = _classification_problems(set(_all_tools()) | {"gitea_branch_protect"})
    assert len(problems) == 1
    assert "gitea_branch_protect" in problems[0]


def test_a_hitl_tool_must_be_an_effect_tool(monkeypatch):
    monkeypatch.setattr(effects, "HITL_TOOLS", effects.HITL_TOOLS | {"git_status"})
    problems = _classification_problems(set(_all_tools()))
    assert problems == ["in HITL_TOOLS but not EFFECT_TOOLS: ['git_status']"]


def test_a_hitl_tool_must_be_registered(monkeypatch):
    monkeypatch.setattr(effects, "HITL_TOOLS", effects.HITL_TOOLS | {"gitea_branch_protect"})
    problems = _classification_problems(set(_all_tools()))
    assert any("not registered" in p and "gitea_branch_protect" in p for p in problems)


def test_the_hitl_class_is_the_reviewed_set():
    """Pinned so a change to the class is a visible decision in review, not a side edit.

    The 14 tools of githost-scoped-2026-11 p3: what developer's and sysadmin's manifests
    already gate, plus npm_publish, pypi_publish and the three *_release_delete.
    """
    assert {
        "gitea_pr_merge",
        "github_pr_merge",
        "gitlab_mr_merge",
        "git_branch_delete_remote",
        "git_push_force_with_lease",
        "gitea_release_delete",
        "github_release_delete",
        "gitlab_release_delete",
        "woodpecker_pipeline_cancel",
        "release",
        "npm_publish",
        "pypi_publish",
        "github_repo_create",
        "gitea_repo_create",
    } == effects.HITL_TOOLS


def test_the_ungated_effect_set_is_the_reviewed_set():
    """The other half of the pin (audit F-02, githost-scoped-2026-11 p3).

    Pinning HITL_TOOLS alone lets a verb leave the class, or a new effect verb land
    outside it, without anyone deciding that it should be ungated. Each name here is
    ungated on purpose, for the reason in effects.py's comment above HITL_TOOLS.
    """
    assert {
        "git_add",
        "git_branch",
        "git_checkout",
        "git_clone",
        "git_commit",
        "git_pull",
        "git_push",
        "git_remote",
        "git_tag",
        "git_worktree",
        "gitea_actions",
        "gitea_create_release",
        "gitea_issue_write",
        "gitea_pr_comment",
        "gitea_pr_create",
        "gitea_pr_review",
        "gitea_release_update",
        "github_actions",
        "github_create_release",
        "github_fork",
        "github_issue_write",
        "github_pr_create",
        "github_pr_review",
        "github_pr_update",
        "github_release_update",
        "gitlab_create_release",
        "gitlab_issue_write",
        "gitlab_mr_create",
        "gitlab_mr_review",
        "gitlab_pipeline",
        "gitlab_release_update",
        "woodpecker_restart",
        "woodpecker_trigger",
    } == effects.UNGATED_EFFECT_TOOLS


def test_the_server_registers_the_same_tools(monkeypatch, tmp_path):
    """The module-level registration above is what server.py does; check the real list."""
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    reset_config()
    import githost_mcp.server as srv

    served = {t.name for t in asyncio.run(srv.mcp.list_tools(run_middleware=False))}
    assert served == set(_all_tools())


def test_every_effect_tool_calls_effect_in_its_source():
    """Structural half of the check; the conftest guard is the behavioural half (every
    success path the suite drives must record one)."""
    tools = _all_tools()
    for name in sorted(effects.EFFECT_TOOLS):
        source = inspect.getsource(tools[name])
        assert ".effect(" in source, f"{name} is an effect tool but never calls ac.effect()"


def test_selectors_name_real_values():
    for tool, (_argument, values) in effects.EFFECT_SELECTORS.items():
        assert tool in effects.EFFECT_TOOLS
        assert values


@pytest.mark.parametrize(
    ("tool", "params", "expected"),
    [
        ("git_push", {}, True),
        ("git_status", {}, False),
        ("git_branch", {"action": "list"}, False),
        ("git_branch", {"action": "delete"}, True),
        ("gitea_actions", {"method": "get_job_log"}, False),
        ("gitea_actions", {"method": "rerun_failed_jobs"}, True),
        ("release", {"dry_run": True}, False),
        ("release", {"dry_run": False}, True),
    ],
)
def test_requires_effect(tool, params, expected):
    assert effects.requires_effect(tool, params) is expected


def test_an_effect_tool_that_records_nothing_is_flagged(audit_env, monkeypatch):
    reported = []
    monkeypatch.setattr(
        "githost_mcp.audit._report_missing_effect", lambda tool, params: reported.append(tool)
    )
    AuditCtx("git_push", "local", "/repo", {}).finish("ok")
    assert reported == ["git_push"]
    assert _entries(audit_env)[-1]["effect_incomplete"] == ["effect_not_recorded"]


def test_a_failed_effect_tool_records_no_effect(audit_env):
    ac = AuditCtx("git_push", "local", "/repo", {})
    ac.effect(ref="main")
    ac.finish("error:PushRejected", "rejected")
    entry = _entries(audit_env)[-1]
    assert "effect" not in entry
    assert "effect_incomplete" not in entry


# ---------------------------------------------------------------------------
# Effect content
# ---------------------------------------------------------------------------


@pytest.fixture()
def pushed(audit_env):
    repo = git.Repo.init(audit_env / "repo")
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "t@test.com").release()
    (audit_env / "repo" / "a.txt").write_text("1")
    repo.index.add(["a.txt"])
    first = repo.index.commit("one")
    bare = git.Repo.init(audit_env / "remote.git", bare=True)
    repo.create_remote("origin", bare.git_dir)
    branch = repo.active_branch.name
    repo.remotes.origin.push(branch)
    return repo, bare, branch, first


def test_git_commit_records_sha_parent_and_a_count_not_the_message(pushed, audit_env):
    from githost_mcp.tools import git_local

    repo, _bare, _branch, first = pushed
    fns = _register(git_local)
    (audit_env / "repo" / "secret-plan.txt").write_text("x")
    fns["git_add"](repo.working_dir, ["secret-plan.txt"])
    result = fns["git_commit"](repo.working_dir, "a message that must not be audited")
    assert "error" not in result, result

    add_entry, commit_entry = _entries(audit_env)[-2:]
    assert add_entry["effect"] == {"paths_staged": 1}
    effect = commit_entry["effect"]
    assert effect["sha"] == repo.head.commit.hexsha
    assert effect["parent_sha"] == first.hexsha
    assert effect["files_changed"] == 1
    flat = json.dumps(commit_entry) + json.dumps(add_entry)
    assert "a message that must not be audited" not in flat
    assert "secret-plan" not in json.dumps(commit_entry["effect"])
    assert "secret-plan" not in json.dumps(add_entry["effect"])


def test_git_push_records_the_resolved_ref_and_both_shas(pushed, audit_env):
    from githost_mcp.tools import git_local

    repo, bare, branch, first = pushed
    (audit_env / "repo" / "a.txt").write_text("2")
    repo.index.add(["a.txt"])
    second = repo.index.commit("two")
    # branch left to default: the old entry said `branch: null`
    result = _register(git_local)["git_push"](repo.working_dir)
    assert "error" not in result, result

    entry = _entries(audit_env)[-1]
    assert entry["params"]["branch"] is None
    effect = entry["effect"]
    assert effect["ref"] == branch
    assert effect["remote"] == "origin"
    assert effect["new_sha"] == second.hexsha == bare.refs[branch].commit.hexsha
    assert effect["old_sha"] == first.hexsha
    assert effect["forced"] is False and effect["lease"] is False
    assert "FAST_FORWARD" in effect["flags"]
    assert "effect_incomplete" not in entry


def test_git_push_of_a_new_branch_says_created(pushed, audit_env):
    from githost_mcp.tools import git_local

    repo, _bare, _branch, _first = pushed
    repo.create_head("feature/x").checkout()
    _register(git_local)["git_push"](repo.working_dir)
    effect = _entries(audit_env)[-1]["effect"]
    assert effect["ref"] == "feature/x"
    assert effect["created"] is True
    assert "old_sha" not in effect


def test_git_checkout_records_old_and_new_head(pushed, audit_env):
    from githost_mcp.tools import git_local

    repo, _bare, _branch, first = pushed
    (audit_env / "repo" / "a.txt").write_text("3")
    repo.index.add(["a.txt"])
    second = repo.index.commit("three")
    result = _register(git_local)["git_checkout"](repo.working_dir, first.hexsha)
    assert "error" not in result, result
    effect = _entries(audit_env)[-1]["effect"]
    assert effect == {"ref": first.hexsha, "old_head": second.hexsha, "new_head": first.hexsha}


@respx.mock
async def test_gitea_pr_create_records_the_pr_number(audit_env):
    from githost_mcp.tools import gitea

    respx.post("https://gitea.example.com/api/v1/repos/o/r/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": 41, "head": {"sha": "c" * 40}, "title": "t", "state": "open"}
        )
    )
    await _register(gitea)["gitea_pr_create"]("o/r", "t", "feature/x", "main", body="BODY TEXT")
    entry = _entries(audit_env)[-1]
    assert entry["effect"] == {"pr_number": 41, "head_sha": "c" * 40, "base": "main"}
    assert "BODY TEXT" not in json.dumps(entry)


@respx.mock
async def test_gitea_pr_merge_reads_back_the_merge_sha(audit_env):
    from githost_mcp.tools import gitea

    base = "https://gitea.example.com/api/v1/repos/o/r/pulls/5"
    respx.post(f"{base}/merge").mock(return_value=httpx.Response(200))
    respx.get(base).mock(return_value=httpx.Response(200, json={"merge_commit_sha": "d" * 40}))
    result = await _register(gitea)["gitea_pr_merge"]("o/r", 5, merge_style="squash")
    assert result == {"merged": True, "pr_number": 5}
    assert _entries(audit_env)[-1]["effect"] == {
        "pr_number": 5,
        "merge_sha": "d" * 40,
        "method": "squash",
        "branch_deleted": False,
    }


@respx.mock
async def test_gitea_pr_merge_sends_title_and_branch_deletion_in_swagger_field_names(audit_env):
    """`merge_title_field` and `delete_branch_after_merge` are MergePullRequestOption's
    names (Gitea 1.27.3 swagger). The title used to go out as `merge_message_title`."""
    from githost_mcp.tools import gitea

    base = "https://gitea.example.com/api/v1/repos/o/r/pulls/5"
    route = respx.post(f"{base}/merge").mock(return_value=httpx.Response(200))
    respx.get(base).mock(return_value=httpx.Response(200, json={"merge_commit_sha": "d" * 40}))
    await _register(gitea)["gitea_pr_merge"](
        "o/r", 5, merge_style="squash", message="Title", delete_branch_after_merge=True
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {"Do": "squash", "merge_title_field": "Title", "delete_branch_after_merge": True}
    entry = _entries(audit_env)[-1]
    assert entry["effect"]["branch_deleted"] is True
    assert entry["params"]["delete_branch_after_merge"] is True


@respx.mock
async def test_gitea_pr_merge_keeps_the_branch_by_default(audit_env):
    from githost_mcp.tools import gitea

    base = "https://gitea.example.com/api/v1/repos/o/r/pulls/5"
    route = respx.post(f"{base}/merge").mock(return_value=httpx.Response(200))
    respx.get(base).mock(return_value=httpx.Response(200, json={}))
    await _register(gitea)["gitea_pr_merge"]("o/r", 5)
    assert json.loads(route.calls.last.request.content) == {"Do": "merge"}


def test_github_pr_merge_sends_a_custom_body_and_records_only_that_it_did(audit_env):
    from unittest.mock import MagicMock, patch

    from githost_mcp.tools import github

    status = MagicMock(merged=True, sha="e" * 40, message="merged")
    pr = MagicMock()
    pr.merge.return_value = status
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = pr
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        result = _register(github)["github_pr_merge"](
            "o/r", 7, merge_method="squash", commit_message="SECRET BODY TEXT"
        )
    assert result["merged"] is True
    assert pr.merge.call_args.kwargs["commit_message"] == "SECRET BODY TEXT"
    entry = _entries(audit_env)[-1]
    assert entry["effect"]["custom_body"] is True
    assert entry["params"]["custom_body"] is True
    assert "SECRET BODY TEXT" not in json.dumps(entry)


def test_github_pr_merge_without_a_body_leaves_githubs_default(audit_env):
    from unittest.mock import MagicMock, patch

    from githost_mcp.tools import github

    pr = MagicMock()
    pr.merge.return_value = MagicMock(merged=True, sha="e" * 40, message="merged")
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = pr
    with patch("githost_mcp.tools.github.get_github", return_value=gh):
        _register(github)["github_pr_merge"]("o/r", 7, merge_method="squash")
    assert "commit_message" not in pr.merge.call_args.kwargs
    assert _entries(audit_env)[-1]["effect"]["custom_body"] is False


@respx.mock
async def test_a_failed_merge_sha_read_back_is_incomplete_not_a_failed_merge(audit_env):
    from githost_mcp.tools import gitea

    base = "https://gitea.example.com/api/v1/repos/o/r/pulls/5"
    respx.post(f"{base}/merge").mock(return_value=httpx.Response(200))
    respx.get(base).mock(return_value=httpx.Response(500, text="boom"))
    result = await _register(gitea)["gitea_pr_merge"]("o/r", 5)
    assert result["merged"] is True
    entry = _entries(audit_env)[-1]
    assert entry["result"] == "ok"
    assert entry["effect"]["merge_sha"] is None
    assert entry["effect_incomplete"] == ["merge_sha"]


@respx.mock
async def test_gitea_rerun_failed_jobs_records_the_run(audit_env):
    from githost_mcp.tools import gitea

    respx.post("https://gitea.example.com/api/v1/repos/o/r/actions/runs/9/rerun-failed-jobs").mock(
        return_value=httpx.Response(201)
    )
    await _register(gitea)["gitea_actions"]("o/r", "rerun_failed_jobs", run_id=9)
    assert _entries(audit_env)[-1]["effect"] == {"action": "rerun_failed_jobs", "run_id": 9}


def test_effect_strings_are_scrubbed_before_they_are_bounded(audit_env):
    """A token straddling the bound must still be masked (bound_rejected_value's rule)."""
    ac = AuditCtx("git_push", "local", "/repo", {})
    ref = "x" * (effects.MAX_EFFECT_STRING_CHARS - 10) + TOKEN
    ac.effect(ref=ref, remote="https://user:hunter2@example.com/r.git")
    ac.finish("ok")
    effect = _entries(audit_env)[-1]["effect"]
    assert TOKEN[:10] not in effect["ref"]
    assert "hunter2" not in effect["remote"]
    assert len(effect["ref"]) <= effects.MAX_EFFECT_STRING_CHARS + 1


def test_effect_values_are_bounded_in_shape(audit_env):
    ac = AuditCtx("release", "local", "/repo", {"dry_run": False})
    ac.effect(
        flags=[str(i) for i in range(100)],
        targets={"github": "ok", "deep": {"a": {"b": 1}}},
        nested_list=[[1, 2]],
    )
    ac.finish("ok")
    effect = _entries(audit_env)[-1]["effect"]
    assert len(effect["flags"]) == effects.MAX_EFFECT_LIST_ITEMS
    assert effect["targets"] == {"github": "ok", "deep": "<nested>"}
    assert effect["nested_list"] == ["<nested>"]


def test_a_lambda_that_raises_becomes_an_incomplete_field(audit_env):
    def boom():
        raise RuntimeError("lazy attribute failed")

    ac = AuditCtx("github_fork", "github", "o/r", {})
    ac.effect(fork=lambda: "o/r-fork", repo_id=lambda: boom())
    ac.finish("ok")
    entry = _entries(audit_env)[-1]
    assert entry["effect"] == {"fork": "o/r-fork", "repo_id": None}
    assert entry["effect_incomplete"] == ["repo_id"]


# ---------------------------------------------------------------------------
# HMAC: effect and correlation are inside the signed entry
# ---------------------------------------------------------------------------


def test_an_entry_with_effect_verifies_and_a_tampered_effect_does_not(audit_env):
    ac = AuditCtx("git_push", "local", "/repo", {})
    ac.effect(ref="main", new_sha="a" * 40)
    ac.finish("ok")
    entry = _entries(audit_env)[-1]
    assert verify_entry_integrity(entry) == INTEGRITY_VERIFIED
    entry["effect"]["new_sha"] = "b" * 40
    assert verify_entry_integrity(entry) == INTEGRITY_TAMPERED


def test_a_tampered_run_id_fails_verification(audit_env):
    AuditCtx("git_status", "local", "/repo", {}).finish("ok")
    entry = _entries(audit_env)[-1]
    entry["run_id"] = RUN
    assert verify_entry_integrity(entry) == INTEGRITY_TAMPERED


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_every_entry_carries_the_correlation_fields_even_when_absent(audit_env):
    AuditCtx("git_status", "local", "/repo", {}).finish("ok")
    audit_rejection("git_push", "local", "/repo", "invalid_argument", "bad")
    for entry in _entries(audit_env):
        for key in ("run_id", "task_id", "session_id", "trace_id"):
            assert key in entry and entry[key] is None


@pytest.mark.parametrize(
    "value",
    [RUN, RUN.upper()],
)
def test_valid_uuids_are_accepted_lowercased(value):
    assert valid_uuid(value) == RUN


@pytest.mark.parametrize(
    "value",
    ["", "not-a-uuid", RUN + "\n", RUN + "x", "${FORGE_RUN_ID}", RUN.replace("-", "")],
)
def test_invalid_uuids_are_refused(value):
    assert valid_uuid(value) is None


@pytest.mark.parametrize("value", ["", "a b", "x" * 129, "id\n", "../x/y"])
def test_invalid_session_ids_are_refused(value):
    assert valid_session_id(value) is None


def test_invalid_headers_are_named_not_recorded():
    corr = correlation_from_headers(
        {
            "x-forge-run-id": "<script>",
            "x-forge-task-id": TASK,
            "x-forge-session-id": "has space",
        }
    )
    assert corr.run_id is None and corr.session_id is None
    assert corr.task_id == TASK
    assert corr.invalid == ("run_id", "session_id")


def test_absent_headers_are_not_invalid():
    corr = correlation_from_headers({})
    assert corr.invalid == ()
    assert corr.run_id is corr.task_id is corr.session_id is None


async def test_middleware_records_forwarded_ids_and_closes_a_leaked_ctx(audit_env):
    """End to end over the in-memory transport: the scope wraps the call, and a ctx the
    tool never finished is finished by the middleware rather than left open."""
    app = FastMCP("probe")
    app.add_middleware(CallScopeMiddleware())

    @app.tool
    def leaky() -> str:
        AuditCtx("git_status", "local", "/repo", {})  # never finished
        return "done"

    async with Client(app) as client:
        await client.call_tool("leaky")
    (entry,) = _entries(audit_env)
    assert entry["result"] == "error:unfinished"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_forwarded_headers_reach_the_audit_entry_of_a_sync_tool_over_http(audit_env):
    """The deployed path: streamable HTTP, a sync tool on a worker thread (Phase 0)."""
    from fastmcp.client.transports import StreamableHttpTransport

    from githost_mcp.tools import git_local

    repo = git.Repo.init(audit_env / "repo")
    app = FastMCP("probe")
    app.add_middleware(CallScopeMiddleware())
    git_local.register(app)
    port = _free_port()
    server = asyncio.create_task(app.run_http_async(host="127.0.0.1", port=port, show_banner=False))
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                await asyncio.sleep(0.05)
        transport = StreamableHttpTransport(
            f"http://127.0.0.1:{port}/mcp",
            headers={
                "X-Forge-Run-Id": RUN,
                "X-Forge-Task-Id": TASK,
                "X-Forge-Session-Id": "sess-1",
            },
        )
        async with Client(transport) as client:
            await client.call_tool("git_status", {"repo_path": repo.working_dir})
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server
    entry = _entries(audit_env)[-1]
    assert entry["tool"] == "git_status"
    assert (entry["run_id"], entry["task_id"], entry["session_id"]) == (RUN, TASK, "sess-1")
    assert "correlation_invalid" not in entry


# ---------------------------------------------------------------------------
# audit_log_query filters
# ---------------------------------------------------------------------------


def test_audit_log_query_filters_by_run_task_and_trace(audit_env):
    from githost_mcp.audit import write_audit_entry
    from githost_mcp.correlation import Correlation
    from githost_mcp.tools import audit_query

    write_audit_entry("git_push", "local", "/r", {}, "ok", 1, correlation=Correlation(run_id=RUN))
    write_audit_entry("git_push", "local", "/r", {}, "ok", 1, correlation=Correlation(task_id=TASK))
    write_audit_entry("git_push", "local", "/r", {}, "ok", 1, trace_id="f" * 32)
    query = _register(audit_query)["audit_log_query"]

    assert [e["run_id"] for e in query(run_id=RUN)["entries"]] == [RUN]
    assert [e["task_id"] for e in query(task_id=TASK.upper())["entries"]] == [TASK]
    assert [e["trace_id"] for e in query(trace_id="F" * 32)["entries"]] == ["f" * 32]
    assert query(run_id=TASK)["entries"] == []


@pytest.mark.parametrize(
    ("argument", "value"),
    [("run_id", "nope"), ("task_id", RUN + " "), ("trace_id", "abc")],
)
def test_audit_log_query_refuses_a_malformed_id(audit_env, argument, value):
    from githost_mcp.tools import audit_query

    (audit_env / "audit.jsonl").write_text("")
    result = _register(audit_query)["audit_log_query"](**{argument: value})
    assert "error" in result and argument in result["error"]


# ---------------------------------------------------------------------------
# Round-1 audit fixes: telemetry after a side effect never fails or misreports the call
# ---------------------------------------------------------------------------

# validate_remote_url accepts these; urlsplit refuses them (F-01 / CR-09 probe input).
_URLSPLIT_REFUSED = ["https://[::1/x", "https://[gitea.example/o/r.git", "ssh://[::1/r.git"]


@pytest.mark.parametrize("url", _URLSPLIT_REFUSED)
def test_url_host_is_total(url):
    from githost_mcp.tools.git_local import url_host

    assert url_host(url) is None


@pytest.mark.parametrize("url", _URLSPLIT_REFUSED)
def test_git_remote_add_with_an_unsplittable_url_reports_success(pushed, audit_env, url):
    """F-01: the remote is created first; the host read must not turn that into an error."""
    from githost_mcp.security import validate_remote_url
    from githost_mcp.tools import git_local

    try:
        validate_remote_url(url)
    except Exception:
        pytest.skip("validator refuses this shape; nothing to probe")
    repo, *_ = pushed
    result = _register(git_local)["git_remote"](repo.working_dir, "add", "probe", url)
    assert "error" not in result, result
    entry = _entries(audit_env)[-1]
    assert entry["result"] == "ok"
    assert entry["effect"]["host"] is None
    assert "probe" in [r.name for r in repo.remotes]


def test_git_show_reports_an_error_when_the_stats_read_fails(pushed, audit_env, monkeypatch):
    """CR-03: finish("ok") used to run before commit.stats; the except path's second
    finish() was then ignored, so the audit said ok while the caller got an error."""
    from githost_mcp.tools import git_local

    repo, *_ = pushed

    def boom(self):
        raise git.GitCommandError("diff", 128)

    monkeypatch.setattr(git.Commit, "stats", property(boom))
    result = _register(git_local)["git_show"](repo.working_dir, "HEAD")
    assert "error" in result
    entries = _entries(audit_env)
    assert entries[-1]["tool"] == "git_show"
    assert entries[-1]["result"].startswith("error:")


def test_push_effect_ignores_a_followed_tag(pushed, audit_env):
    """CR-08: push.followTags sends a NEW_TAG PushInfo beside the branch's; it used to mark
    the branch created and drop old_sha."""
    from githost_mcp.tools import git_local

    repo, bare, _branch, first = pushed
    repo.config_writer().set_value("push", "followTags", "true").release()
    (audit_env / "repo" / "a.txt").write_text("3")
    repo.index.add(["a.txt"])
    second = repo.index.commit("three")
    repo.create_tag("v9.9.9", ref=second, message="annotated, so followTags sends it")
    result = _register(git_local)["git_push"](repo.working_dir)
    assert "error" not in result, result
    assert "v9.9.9" in [t.name for t in bare.tags], "precondition: the tag was followed"
    effect = _entries(audit_env)[-1]["effect"]
    assert "created" not in effect
    assert effect["old_sha"] == first.hexsha


def test_push_effect_without_a_matching_ref_uses_every_info(pushed):
    from githost_mcp.tools.git_local import push_effect_fields

    class Info:
        flags = git.remote.PushInfo.NEW_HEAD
        remote_ref_string = "refs/heads/other"
        _old_commit_sha = None

    assert push_effect_fields(pushed[0], [Info()], "a" * 40, ref="main")["created"] is True


def test_pushinfo_private_old_sha_attr_exists(pushed):
    """F-05: push_effect_fields reads GitPython's private PushInfo._old_commit_sha. If an
    upgrade renames it, old_sha goes null on every push with no other signal."""
    repo, _bare, branch, _first = pushed
    infos = list(repo.remotes.origin.push(branch))
    assert infos, "push returned no PushInfo"
    assert all(hasattr(i, "_old_commit_sha") for i in infos)
    assert all(hasattr(i, "remote_ref_string") for i in infos)


def test_npm_target_host_read_cannot_fail_a_publish(audit_env, monkeypatch, tmp_path):
    """Category D probe member: the npm effect's urlsplit ran after the publish."""
    from githost_mcp.tools import registry

    ac = AuditCtx("npm_publish", "npm", str(tmp_path), {})
    bad = "https://[registry.example/"
    ac.effect(target=lambda: registry.urlsplit(bad).hostname if bad else "x")
    ac.finish("ok")
    entry = _entries(audit_env)[-1]
    assert entry["result"] == "ok"
    assert entry["effect"]["target"] is None
    assert "target" in entry["effect_incomplete"]


def test_correlation_header_read_failure_is_logged_once(monkeypatch):
    """F-04: a raising get_http_headers used to drop every id with no log line."""
    import fastmcp.server.dependencies as deps

    from githost_mcp import correlation

    calls = []

    def raising():
        raise RuntimeError("api moved")

    monkeypatch.setattr(deps, "get_http_headers", raising)
    monkeypatch.setattr(correlation, "_header_read_failed_logged", False)
    monkeypatch.setattr(correlation.log, "warning", lambda event, **kw: calls.append((event, kw)))
    assert correlation._read_headers() == {}
    assert correlation._read_headers() == {}
    assert calls == [("correlation_header_read_failed", {"error_class": "RuntimeError"})]


def test_git_add_on_an_unborn_branch_counts_the_files_a_directory_staged(audit_env):
    """CodeRabbit CR-10 (p4): `src/` on an unborn branch counted as one path."""
    from githost_mcp.tools import git_local

    repo = git.Repo.init(audit_env / "r")
    (audit_env / "r" / "src").mkdir()
    for i in range(3):
        (audit_env / "r" / "src" / f"f{i}.py").write_text("x")
    result = _register(git_local)["git_add"](str(audit_env / "r"), ["src"])
    assert sorted(result["staged"]) == ["src/f0.py", "src/f1.py", "src/f2.py"]
    assert _entries(audit_env)[-1]["effect"] == {"paths_staged": 3}
    assert len(repo.index.entries) == 3


def test_git_push_reports_the_sha_git_sent_when_the_branch_moves_mid_push(audit_env, monkeypatch):
    """CodeRabbit CR-11 (p4): a commit landing between the sha capture and git resolving
    refs/heads/<branch> was pushed while the earlier sha was reported."""
    from githost_mcp.tools import git_local

    bare = git.Repo.init(audit_env / "bare.git", bare=True)
    repo = git.Repo.init(audit_env / "r", initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "T")
        cw.set_value("user", "email", "t@t")
    (audit_env / "r" / "f").write_text("1")
    repo.index.add(["f"])
    repo.index.commit("one")
    repo.create_remote("origin", str(audit_env / "bare.git"))
    real_push = git.Remote.push

    def racing_push(self, *a, **k):
        (audit_env / "r" / "f").write_text("2")
        repo.index.add(["f"])
        repo.index.commit("two, from a concurrent writer")
        return real_push(self, *a, **k)

    monkeypatch.setattr(git.Remote, "push", racing_push)
    result = _register(git_local)["git_push"](str(audit_env / "r"), "origin", "main")
    sent = bare.commit("main").hexsha
    assert result["pushed_sha"] == sent
    assert _entries(audit_env)[-1]["effect"]["new_sha"] == sent


def test_push_effect_without_a_matching_ref_is_marked_incomplete(pushed, audit_env):
    """#1434 R2-01: the fallback used to be silent, so a tag's PushInfo could stand in for
    the branch's with nothing in the entry saying so."""
    from githost_mcp.audit import AuditCtx
    from githost_mcp.tools.git_local import push_effect_fields

    class Info:
        flags = git.remote.PushInfo.NEW_TAG
        remote_ref_string = "refs/tags/v1"
        _old_commit_sha = None

    fields = push_effect_fields(pushed[0], [Info()], "a" * 40, ref="main")
    assert fields["ref_matched"] is None
    ac = AuditCtx("git_push", "local", "/r", {"repo_path": "/r"})
    ac.effect(**fields)
    ac.finish("ok")
    assert _entries(audit_env)[-1]["effect_incomplete"] == ["ref_matched"]


def test_push_effect_with_a_matching_ref_has_no_marker(pushed):
    from githost_mcp.tools.git_local import push_effect_fields

    class Info:
        flags = git.remote.PushInfo.FAST_FORWARD
        remote_ref_string = "refs/heads/main"
        _old_commit_sha = None

    assert "ref_matched" not in push_effect_fields(pushed[0], [Info()], "a" * 40, ref="main")

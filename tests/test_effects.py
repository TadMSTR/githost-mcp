"""Effect fields and correlation ids in the audit trail (#1426 (id 1509), Done-when 1-2).

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
TOKEN = "ghp_0123456789abcdef0123456789abcdef0123"


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


def test_every_registered_tool_is_classified_exactly_once():
    names = set(_all_tools())
    unclassified = names - effects.EFFECT_TOOLS - effects.READ_TOOLS
    assert not unclassified, (
        f"classify these in effects.EFFECT_TOOLS or READ_TOOLS: {sorted(unclassified)}"
    )
    assert not effects.EFFECT_TOOLS & effects.READ_TOOLS
    stale = (effects.EFFECT_TOOLS | effects.READ_TOOLS) - names
    assert not stale, f"classified but not registered: {sorted(stale)}"


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
    }


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

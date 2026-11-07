"""Woodpecker CI diagnostics (githost-scoped-2026-11 p1, Phase 2).

Fixtures follow the Woodpecker 3.x schema in doc-cache `woodpecker` rest-api-84/87/88/91/51
and the live pipeline 1235 detail recorded in Phase 0 — never what this code sends today.
The old list test asserted the params the code produced and so locked the `limit` bug in.
"""

import base64
import json
import time

import httpx
import pytest
import respx

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config

BASE = "https://ci.example.com/api"
LOOKUP_URL = f"{BASE}/repos/lookup/owner/repo"
REPO_ID = 42
REPO_URL = f"{BASE}/repos/{REPO_ID}"


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WOODPECKER_URL", "https://ci.example.com")
    monkeypatch.setenv("WOODPECKER_TOKEN", "wp_fake_token_1234567890abcdef")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.woodpecker import register

    register(MockMCP())
    return registered


def _lookup():
    return respx.get(LOOKUP_URL).mock(
        return_value=httpx.Response(200, json={"id": REPO_ID, "full_name": "owner/repo"})
    )


def _step(sid, name, state, *, started=1794078592, finished=1794078600, exit_code=0, **kw):
    return {
        "id": sid,
        "pid": sid,
        "ppid": 1,
        "pipeline_id": 2653,
        "name": name,
        "type": "commands",
        "state": state,
        "exit_code": exit_code,
        "error": kw.get("error"),
        "started": started,
        "finished": finished,
        "uuid": f"01M6VPNH8MP5B3GK4B4{sid}",
        "depends_on": [],
    }


def _pipeline(steps, *, status="failure", workflow="woodpecker", extra_workflows=(), **kw):
    """Pipeline 1235's shape: global id 2653, number 1235, one `woodpecker` workflow."""
    workflows = [{"id": 2631, "name": workflow, "state": status, "children": steps}]
    workflows += list(extra_workflows)
    return {
        "id": 2653,
        "number": 1235,
        "status": status,
        "event": "push",
        "branch": "feature/deploy-guardrails",
        "ref": "refs/heads/feature/deploy-guardrails",
        "commit": "66f41cae4591c067dac338ba35c158533d6a706c",
        "message": kw.get("message", "feat: deploy guardrails"),
        "created": kw.get("created", 1794078580),
        "started": kw.get("started", 1794078590),
        "finished": kw.get("finished", 1794078803),
        "errors": kw.get(
            "errors",
            [
                {
                    "type": "bad_habit",
                    "message": "Consider adding a `when` block",
                    "is_warning": True,
                    "data": {"field": "steps.python"},
                }
            ],
        ),
        "cancel_info": kw.get("cancel_info"),
        "workflows": workflows,
    }


FAILED_1235 = [
    _step(9962, "clone", "success"),
    _step(9963, "lint-manifests", "success"),
    _step(9965, "bats", "success"),
    _step(9966, "python", "failure", exit_code=1),
]


def _log(lines):
    return [
        {"id": i, "step_id": 9966, "line": i, "time": i, "type": 0, "data": _b64(t)}
        for i, t in enumerate(lines)
    ]


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _audit_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# --- woodpecker_list_pipelines ------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sends_perpage_not_limit(tools):
    with respx.mock:
        _lookup()
        route = respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=[]))
        await tools["woodpecker_list_pipelines"]("owner/repo", limit=2)
    params = route.calls.last.request.url.params
    assert params["perPage"] == "2"
    assert "limit" not in params


@pytest.mark.asyncio
async def test_list_returns_at_most_limit_even_if_server_ignores_perpage(tools):
    rows = [{"id": 3000 + i, "number": 100 + i, "status": "success"} for i in range(50)]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=rows))
        result = await tools["woodpecker_list_pipelines"]("owner/repo", limit=2)
    assert len(result["pipelines"]) == 2


@pytest.mark.asyncio
async def test_list_forwards_every_filter_to_the_server(tools):
    with respx.mock:
        _lookup()
        route = respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=[]))
        await tools["woodpecker_list_pipelines"](
            "owner/repo",
            status="failure",
            branch="main",
            event="push, pull_request",
            ref="refs/pull/12",
            before="2026-11-07T00:00:00Z",
            after="2026-11-01T00:00:00+02:00",
        )
    params = route.calls.last.request.url.params
    assert params["status"] == "failure"
    assert params["branch"] == "main"
    assert params["event"] == "push,pull_request"
    assert params["ref"] == "refs/pull/12"
    assert params["before"] == "2026-11-07T00:00:00Z"
    assert params["after"] == "2026-11-01T00:00:00+02:00"


@pytest.mark.asyncio
async def test_list_rows_carry_number_handle_and_commit(tools):
    row = _pipeline(FAILED_1235, message="x" * 1000)
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=[row]))
        result = await tools["woodpecker_list_pipelines"]("owner/repo")
    p = result["pipelines"][0]
    assert p["pipeline_id"] == 1235
    assert p["internal_id"] == 2653
    assert p["commit"] == "66f41cae4591c067dac338ba35c158533d6a706c"
    assert p["ref"] == "refs/heads/feature/deploy-guardrails"
    assert len(p["message"]) <= 301


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "failed"},
        {"event": "push,merge"},
        {"before": "2026-11-07"},
        {"before": "2026-11-07T00:00:00"},  # no offset: not RFC3339
        {"after": "2026-13-40T00:00:00Z"},
        {"branch": "main\nx"},
        {"branch": ""},
        {"ref": "r" * 256},
    ],
)
async def test_list_rejects_bad_filters_before_any_request(tools, kwargs):
    with respx.mock:  # no routes: any request would raise
        result = await tools["woodpecker_list_pipelines"]("owner/repo", **kwargs)
    assert "error" in result


# --- woodpecker_status --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_steps_errors_and_number(tools):
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235))
        )
        r = await tools["woodpecker_status"]("owner/repo", 1235)
    assert r["pipeline_id"] == 1235 and r["internal_id"] == 2653
    assert r["event"] == "push"
    assert r["commit"].startswith("66f41cae")
    python = next(s for s in r["steps"] if s["name"] == "python")
    assert python == {
        "workflow": "woodpecker",
        "name": "python",
        "type": "commands",
        "state": "failure",
        "exit_code": 1,
        "error": None,
        "started": 1794078592,
        "finished": 1794078600,
        "uuid": "01M6VPNH8MP5B3GK4B49966",
    }
    assert r["errors"] == [
        {"type": "bad_habit", "message": "Consider adding a `when` block", "is_warning": True}
    ]
    assert r["stalled_hint"] is None  # terminal pipeline


@pytest.mark.asyncio
async def test_status_stalled_hint_names_first_unfinished_step(tools):
    now = int(time.time())
    steps = [
        _step(1, "clone", "success", started=now - 100, finished=now - 95),
        _step(2, "python", "running", started=now - 90, finished=0),
        _step(3, "bats", "pending", started=0, finished=0),
    ]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/7").mock(
            return_value=httpx.Response(200, json=_pipeline(steps, status="running"))
        )
        r = await tools["woodpecker_status"]("owner/repo", 7)
    hint = r["stalled_hint"]
    assert hint["step"] == "python" and hint["state"] == "running"
    assert 85 <= hint["seconds_in_state"] <= 120


@pytest.mark.asyncio
async def test_status_stalled_pending_step_counts_from_pipeline_start(tools):
    now = int(time.time())
    steps = [_step(1, "clone", "pending", started=0, finished=0)]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/7").mock(
            return_value=httpx.Response(
                200, json=_pipeline(steps, status="pending", started=0, created=now - 600)
            )
        )
        r = await tools["woodpecker_status"]("owner/repo", 7)
    assert r["stalled_hint"]["since"] == now - 600


@pytest.mark.asyncio
async def test_status_cancel_info_and_step_error(tools):
    steps = [_step(1, "python", "killed", error="context canceled https://u:pw999@h.x/")]
    info = {"canceled_by_user": "ted", "canceled_by_step": "", "superseded_by": 0, "x": "y"}
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/7").mock(
            return_value=httpx.Response(
                200, json=_pipeline(steps, status="killed", cancel_info=info)
            )
        )
        r = await tools["woodpecker_status"]("owner/repo", 7)
    assert r["cancel_info"] == {
        "canceled_by_step": "",
        "canceled_by_user": "ted",
        "superseded_by": 0,
    }
    assert "pw999" not in r["steps"][0]["error"]


@pytest.mark.asyncio
async def test_status_rejects_non_positive_pipeline_id(tools):
    with respx.mock:
        r = await tools["woodpecker_status"]("owner/repo", 0)
    assert "error" in r


# --- woodpecker_get_logs ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_logs_defaults_to_failed_step_and_tail(tools, tmp_path):
    lines = [f"ok {i}" for i in range(830)] + ["  FAIL one fingerprint", "1 test(s) FAILED"]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235))
        )
        route = respx.get(f"{REPO_URL}/logs/1235/9966").mock(
            return_value=httpx.Response(200, json=_log(lines))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 1235)
    assert route.called
    assert r["step"] == "python" and r["selected_by"] == "first_failed"
    assert r["mode"] == "tail"
    assert r["lines"][-1] == "1 test(s) FAILED"
    assert r["total_lines"] == 832
    assert (r["returned_from"], r["returned_to"]) == (332, 832)


@pytest.mark.asyncio
async def test_get_logs_picks_running_step_when_none_failed(tools):
    steps = [_step(1, "clone", "success"), _step(2, "bats", "running", finished=0)]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/5").mock(
            return_value=httpx.Response(200, json=_pipeline(steps, status="running"))
        )
        respx.get(f"{REPO_URL}/logs/5/2").mock(
            return_value=httpx.Response(200, json=_log(["running..."]))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 5)
    assert r["step"] == "bats" and r["selected_by"] == "first_running"
    assert r["mode"] == "head"


@pytest.mark.asyncio
async def test_get_logs_ambiguous_step_name_lists_workflows(tools):
    other = {
        "id": 2632,
        "name": "release",
        "state": "success",
        "children": [_step(20, "python", "success")],
    }
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235, extra_workflows=[other]))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 1235, step_name="python")
    assert "error" in r
    assert "release" in r["error"] and "woodpecker" in r["error"]


@pytest.mark.asyncio
async def test_get_logs_workflow_disambiguates(tools):
    other = {
        "id": 2632,
        "name": "release",
        "state": "success",
        "children": [_step(20, "python", "success")],
    }
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235, extra_workflows=[other]))
        )
        route = respx.get(f"{REPO_URL}/logs/1235/20").mock(
            return_value=httpx.Response(200, json=_log(["release python"]))
        )
        r = await tools["woodpecker_get_logs"](
            "owner/repo", 1235, step_name="python", workflow="release"
        )
    assert route.called
    assert r["workflow"] == "release" and r["lines"] == ["release python"]


@pytest.mark.asyncio
async def test_get_logs_unknown_workflow_lists_workflows(tools):
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 1235, workflow="nope")
    assert "error" in r and "woodpecker" in r["error"]


@pytest.mark.asyncio
async def test_get_logs_pending_step_returns_state_not_empty_lines(tools):
    steps = [_step(1, "clone", "success"), _step(2, "python", "pending", started=0, finished=0)]
    with respx.mock:  # no log route: fetching one would raise
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/5").mock(
            return_value=httpx.Response(200, json=_pipeline(steps, status="running"))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 5, step_name="python")
    assert r["state"] == "pending"
    assert r["lines"] is None
    assert "not run" in r["note"]


@pytest.mark.asyncio
async def test_get_logs_grep_returns_segments(tools):
    lines = ["ok 1", "ok 2", "not ok 3 - boom", "# expected 1", "ok 4"]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235))
        )
        respx.get(f"{REPO_URL}/logs/1235/9966").mock(
            return_value=httpx.Response(200, json=_log(lines))
        )
        r = await tools["woodpecker_get_logs"]("owner/repo", 1235, grep="not ok", context=1)
    assert r["segments"] == [
        {"from": 1, "to": 4, "lines": ["ok 2", "not ok 3 - boom", "# expected 1"]}
    ]


@pytest.mark.asyncio
async def test_get_logs_audit_records_params_never_grep_text_or_content(tools, tmp_path):
    lines = ["SECRETLINE-content-abc", "needle-xyz"]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=_pipeline(FAILED_1235))
        )
        respx.get(f"{REPO_URL}/logs/1235/9966").mock(
            return_value=httpx.Response(200, json=_log(lines))
        )
        await tools["woodpecker_get_logs"]("owner/repo", 1235, grep="needle-xyz")
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "needle-xyz" not in raw
    assert "SECRETLINE" not in raw
    rec = _audit_records(tmp_path)[-1]
    assert rec["tool"] == "woodpecker_get_logs"
    assert rec["params"]["grep"] is True
    app_log = tmp_path / "app.log"
    if app_log.exists():
        assert "SECRETLINE" not in app_log.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [{"mode": "middle"}, {"context": 21}, {"grep": "x" * 201}, {"grep": "a\x00b"}, {"limit": 0}],
)
async def test_get_logs_rejects_bad_view_args_before_any_request(tools, kwargs):
    with respx.mock:
        r = await tools["woodpecker_get_logs"]("owner/repo", 1235, **kwargs)
    assert "error" in r


# --- woodpecker_restart -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_sends_no_override_and_returns_new_number(tools):
    new = _pipeline([], status="pending")
    new.update({"id": 2700, "number": 1250})
    with respx.mock:
        _lookup()
        route = respx.post(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(200, json=new)
        )
        r = await tools["woodpecker_restart"]("owner/repo", 1235)
    req = route.calls.last.request
    assert dict(req.url.params) == {}
    assert req.content == b""
    assert r["pipeline_id"] == 1250 and r["internal_id"] == 2700
    assert r["restarted_from"] == 1235
    assert r["event"] == "push"


@pytest.mark.asyncio
async def test_restart_reports_server_error(tools):
    with respx.mock:
        _lookup()
        respx.post(f"{REPO_URL}/pipelines/1235").mock(
            return_value=httpx.Response(404, text="pipeline not found")
        )
        r = await tools["woodpecker_restart"]("owner/repo", 1235)
    assert "404" in r["error"]


@pytest.mark.asyncio
async def test_restart_rejects_bad_repo(tools):
    r = await tools["woodpecker_restart"]("../etc", 1)
    assert "error" in r


# --- woodpecker_pipeline_config -----------------------------------------------------------


CONFIG = "steps:\n  python:\n    image: python:3.12\n    environment:\n      TOKEN:\n" + (
    "        from_secret: gh_token\n"
)


@pytest.mark.asyncio
async def test_config_decodes_base64_and_lists_files(tools):
    body = [
        {"hash": "ee89", "name": "woodpecker", "data": _b64(CONFIG)},
        {"hash": "ff00", "name": "release", "data": _b64("steps: {}\n")},
    ]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1235/config").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["woodpecker_pipeline_config"]("owner/repo", 1235)
        r2 = await tools["woodpecker_pipeline_config"]("owner/repo", 1235, file="release")
    assert r["files"] == ["woodpecker", "release"]
    assert r["file"] == "woodpecker"
    assert r["lines"][0] == "steps:"
    assert "from_secret: gh_token" in r["lines"][-1]
    assert r2["lines"] == ["steps: {}"]


@pytest.mark.asyncio
async def test_config_accepts_int_array_encoding(tools):
    body = [{"hash": "x", "name": "woodpecker", "data": list(b"a: 1\n")}]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1/config").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["woodpecker_pipeline_config"]("owner/repo", 1)
    assert r["lines"] == ["a: 1"]


@pytest.mark.asyncio
async def test_config_unknown_file_lists_files(tools):
    body = [{"hash": "x", "name": "woodpecker", "data": _b64("a: 1\n")}]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1/config").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["woodpecker_pipeline_config"]("owner/repo", 1, file="other")
    assert "error" in r and "woodpecker" in r["error"]


@pytest.mark.asyncio
async def test_config_is_scrubbed(tools):
    body = [{"hash": "x", "name": "w", "data": _b64("url: https://bob:hunter2pw@h.x/r\n")}]
    with respx.mock:
        _lookup()
        respx.get(f"{REPO_URL}/pipelines/1/config").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["woodpecker_pipeline_config"]("owner/repo", 1)
    assert "hunter2pw" not in r["lines"][0]


# --- woodpecker_queue_info ----------------------------------------------------------------


def _task(repo_id, number, name, agent=""):
    return {
        "id": "t1",
        "repo_id": repo_id,
        "pipeline_id": 9000,
        "pipeline_number": number,
        "name": name,
        "agent_name": agent,
        "agent_id": 3,
        "created": int(time.time()) - 30,
        "labels": {"repo": "owner/repo", "secret-ish": "do-not-return"},
        "dependencies": [],
        "run_on": [],
    }


@pytest.mark.asyncio
async def test_queue_info_filters_by_repo_and_drops_labels(tools):
    body = {
        "paused": False,
        "pending": [_task(REPO_ID, 1250, "woodpecker"), _task(7, 3, "other")],
        "running": [_task(REPO_ID, 1249, "woodpecker", agent="agent-1")],
        "waiting_on_deps": [],
        "stats": {"worker_count": 2, "pending_count": 2, "running_count": 1},
    }
    with respx.mock:
        _lookup()
        respx.get(f"{BASE}/queue/info").mock(return_value=httpx.Response(200, json=body))
        r = await tools["woodpecker_queue_info"]("owner/repo")
    assert [p["pipeline_id"] for p in r["pending"]] == [1250]
    assert r["running"][0]["agent_name"] == "agent-1"
    assert 25 <= r["running"][0]["age_seconds"] <= 60
    assert "do-not-return" not in json.dumps(r)
    assert r["stats"]["worker_count"] == 2


@pytest.mark.asyncio
async def test_queue_info_without_repo_returns_all(tools):
    body = {"pending": [_task(1, 1, "a"), _task(2, 2, "b")], "running": []}
    with respx.mock:
        respx.get(f"{BASE}/queue/info").mock(return_value=httpx.Response(200, json=body))
        r = await tools["woodpecker_queue_info"]()
    assert len(r["pending"]) == 2


@pytest.mark.asyncio
async def test_queue_info_non_admin_is_a_clear_error(tools):
    with respx.mock:
        respx.get(f"{BASE}/queue/info").mock(return_value=httpx.Response(403))
        r = await tools["woodpecker_queue_info"]()
    assert "authorization denied" in r["error"]

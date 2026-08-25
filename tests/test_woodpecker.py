"""Tests for Woodpecker tools with respx HTTP mocks (Woodpecker 3.x API)."""

import base64
import json

import httpx
import pytest
import respx

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config

LOOKUP_URL = "https://ci.example.com/api/repos/lookup/owner/repo"
REPO_ID = 42
REPO_URL = f"https://ci.example.com/api/repos/{REPO_ID}"


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


def _lookup_mock():
    """Standard lookup route returning numeric repo ID."""
    return respx.get(LOOKUP_URL).mock(
        return_value=httpx.Response(200, json={"id": REPO_ID, "full_name": "owner/repo"})
    )


@pytest.mark.asyncio
async def test_woodpecker_list_pipelines_success(tools):
    mock_data = [
        {
            "id": 1,
            "number": 1,
            "status": "success",
            "branch": "main",
            "event": "push",
            "created": 1000,
            "started": 1001,
            "finished": 1010,
        },
        {
            "id": 2,
            "number": 2,
            "status": "failure",
            "branch": "feature",
            "event": "push",
            "created": 2000,
            "started": 2001,
            "finished": 2010,
        },
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=mock_data))
        result = await tools["woodpecker_list_pipelines"]("owner/repo")
    assert "pipelines" in result
    assert len(result["pipelines"]) == 2
    assert result["pipelines"][0]["status"] == "success"
    assert result["pipelines"][0]["branch"] == "main"


@pytest.mark.asyncio
async def test_woodpecker_list_pipelines_status_filter(tools):
    mock_data = [
        {
            "id": 1,
            "number": 1,
            "status": "success",
            "branch": "main",
            "event": "push",
            "created": 1000,
            "started": 1001,
            "finished": 1010,
        },
        {
            "id": 2,
            "number": 2,
            "status": "failure",
            "branch": "feature",
            "event": "push",
            "created": 2000,
            "started": 2001,
            "finished": 2010,
        },
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(200, json=mock_data))
        result = await tools["woodpecker_list_pipelines"]("owner/repo", status="success")
    assert len(result["pipelines"]) == 1
    assert result["pipelines"][0]["status"] == "success"


def _b64_entry(text: str, **extra) -> dict:
    """A Woodpecker 3.x log entry: text lives in base64 "data", not plaintext "out"."""
    return {"data": base64.b64encode(text.encode()).decode(), **extra}


def _pipeline_detail(steps: list[dict]) -> dict:
    """A pipeline-detail response shape. Steps are nested under
    workflows[].children[], not a separate /pipelines/{n}/steps endpoint — that
    route also matches nothing and 200s with the Woodpecker SPA's HTML index,
    confirmed live 2026-08-25 (a defect the build plan didn't catch)."""
    return {"id": 1, "number": 1, "workflows": [{"id": 1, "name": "woodpecker", "children": steps}]}


@pytest.mark.asyncio
async def test_woodpecker_get_logs_by_step_name(tools):
    mock_steps = [
        {"id": 10, "name": "clone"},
        {"id": 11, "name": "build"},
    ]
    mock_logs = [
        _b64_entry("step output line 1\n", pos=0, time=1000),
        _b64_entry("step output line 2\n", pos=1, time=1001),
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        # Correct route is /repos/{id}/logs/{pipeline}/{step}, not
        # /repos/{id}/pipelines/{pipeline}/{step}/logs — the old path matched no
        # Woodpecker route and 200'd with the SPA's HTML index (vikunja #478).
        # respx raises on an unmatched route, so mocking only this path is itself
        # part of the assertion; route.called below makes it explicit.
        logs_route = respx.get(f"{REPO_URL}/logs/1/11").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1, step_name="build")
    assert logs_route.called
    assert result["step"] == "build"
    assert len(result["lines"]) == 2
    assert result["lines"][0] == "step output line 1"
    assert result["lines"][1] == "step output line 2"
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_woodpecker_get_logs_truncation(tools):
    mock_steps = [{"id": 10, "name": "build"}]
    mock_logs = [_b64_entry(f"line {i}\n", pos=i, time=i) for i in range(600)]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        logs_route = respx.get(f"{REPO_URL}/logs/1/10").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert logs_route.called
    assert len(result["lines"]) == 500
    assert result["lines"][0] == "line 0"
    assert result["truncated"] is True
    assert "notice" in result


@pytest.mark.asyncio
async def test_woodpecker_get_logs_decodes_multiple_entries_with_newline_join(tools):
    """Entries carry no trailing newline of their own; each decoded entry becomes
    exactly one line, not a run-on concatenation (vikunja #478 defect 2)."""
    mock_steps = [{"id": 10, "name": "build"}]
    mock_logs = [
        _b64_entry("+ python --version"),
        _b64_entry("Python 3.12.13"),
        _b64_entry("+ apt-get update"),
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        respx.get(f"{REPO_URL}/logs/1/10").mock(return_value=httpx.Response(200, json=mock_logs))
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert result["lines"] == ["+ python --version", "Python 3.12.13", "+ apt-get update"]


@pytest.mark.asyncio
async def test_woodpecker_get_logs_null_data_is_blank_line(tools):
    """A present-but-null "data" is a real blank output line, not the missing-key
    bug shape — confirmed live 2026-08-25 (149 of 2219 entries on one step)."""
    mock_steps = [{"id": 10, "name": "build"}]
    mock_logs = [
        _b64_entry("+ echo hi"),
        {"id": 1, "step_id": 10, "time": 0, "line": 1, "data": None, "type": 0},
        _b64_entry("hi"),
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        respx.get(f"{REPO_URL}/logs/1/10").mock(return_value=httpx.Response(200, json=mock_logs))
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert result["lines"] == ["+ echo hi", "", "hi"]


@pytest.mark.asyncio
async def test_woodpecker_get_logs_missing_data_field_errors(tools):
    """A dict entry without "data" is the shape of the old bug reappearing — it must
    raise, not silently dump str(entry) as a garbage "line" (vikunja #478 defect 2)."""
    mock_steps = [{"id": 10, "name": "build"}]
    mock_logs = [{"out": "unexpected 1.x shape", "pos": 0, "time": 0}]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        respx.get(f"{REPO_URL}/logs/1/10").mock(return_value=httpx.Response(200, json=mock_logs))
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert "error" in result
    assert "data" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_get_logs_html_response_errors_clearly(tools):
    """A 200 with an HTML body (Woodpecker's SPA fallback for an unmatched route)
    must fail with a named routing error, not an opaque JSONDecodeError."""
    mock_steps = [{"id": 10, "name": "build"}]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail(mock_steps))
        )
        respx.get(f"{REPO_URL}/logs/1/10").mock(
            return_value=httpx.Response(
                200, text="<!doctype html><html>...</html>", headers={"content-type": "text/html"}
            )
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert "error" in result
    assert "non-JSON" in result["error"]
    assert "text/html" in result["error"]
    assert "logs/1/10" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_pipeline_cancel_success(tools):
    with respx.mock:
        _lookup_mock()
        respx.delete(f"{REPO_URL}/pipelines/7").mock(return_value=httpx.Response(204))
        result = await tools["woodpecker_pipeline_cancel"]("owner/repo", 7)
    assert result["cancelled"] is True
    assert result["id"] == 7


@pytest.mark.asyncio
async def test_woodpecker_pipeline_cancel_already_finished(tools):
    with respx.mock:
        _lookup_mock()
        respx.delete(f"{REPO_URL}/pipelines/5").mock(
            return_value=httpx.Response(409, text="pipeline is finished")
        )
        result = await tools["woodpecker_pipeline_cancel"]("owner/repo", 5)
    assert "error" in result
    assert "already finished" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_repo_not_found(tools):
    """Lookup 404 returns a clear error, not a crash."""
    with respx.mock:
        respx.get(LOOKUP_URL).mock(return_value=httpx.Response(404))
        result = await tools["woodpecker_list_pipelines"]("owner/repo")
    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.asyncio
async def test_woodpecker_trigger_with_branch(tools):
    with respx.mock:
        _lookup_mock()
        route = respx.post(f"{REPO_URL}/pipelines").mock(
            return_value=httpx.Response(
                200, json={"id": 88, "number": 12, "status": "pending", "branch": "dev"}
            )
        )
        result = await tools["woodpecker_trigger"]("owner/repo", branch="dev")
    # The chainable handle is the per-repo `number`; feeding the global `id` back into
    # status/logs/cancel 404s, so trigger -> status never worked (vikunja #269, id 280).
    assert result["pipeline_id"] == 12
    assert result["internal_id"] == 88
    assert result["branch"] == "dev"
    # Woodpecker 3.x wants a JSON body; a query param gets HTTP 400 (vikunja #269,
    # id 280). Assert the wire form, not just the parsed result — the previous
    # version of this test asserted url.params and so locked the bug in.
    request = route.calls.last.request
    assert json.loads(request.content) == {"branch": "dev"}
    assert "branch" not in request.url.params
    assert request.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_woodpecker_trigger_default_branch(tools):
    with respx.mock:
        _lookup_mock()
        route = respx.post(f"{REPO_URL}/pipelines").mock(
            return_value=httpx.Response(200, json={"number": 3, "status": "pending"})
        )
        result = await tools["woodpecker_trigger"]("owner/repo")
    assert result["pipeline_id"] == 3
    assert result["status"] == "pending"
    # Omitted entirely rather than sent as null, so the repo default applies.
    assert json.loads(route.calls.last.request.content) == {}


@pytest.mark.asyncio
async def test_woodpecker_status_success(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/9").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 9,
                    "status": "success",
                    "branch": "main",
                    "started": 1000,
                    "finished": 1010,
                },
            )
        )
        result = await tools["woodpecker_status"]("owner/repo", 9)
    assert result["id"] == 9
    assert result["status"] == "success"
    assert result["finished_at"] == 1010


@pytest.mark.asyncio
async def test_woodpecker_get_logs_no_steps(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(200, json=_pipeline_detail([]))
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert "error" in result
    assert "No steps" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_get_logs_step_not_found(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1").mock(
            return_value=httpx.Response(
                200, json=_pipeline_detail([{"id": 10, "name": "build"}])
            )
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1, step_name="deploy")
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_check_response_401(tools):
    """A 401 on the action endpoint surfaces a clean auth error."""
    with respx.mock:
        _lookup_mock()
        respx.post(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(401))
        result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert "authentication failed" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_check_response_403(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/9").mock(return_value=httpx.Response(403))
        result = await tools["woodpecker_status"]("owner/repo", 9)
    assert "error" in result
    assert "authorization denied" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_check_response_500(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/9").mock(return_value=httpx.Response(500))
        result = await tools["woodpecker_status"]("owner/repo", 9)
    assert "error" in result
    assert "500" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_error_includes_response_body(tools):
    """vikunja #277 (id 288): the body is the only thing that says why. Dropping it
    is what made #269's 400 undiagnosable for the tool's entire life."""
    with respx.mock:
        _lookup_mock()
        respx.post(f"{REPO_URL}/pipelines").mock(
            return_value=httpx.Response(400, text="Bad Request: branch not found")
        )
        result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert "400" in result["error"]
    assert "branch not found" in result["error"], f"response body was discarded: {result['error']}"


@pytest.mark.asyncio
async def test_woodpecker_error_body_is_bounded(tools):
    """A large body must not be echoed wholesale into the result."""
    with respx.mock:
        _lookup_mock()
        respx.post(f"{REPO_URL}/pipelines").mock(return_value=httpx.Response(400, text="x" * 5000))
        result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert result["error"].count("x") <= 300


@pytest.mark.asyncio
async def test_woodpecker_error_body_is_scrubbed(tools):
    """SC-14: the body can echo back a credential-bearing URL."""
    leaked = "https://ted:ghp_LEAKED_TOKEN@gitea.example.com/o/r.git"
    with respx.mock:
        _lookup_mock()
        respx.post(f"{REPO_URL}/pipelines").mock(
            return_value=httpx.Response(400, text=f"clone failed for {leaked}")
        )
        result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert "ghp_LEAKED_TOKEN" not in result["error"], (
        f"credential reached the caller: {result['error']}"
    )


@pytest.mark.asyncio
async def test_woodpecker_error_with_empty_body_omits_separator(tools):
    """No body means no dangling colon."""
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/9").mock(return_value=httpx.Response(500, text=""))
        result = await tools["woodpecker_status"]("owner/repo", 9)
    assert "error" in result
    assert "500" in result["error"]
    assert not result["error"].rstrip().endswith(":")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("woodpecker_trigger", ("bad repo",)),
        ("woodpecker_list_pipelines", ("bad repo",)),
        ("woodpecker_get_logs", ("bad repo", 1)),
        ("woodpecker_pipeline_cancel", ("bad repo", 1)),
        ("woodpecker_status", ("bad repo", 1)),
    ],
)
async def test_woodpecker_rejects_bad_repo_format(tools, tool_name, args):
    result = await tools[tool_name](*args)
    assert "error" in result
    assert "owner/repo" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_missing_token(tools, monkeypatch):
    monkeypatch.delenv("WOODPECKER_TOKEN", raising=False)
    reset_config()
    result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert "WOODPECKER_TOKEN" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_missing_url(tools, monkeypatch):
    monkeypatch.delenv("WOODPECKER_URL", raising=False)
    reset_config()
    result = await tools["woodpecker_trigger"]("owner/repo")
    assert "error" in result
    assert "WOODPECKER_URL" in result["error"]

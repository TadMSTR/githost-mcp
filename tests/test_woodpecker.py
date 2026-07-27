"""Tests for Woodpecker tools with respx HTTP mocks (Woodpecker 3.x API)."""

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


@pytest.mark.asyncio
async def test_woodpecker_get_logs_by_step_name(tools):
    mock_steps = [
        {"id": 10, "name": "clone"},
        {"id": 11, "name": "build"},
    ]
    mock_logs = [
        {"out": "step output line 1", "pos": 0, "time": 1000},
        {"out": "step output line 2", "pos": 1, "time": 1001},
    ]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1/steps").mock(
            return_value=httpx.Response(200, json=mock_steps)
        )
        respx.get(f"{REPO_URL}/pipelines/1/11/logs").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1, step_name="build")
    assert result["step"] == "build"
    assert len(result["lines"]) == 2
    assert result["lines"][0] == "step output line 1"
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_woodpecker_get_logs_truncation(tools):
    mock_steps = [{"id": 10, "name": "build"}]
    mock_logs = [{"out": f"line {i}", "pos": i, "time": i} for i in range(600)]
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1/steps").mock(
            return_value=httpx.Response(200, json=mock_steps)
        )
        respx.get(f"{REPO_URL}/pipelines/1/10/logs").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert len(result["lines"]) == 500
    assert result["truncated"] is True
    assert "notice" in result


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
        respx.get(f"{REPO_URL}/pipelines/1/steps").mock(return_value=httpx.Response(200, json=[]))
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert "error" in result
    assert "No steps" in result["error"]


@pytest.mark.asyncio
async def test_woodpecker_get_logs_step_not_found(tools):
    with respx.mock:
        _lookup_mock()
        respx.get(f"{REPO_URL}/pipelines/1/steps").mock(
            return_value=httpx.Response(200, json=[{"id": 10, "name": "build"}])
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

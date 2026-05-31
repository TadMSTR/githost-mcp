"""Tests for new Woodpecker tools with respx HTTP mocks."""

import pytest
import respx
import httpx

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


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


@pytest.mark.asyncio
async def test_woodpecker_list_pipelines_success(tools):
    mock_data = [
        {"id": 1, "number": 1, "status": "success", "branch": "main", "event": "push",
         "created": 1000, "started": 1001, "finished": 1010},
        {"id": 2, "number": 2, "status": "failure", "branch": "feature", "event": "push",
         "created": 2000, "started": 2001, "finished": 2010},
    ]
    with respx.mock:
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines").mock(
            return_value=httpx.Response(200, json=mock_data)
        )
        result = await tools["woodpecker_list_pipelines"]("owner/repo")
    assert "pipelines" in result
    assert len(result["pipelines"]) == 2
    assert result["pipelines"][0]["status"] == "success"
    assert result["pipelines"][0]["branch"] == "main"


@pytest.mark.asyncio
async def test_woodpecker_list_pipelines_status_filter(tools):
    mock_data = [
        {"id": 1, "number": 1, "status": "success", "branch": "main", "event": "push",
         "created": 1000, "started": 1001, "finished": 1010},
        {"id": 2, "number": 2, "status": "failure", "branch": "feature", "event": "push",
         "created": 2000, "started": 2001, "finished": 2010},
    ]
    with respx.mock:
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines").mock(
            return_value=httpx.Response(200, json=mock_data)
        )
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
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines/42/steps").mock(
            return_value=httpx.Response(200, json=mock_steps)
        )
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines/42/11/logs").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 42, step_name="build")
    assert result["step"] == "build"
    assert len(result["lines"]) == 2
    assert result["lines"][0] == "step output line 1"
    assert "truncated" not in result


@pytest.mark.asyncio
async def test_woodpecker_get_logs_truncation(tools):
    mock_steps = [{"id": 10, "name": "build"}]
    # Generate 600 log lines
    mock_logs = [{"out": f"line {i}", "pos": i, "time": i} for i in range(600)]
    with respx.mock:
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines/1/steps").mock(
            return_value=httpx.Response(200, json=mock_steps)
        )
        respx.get("https://ci.example.com/api/repos/owner/repo/pipelines/1/10/logs").mock(
            return_value=httpx.Response(200, json=mock_logs)
        )
        result = await tools["woodpecker_get_logs"]("owner/repo", 1)
    assert len(result["lines"]) == 500
    assert result["truncated"] is True
    assert "notice" in result


@pytest.mark.asyncio
async def test_woodpecker_pipeline_cancel_success(tools):
    with respx.mock:
        respx.delete("https://ci.example.com/api/repos/owner/repo/pipelines/7").mock(
            return_value=httpx.Response(204)
        )
        result = await tools["woodpecker_pipeline_cancel"]("owner/repo", 7)
    assert result["cancelled"] is True
    assert result["id"] == 7


@pytest.mark.asyncio
async def test_woodpecker_pipeline_cancel_already_finished(tools):
    with respx.mock:
        respx.delete("https://ci.example.com/api/repos/owner/repo/pipelines/5").mock(
            return_value=httpx.Response(409, text="pipeline is finished")
        )
        result = await tools["woodpecker_pipeline_cancel"]("owner/repo", 5)
    assert "error" in result
    assert "already finished" in result["error"]

"""Tests for Gitea tools with respx HTTP mocks."""

import pytest
import respx
import httpx

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "gitea_fake_token_1234567890abcdef")
    monkeypatch.setenv("GITEA_OWNER", "testowner")
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

    from githost_mcp.tools.gitea import register
    register(MockMCP())
    return registered


@pytest.mark.asyncio
async def test_gitea_list_releases(tools):
    fns = tools
    mock_data = [{"tag_name": "v1.0.0", "name": "v1.0.0", "html_url": "https://gitea.example.com/testowner/repo/releases/tag/v1.0.0", "draft": False, "prerelease": False, "published_at": "2026-05-01"}]

    with respx.mock:
        respx.get("https://gitea.example.com/api/v1/repos/testowner/repo/releases").mock(
            return_value=httpx.Response(200, json=mock_data)
        )
        result = await fns["gitea_list_releases"]("testowner/repo")
    assert len(result["releases"]) == 1
    assert result["releases"][0]["tag"] == "v1.0.0"


@pytest.mark.asyncio
async def test_gitea_create_release(tools):
    fns = tools
    mock_response = {"id": 1, "tag_name": "v1.0.0", "html_url": "https://gitea.example.com/testowner/repo/releases/1"}

    with respx.mock:
        respx.post("https://gitea.example.com/api/v1/repos/testowner/repo/releases").mock(
            return_value=httpx.Response(201, json=mock_response)
        )
        result = await fns["gitea_create_release"]("testowner/repo", "v1.0.0")
    assert result["tag"] == "v1.0.0"


@pytest.mark.asyncio
async def test_gitea_401_clean_error(tools, monkeypatch):
    """401 must surface without token value."""
    fns = tools
    token = "gitea_fake_token_1234567890abcdef"

    with respx.mock:
        respx.get("https://gitea.example.com/api/v1/repos/testowner/repo/releases").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        result = await fns["gitea_list_releases"]("testowner/repo")
    assert "error" in result
    assert token not in result["error"]

"""Tests for GitLab tools with mocked python-gitlab."""

from unittest.mock import MagicMock, patch
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-fakefakefake1234567890abcdef")
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.com")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()
    import githost_mcp._providers.gitlab_client as gc
    gc._client = None


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.gitlab import register
    register(MockMCP())
    return registered


def test_gitlab_list_releases(tools):
    fns = tools
    mock_rel = MagicMock()
    mock_rel.tag_name = "v1.0.0"
    mock_rel.name = "Release 1.0.0"
    mock_rel.released_at = "2026-05-01T00:00:00Z"

    mock_proj = MagicMock()
    mock_proj.releases.list.return_value = [mock_rel]
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    with patch("githost_mcp.tools.gitlab.get_gitlab", return_value=mock_gl), \
         patch("githost_mcp.tools.gitlab.gitlab_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        result = fns["gitlab_list_releases"]("owner/project")
    assert len(result["releases"]) == 1
    assert result["releases"][0]["tag"] == "v1.0.0"


def test_gitlab_401_clean_error(tools, monkeypatch):
    """GitLab 401 must not include token value."""
    fns = tools
    token = "glpat-fakefakefake1234567890abcdef"
    monkeypatch.setenv("GITLAB_TOKEN", token)
    reset_config()
    import githost_mcp._providers.gitlab_client as gc
    gc._client = None

    def raise_401(*args, **kwargs):
        raise Exception(f"401 Unauthorized {token}")

    with patch("githost_mcp.tools.gitlab.get_gitlab", side_effect=raise_401):
        result = fns["gitlab_list_releases"]("owner/project")
    assert "error" in result
    assert token not in result["error"]

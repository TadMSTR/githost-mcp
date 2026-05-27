"""Tests for GitHub tools with mocked PyGithub."""

from unittest.mock import MagicMock, patch
import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fakefakefake123456789012345678901")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()
    # Reset cached github client
    import githost_mcp._providers.github_client as gc
    gc._client = None


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.github import register
    register(MockMCP())
    return registered


def _mock_release(tag="v1.0.0"):
    r = MagicMock()
    r.id = 1
    r.tag_name = tag
    r.title = tag
    r.html_url = f"https://github.com/owner/repo/releases/tag/{tag}"
    r.draft = False
    r.prerelease = False
    r.published_at = None
    return r


def test_github_create_release(tools):
    fns = tools
    mock_release = _mock_release()
    mock_repo = MagicMock()
    mock_repo.create_git_release.return_value = mock_release
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    with patch("githost_mcp.tools.github.get_github", return_value=mock_gh), \
         patch("githost_mcp.tools.github.github_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        result = fns["github_create_release"]("owner/repo", "v1.0.0")
    assert result["tag"] == "v1.0.0"
    assert "url" in result


def test_github_401_surfaces_clean_error(tools, monkeypatch):
    """401 errors must not include the token value."""
    fns = tools
    token = "ghp_fakefakefake123456789012345678901"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    reset_config()
    import githost_mcp._providers.github_client as gc
    gc._client = None

    def raise_401(*args, **kwargs):
        raise Exception(f"401 Unauthorized: {token}")

    with patch("githost_mcp.tools.github.get_github", side_effect=raise_401):
        result = fns["github_create_release"]("owner/repo", "v1.0.0")
    assert "error" in result
    assert token not in result["error"]


def test_github_pr_list(tools):
    fns = tools
    mock_pr = MagicMock()
    mock_pr.number = 42
    mock_pr.title = "Test PR"
    mock_pr.state = "open"
    mock_pr.user.login = "devuser"
    mock_pr.base.ref = "main"
    mock_pr.head.ref = "feature"
    mock_pr.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    mock_pr.html_url = "https://github.com/owner/repo/pull/42"

    mock_repo = MagicMock()
    mock_repo.get_pulls.return_value = [mock_pr]
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    with patch("githost_mcp.tools.github.get_github", return_value=mock_gh), \
         patch("githost_mcp.tools.github.github_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)):
        result = fns["github_pr_list"]("owner/repo")
    assert len(result["prs"]) == 1
    assert result["prs"][0]["number"] == 42

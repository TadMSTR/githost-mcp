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

    with (
        patch("githost_mcp.tools.gitlab.get_gitlab", return_value=mock_gl),
        patch(
            "githost_mcp.tools.gitlab.gitlab_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        ),
    ):
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


def _passthrough(fn, *a, **kw):
    return fn(*a, **kw)


def _patch_gl(mock_gl):
    return (
        patch("githost_mcp.tools.gitlab.get_gitlab", return_value=mock_gl),
        patch("githost_mcp.tools.gitlab.gitlab_call", side_effect=_passthrough),
    )


def test_gitlab_create_release(tools):
    rel = MagicMock()
    rel.name = "Release 1.0.0"
    rel._links = {"self": "https://gitlab.com/owner/project/-/releases/v1.0.0"}
    mock_proj = MagicMock()
    mock_proj.releases.create.return_value = rel
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_create_release"]("owner/project", "v1.0.0")
    assert result["tag"] == "v1.0.0"
    assert result["url"].endswith("v1.0.0")


def test_gitlab_get_release(tools):
    rel = MagicMock()
    rel.tag_name = "v1.0.0"
    rel.name = "Release 1.0.0"
    rel.description = "notes"
    rel.released_at = "2026-05-01T00:00:00Z"
    mock_proj = MagicMock()
    mock_proj.releases.get.return_value = rel
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_get_release"]("owner/project", "v1.0.0")
    assert result["tag"] == "v1.0.0"
    assert result["description"] == "notes"


def test_gitlab_mr_list(tools):
    mr = MagicMock()
    mr.iid = 5
    mr.title = "Add feature"
    mr.state = "opened"
    mr.author = {"username": "dev"}
    mr.source_branch = "feature"
    mr.target_branch = "main"
    mr.created_at = "2026-05-01T00:00:00Z"
    mr.web_url = "https://gitlab.com/owner/project/-/merge_requests/5"
    mock_proj = MagicMock()
    mock_proj.mergerequests.list.return_value = [mr]
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_mr_list"]("owner/project", state="opened")
    assert result["mrs"][0]["iid"] == 5
    assert result["mrs"][0]["author"] == "dev"


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("gitlab_create_release", ("owner/project", "v1")),
        ("gitlab_get_release", ("owner/project", "v1")),
        ("gitlab_list_releases", ("owner/project",)),
        ("gitlab_mr_list", ("owner/project",)),
    ],
)
def test_gitlab_tool_error_paths(tools, tool_name, args):
    with patch("githost_mcp.tools.gitlab.get_gitlab", side_effect=ValueError("boom")):
        result = tools[tool_name](*args)
    assert "error" in result


def test_gitlab_mr_create(tools):
    mr = MagicMock()
    mr.iid = 5
    mr.title = "Add feature"
    mr.state = "opened"
    mr.source_branch = "feature"
    mr.target_branch = "main"
    mr.web_url = "https://gitlab.com/owner/project/-/merge_requests/5"
    mock_proj = MagicMock()
    mock_proj.mergerequests.create.return_value = mr
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_mr_create"]("owner/project", "Add feature", "feature", "main")
    assert result["iid"] == 5
    payload = mock_proj.mergerequests.create.call_args.args[0]
    assert payload["source_branch"] == "feature"
    assert payload["target_branch"] == "main"


def test_gitlab_mr_get(tools):
    mr = MagicMock()
    mr.iid = 5
    mr.title = "Add feature"
    mr.state = "opened"
    mr.merge_status = "can_be_merged"
    mr.source_branch = "feature"
    mr.target_branch = "main"
    mr.author = {"username": "dev"}
    mr.web_url = "https://gitlab.com/owner/project/-/merge_requests/5"
    mr.created_at = "2026-05-01T00:00:00Z"
    mr.updated_at = "2026-05-02T00:00:00Z"
    mr.labels = ["feature"]
    mock_proj = MagicMock()
    mock_proj.mergerequests.get.return_value = mr
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_mr_get"]("owner/project", 5)
    assert result["iid"] == 5
    assert result["author"] == "dev"
    assert result["merge_status"] == "can_be_merged"


def test_gitlab_mr_merge(tools):
    mr = MagicMock()
    mr.iid = 5
    mr.state = "merged"
    mr.web_url = "https://gitlab.com/owner/project/-/merge_requests/5"
    mock_proj = MagicMock()
    mock_proj.mergerequests.get.return_value = mr
    mock_gl = MagicMock()
    mock_gl.projects.get.return_value = mock_proj

    p1, p2 = _patch_gl(mock_gl)
    with p1, p2:
        result = tools["gitlab_mr_merge"](
            "owner/project", 5, merge_commit_message="Merge it", squash=True
        )
    assert result["merged"] is True
    assert result["iid"] == 5
    assert mr.merge.call_args.kwargs["squash"] is True
    assert mr.merge.call_args.kwargs["merge_commit_message"] == "Merge it"


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("gitlab_mr_create", ("owner/project", "t", "feature", "main")),
        ("gitlab_mr_get", ("owner/project", 1)),
        ("gitlab_mr_merge", ("owner/project", 1)),
    ],
)
def test_gitlab_mr_tool_error_paths(tools, tool_name, args):
    with patch("githost_mcp.tools.gitlab.get_gitlab", side_effect=ValueError("boom")):
        result = tools[tool_name](*args)
    assert "error" in result

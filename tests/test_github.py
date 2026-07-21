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

    with (
        patch("githost_mcp.tools.github.get_github", return_value=mock_gh),
        patch(
            "githost_mcp.tools.github.github_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        ),
    ):
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

    with (
        patch("githost_mcp.tools.github.get_github", return_value=mock_gh),
        patch(
            "githost_mcp.tools.github.github_call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        ),
    ):
        result = fns["github_pr_list"]("owner/repo")
    assert len(result["prs"]) == 1
    assert result["prs"][0]["number"] == 42


def _passthrough(fn, *a, **kw):
    return fn(*a, **kw)


def _patch_gh(mock_gh):
    return (
        patch("githost_mcp.tools.github.get_github", return_value=mock_gh),
        patch("githost_mcp.tools.github.github_call", side_effect=_passthrough),
    )


def test_github_get_release_with_published_at(tools):
    rel = _mock_release()
    published = MagicMock()
    published.isoformat.return_value = "2026-05-01T00:00:00"
    rel.published_at = published
    mock_repo = MagicMock()
    mock_repo.get_release.return_value = rel
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_get_release"]("owner/repo", "v1.0.0")
    assert result["tag"] == "v1.0.0"
    assert result["published_at"] == "2026-05-01T00:00:00"


def test_github_get_release_no_published_at(tools):
    """published_at None branch returns None rather than raising."""
    mock_repo = MagicMock()
    mock_repo.get_release.return_value = _mock_release()
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_get_release"]("owner/repo", "v1.0.0")
    assert result["published_at"] is None


def test_github_list_releases(tools):
    mock_repo = MagicMock()
    mock_repo.get_releases.return_value.get_page.return_value = [_mock_release("v1.0.0")]
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_list_releases"]("owner/repo", limit=5)
    assert len(result["releases"]) == 1
    assert result["releases"][0]["tag"] == "v1.0.0"


def _mock_run(run_id=99):
    run = MagicMock()
    run.id = run_id
    run.name = "CI"
    run.status = "completed"
    run.conclusion = "success"
    run.workflow_id = 312
    run.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    run.updated_at.isoformat.return_value = "2026-05-01T00:05:00"
    run.html_url = f"https://github.com/owner/repo/actions/runs/{run_id}"
    return run


def test_github_workflow_list_with_ref(tools):
    """ref filter passes branch kwarg through to get_workflow_runs."""
    mock_repo = MagicMock()
    mock_repo.get_workflow_runs.return_value = [_mock_run()]
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_workflow_list"]("owner/repo", ref="main", limit=3)
    assert result["runs"][0]["id"] == 99
    assert mock_repo.get_workflow_runs.call_args.kwargs == {"branch": "main"}


def test_github_workflow_list_no_ref(tools):
    mock_repo = MagicMock()
    mock_repo.get_workflow_runs.return_value = [_mock_run()]
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_workflow_list"]("owner/repo")
    assert result["runs"][0]["conclusion"] == "success"
    assert mock_repo.get_workflow_runs.call_args.kwargs == {}


def test_github_workflow_status(tools):
    mock_repo = MagicMock()
    mock_repo.get_workflow_run.return_value = _mock_run(run_id=1234)
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_workflow_status"]("owner/repo", 1234)
    assert result["id"] == 1234
    assert result["updated_at"] == "2026-05-01T00:05:00"


def test_github_pr_comments(tools):
    comment = MagicMock()
    comment.id = 7
    comment.user.login = "reviewer"
    comment.body = "LGTM"
    comment.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    comment.updated_at.isoformat.return_value = "2026-05-01T00:01:00"

    mock_pr = MagicMock()
    mock_pr.get_issue_comments.return_value = [comment]
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = mock_pr
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_comments"]("owner/repo", 42)
    assert result["pr"] == 42
    assert result["comments"][0]["author"] == "reviewer"


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("github_create_release", ("owner/repo", "v1")),
        ("github_get_release", ("owner/repo", "v1")),
        ("github_list_releases", ("owner/repo",)),
        ("github_workflow_list", ("owner/repo",)),
        ("github_workflow_status", ("owner/repo", 123)),
        ("github_pr_list", ("owner/repo",)),
        ("github_pr_comments", ("owner/repo", 1)),
    ],
)
def test_github_tool_error_paths(tools, tool_name, args):
    """Every tool routes exceptions through _err rather than propagating."""
    with patch("githost_mcp.tools.github.get_github", side_effect=ValueError("boom")):
        result = tools[tool_name](*args)
    assert "error" in result


def test_github_pr_create(tools):
    pr = MagicMock()
    pr.number = 12
    pr.title = "Add feature"
    pr.state = "open"
    pr.draft = False
    pr.html_url = "https://github.com/owner/repo/pull/12"
    mock_repo = MagicMock()
    mock_repo.create_pull.return_value = pr
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_create"]("owner/repo", "Add feature", "feature", "main")
    assert result["number"] == 12
    assert result["url"].endswith("/12")
    # create_pull is called with keyword base/head per the PyGithub 2.x signature
    assert mock_repo.create_pull.call_args.kwargs["base"] == "main"
    assert mock_repo.create_pull.call_args.kwargs["head"] == "feature"


def test_github_pr_get(tools):
    label = MagicMock()
    label.name = "enhancement"
    pr = MagicMock()
    pr.number = 12
    pr.title = "Add feature"
    pr.state = "open"
    pr.mergeable = True
    pr.merged = False
    pr.draft = False
    pr.head.ref = "feature"
    pr.base.ref = "main"
    pr.html_url = "https://github.com/owner/repo/pull/12"
    pr.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    pr.updated_at.isoformat.return_value = "2026-05-02T00:00:00"
    pr.labels = [label]
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = pr
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_get"]("owner/repo", 12)
    assert result["number"] == 12
    assert result["labels"] == ["enhancement"]
    assert result["base"] == "main"


def test_github_pr_merge(tools):
    status = MagicMock()
    status.merged = True
    status.sha = "abc123"
    status.message = "Pull Request successfully merged"
    pr = MagicMock()
    pr.merge.return_value = status
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = pr
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_merge"]("owner/repo", 12, merge_method="squash", commit_title="t")
    assert result["merged"] is True
    assert result["sha"] == "abc123"
    assert pr.merge.call_args.kwargs["merge_method"] == "squash"
    assert pr.merge.call_args.kwargs["commit_title"] == "t"


def test_github_pr_merge_rejects_bad_method(tools):
    """Invalid merge_method is rejected before any API call."""
    result = tools["github_pr_merge"]("owner/repo", 12, merge_method="fast-forward")
    assert "error" in result
    assert "merge_method must be one of" in result["error"]


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("github_pr_create", ("owner/repo", "t", "feature", "main")),
        ("github_pr_get", ("owner/repo", 1)),
        ("github_pr_merge", ("owner/repo", 1)),
    ],
)
def test_github_pr_tool_error_paths(tools, tool_name, args):
    with patch("githost_mcp.tools.github.get_github", side_effect=ValueError("boom")):
        result = tools[tool_name](*args)
    assert "error" in result


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("github_create_release", ("bad-no-slash", "v1")),
        ("github_get_release", ("bad-no-slash", "v1")),
        ("github_list_releases", ("bad-no-slash",)),
        ("github_workflow_list", ("bad-no-slash",)),
        ("github_workflow_status", ("bad-no-slash", 1)),
        ("github_pr_list", ("bad-no-slash",)),
        ("github_pr_comments", ("bad-no-slash", 1)),
        ("github_pr_create", ("bad-no-slash", "t", "h", "b")),
        ("github_pr_get", ("bad-no-slash", 1)),
        ("github_pr_merge", ("bad-no-slash", 1)),
    ],
)
def test_github_rejects_bad_repo_format(tools, tool_name, args):
    """Every tool rejects a malformed repo before it reaches the client library (IV-01)."""
    result = tools[tool_name](*args)
    assert "error" in result
    assert "owner/repo" in result["error"]


# --------------------------------------------------------------------------
# github_pr_review (method-dispatch)
# --------------------------------------------------------------------------


def _pr_review_mock():
    pr = MagicMock()
    pr.url = "https://api.github.com/repos/owner/repo/pulls/42"
    mock_repo = MagicMock()
    mock_repo.get_pull.return_value = pr
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    return mock_gh, pr


def test_github_pr_review_get_diff(tools):
    mock_gh, pr = _pr_review_mock()
    pr._requester.requestBlob.return_value = (200, {}, "diff --git a/f b/f\n+x\n")
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "get_diff")
    assert result["diff"].startswith("diff --git")
    assert pr._requester.requestBlob.call_args.kwargs["headers"] == {
        "Accept": "application/vnd.github.v3.diff"
    }


def test_github_pr_review_get_files(tools):
    mock_gh, pr = _pr_review_mock()
    f = MagicMock()
    f.filename = "app.py"
    f.status = "modified"
    f.additions = 3
    f.deletions = 1
    f.changes = 4
    f.patch = "@@ -1 +1,3 @@"
    f.previous_filename = None
    pr.get_files.return_value = [f]
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "get_files")
    assert result["files"][0]["filename"] == "app.py"
    assert result["files"][0]["additions"] == 3


def test_github_pr_review_get_reviews(tools):
    mock_gh, pr = _pr_review_mock()
    rev = MagicMock()
    rev.id = 5
    rev.user.login = "reviewer"
    rev.state = "APPROVED"
    rev.body = "ok"
    rev.submitted_at.isoformat.return_value = "2026-05-01T00:00:00"
    pr.get_reviews.return_value = [rev]
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "get_reviews")
    assert result["reviews"][0]["state"] == "APPROVED"
    assert result["reviews"][0]["user"] == "reviewer"


def test_github_pr_review_submit_approve(tools):
    mock_gh, pr = _pr_review_mock()
    rev = MagicMock()
    rev.id = 9
    rev.state = "APPROVED"
    pr.create_review.return_value = rev
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "submit_review", event="APPROVE")
    assert result["review_id"] == 9
    assert result["event"] == "APPROVE"
    # APPROVE needs no body
    assert pr.create_review.call_args.kwargs["event"] == "APPROVE"


def test_github_pr_review_submit_requires_body_for_comment(tools):
    mock_gh, _pr = _pr_review_mock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "submit_review", event="COMMENT")
    assert "error" in result
    assert "body is required" in result["error"]


def test_github_pr_review_submit_rejects_bad_event(tools):
    mock_gh, _pr = _pr_review_mock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "submit_review", event="LGTM")
    assert "error" in result
    assert "event must be one of" in result["error"]


def test_github_pr_review_dismiss(tools):
    mock_gh, pr = _pr_review_mock()
    rev = MagicMock()
    pr.get_review.return_value = rev
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"](
            "owner/repo", 42, "dismiss_review", review_id=7, message="stale"
        )
    assert result["dismissed"] is True
    rev.dismiss.assert_called_once_with("stale")


def test_github_pr_review_dismiss_requires_review_id(tools):
    mock_gh, _pr = _pr_review_mock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_pr_review"]("owner/repo", 42, "dismiss_review", message="x")
    assert "error" in result
    assert "review_id is required" in result["error"]


def test_github_pr_review_rejects_bad_method(tools):
    result = tools["github_pr_review"]("owner/repo", 42, "delete_everything")
    assert "error" in result
    assert "method must be one of" in result["error"]


def test_github_pr_review_rejects_bad_repo(tools):
    result = tools["github_pr_review"]("bad-no-slash", 42, "get_diff")
    assert "error" in result
    assert "owner/repo" in result["error"]


def test_github_pr_review_error_path(tools):
    with patch("githost_mcp.tools.github.get_github", side_effect=ValueError("boom")):
        result = tools["github_pr_review"]("owner/repo", 42, "get_files")
    assert "error" in result

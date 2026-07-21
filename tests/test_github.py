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


# --------------------------------------------------------------------------
# github_actions (method-dispatch)
# --------------------------------------------------------------------------


def test_github_actions_run_workflow(tools):
    wf = MagicMock()
    wf.create_dispatch.return_value = True
    mock_repo = MagicMock()
    mock_repo.get_workflow.return_value = wf
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_actions"](
            "owner/repo", "run_workflow", workflow="ci.yml", ref="main", inputs={"env": "prod"}
        )
    assert result["dispatched"] is True
    wf.create_dispatch.assert_called_once_with("main", {"env": "prod"})


def test_github_actions_run_workflow_requires_ref(tools):
    mock_gh = MagicMock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_actions"]("owner/repo", "run_workflow", workflow="ci.yml")
    assert "error" in result
    assert "workflow and ref are required" in result["error"]


def test_github_actions_rerun_and_cancel(tools):
    run = MagicMock()
    mock_repo = MagicMock()
    mock_repo.get_workflow_run.return_value = run
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        r1 = tools["github_actions"]("owner/repo", "rerun_workflow", run_id=5)
        r2 = tools["github_actions"]("owner/repo", "rerun_failed_jobs", run_id=5)
        r3 = tools["github_actions"]("owner/repo", "cancel_run", run_id=5)
    assert r1["rerun"] is True
    assert r2["rerun_failed_jobs"] is True
    assert r3["cancelled"] is True
    run.rerun.assert_called_once()
    run.rerun_failed_jobs.assert_called_once()
    run.cancel.assert_called_once()


def test_github_actions_rerun_requires_run_id(tools):
    mock_gh = MagicMock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_actions"]("owner/repo", "cancel_run")
    assert "error" in result
    assert "run_id is required" in result["error"]


def test_github_actions_get_run_logs(tools):
    job = MagicMock()
    job.id = 3
    job.name = "build"
    job.status = "completed"
    job.conclusion = "success"
    job.html_url = "https://github.com/owner/repo/actions/runs/5/job/3"
    run = MagicMock()
    run.jobs.return_value = [job]
    mock_repo = MagicMock()
    mock_repo.get_workflow_run.return_value = run
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_actions"]("owner/repo", "get_run_logs", run_id=5)
    assert result["jobs"][0]["name"] == "build"
    assert result["jobs"][0]["conclusion"] == "success"


def test_github_actions_rejects_bad_method(tools):
    result = tools["github_actions"]("owner/repo", "delete_repo")
    assert "error" in result
    assert "method must be one of" in result["error"]


def test_github_actions_error_path(tools):
    with patch("githost_mcp.tools.github.get_github", side_effect=ValueError("boom")):
        result = tools["github_actions"]("owner/repo", "cancel_run", run_id=1)
    assert "error" in result


# --------------------------------------------------------------------------
# github_release_update / github_release_delete
# --------------------------------------------------------------------------


def test_github_release_update_partial(tools):
    existing = _mock_release()
    existing.title = "old"
    existing.body = "old body"
    existing.draft = False
    existing.prerelease = False
    updated = _mock_release()
    updated.title = "new"
    existing.update_release.return_value = updated
    mock_repo = MagicMock()
    mock_repo.get_release.return_value = existing
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_release_update"]("owner/repo", "v1.0.0", name="new")
    assert result["name"] == "new"
    # omitted fields default to existing values (not wiped)
    args = existing.update_release.call_args.args
    assert args[0] == "new"  # name
    assert args[1] == "old body"  # body kept


def test_github_release_delete(tools):
    release = _mock_release()
    mock_repo = MagicMock()
    mock_repo.get_release.return_value = release
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_release_delete"]("owner/repo", "v1.0.0")
    assert result["deleted"] is True
    release.delete_release.assert_called_once()


def test_github_release_update_bad_repo(tools):
    result = tools["github_release_update"]("bad-no-slash", "v1")
    assert "error" in result
    assert "owner/repo" in result["error"]


def test_github_release_delete_error_path(tools):
    with patch("githost_mcp.tools.github.get_github", side_effect=ValueError("boom")):
        result = tools["github_release_delete"]("owner/repo", "v1")
    assert "error" in result


# --------------------------------------------------------------------------
# github_issue_read / github_issue_write (method-dispatch)
# --------------------------------------------------------------------------


def _issue_mock(number=1, is_pr=False):
    i = MagicMock()
    i.number = number
    i.title = "An issue"
    i.state = "open"
    i.body = "details"
    i.user.login = "author"
    lbl = MagicMock()
    lbl.name = "bug"
    i.labels = [lbl]
    a = MagicMock()
    a.login = "assignee1"
    i.assignees = [a]
    i.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    i.updated_at.isoformat.return_value = "2026-05-02T00:00:00"
    i.html_url = f"https://github.com/owner/repo/issues/{number}"
    i.pull_request = MagicMock() if is_pr else None
    return i


def test_github_issue_read_list_excludes_prs(tools):
    issue = _issue_mock(1, is_pr=False)
    pr = _issue_mock(2, is_pr=True)
    mock_repo = MagicMock()
    mock_repo.get_issues.return_value = [issue, pr]
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_read"]("owner/repo", "list")
    assert len(result["issues"]) == 1
    assert result["issues"][0]["number"] == 1
    assert result["issues"][0]["labels"] == ["bug"]


def test_github_issue_read_get(tools):
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = _issue_mock(3)
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_read"]("owner/repo", "get", issue_number=3)
    assert result["number"] == 3
    assert result["assignees"] == ["assignee1"]


def test_github_issue_read_get_requires_number(tools):
    mock_gh = MagicMock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_read"]("owner/repo", "get")
    assert "error" in result
    assert "issue_number is required" in result["error"]


def test_github_issue_read_comments(tools):
    c = MagicMock()
    c.id = 8
    c.user.login = "commenter"
    c.body = "hi"
    c.created_at.isoformat.return_value = "2026-05-01T00:00:00"
    issue = _issue_mock(3)
    issue.get_comments.return_value = [c]
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_read"]("owner/repo", "comments", issue_number=3)
    assert result["comments"][0]["author"] == "commenter"


def test_github_issue_write_create(tools):
    created = _issue_mock(10)
    mock_repo = MagicMock()
    mock_repo.create_issue.return_value = created
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_write"](
            "owner/repo", "create", title="New bug", body="repro", labels=["bug"]
        )
    assert result["number"] == 10
    assert mock_repo.create_issue.call_args.kwargs["labels"] == ["bug"]


def test_github_issue_write_create_requires_title(tools):
    mock_gh = MagicMock()
    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_write"]("owner/repo", "create")
    assert "error" in result
    assert "title is required" in result["error"]


def test_github_issue_write_close_and_reopen(tools):
    issue = _issue_mock(5)
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        r1 = tools["github_issue_write"]("owner/repo", "close", issue_number=5)
        r2 = tools["github_issue_write"]("owner/repo", "reopen", issue_number=5)
    assert r1["state"] == "closed"
    assert r2["state"] == "open"
    assert issue.edit.call_args_list[0].kwargs["state"] == "closed"
    assert issue.edit.call_args_list[1].kwargs["state"] == "open"


def test_github_issue_write_add_comment(tools):
    c = MagicMock()
    c.id = 11
    c.html_url = "https://github.com/owner/repo/issues/5#issuecomment-11"
    issue = _issue_mock(5)
    issue.create_comment.return_value = c
    mock_repo = MagicMock()
    mock_repo.get_issue.return_value = issue
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    p1, p2 = _patch_gh(mock_gh)
    with p1, p2:
        result = tools["github_issue_write"](
            "owner/repo", "add_comment", issue_number=5, comment="LGTM"
        )
    assert result["comment_id"] == 11
    issue.create_comment.assert_called_once_with("LGTM")


def test_github_issue_write_rejects_bad_method(tools):
    result = tools["github_issue_write"]("owner/repo", "explode")
    assert "error" in result
    assert "method must be one of" in result["error"]

"""github_list reads every page inside the call's span (CodeRabbit CR-02, p4).

github_call(pr.get_reviews) returned PyGithub's lazy PaginatedList and made no request;
every page was fetched later, outside the span. Probed 2026-11-08: 3 of 3 page requests
ran outside. This drives a real PaginatedList against a stubbed Requester.
"""

import contextlib
from unittest.mock import MagicMock, patch

import github
import pytest
from github.CheckRun import CheckRun
from github.PullRequest import PullRequest
from github.Requester import Requester
from github.WorkflowRun import WorkflowRun

from githost_mcp._providers import github_client
from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config


def _harness(pages: int):
    state = {"in_span": False}
    calls: list[bool] = []

    @contextlib.contextmanager
    def fake_child(*_a, **_k):
        state["in_span"] = True
        try:
            yield None
        finally:
            state["in_span"] = False

    def fake_request(self, verb, url, parameters=None, headers=None, input=None, **kw):
        page = int(url.split("page=")[1].split("&")[0]) if "page=" in url else 1
        calls.append(state["in_span"])
        hdrs = {}
        if page < pages:
            hdrs["link"] = (
                f'<https://api.github.com/repos/o/r/pulls/1/reviews?page={page + 1}>; rel="next"'
            )
        return hdrs, [{"id": page * 10 + i, "state": "COMMENTED"} for i in range(2)]

    gh = github.Github(auth=github.Auth.Token("x" * 40))
    pr = PullRequest(
        gh._Github__requester,
        {},
        {"url": "https://api.github.com/repos/o/r/pulls/1", "number": 1},
        completed=True,
    )
    patches = (
        patch.object(github_client, "current_child", fake_child),
        patch.object(Requester, "requestJsonAndCheck", fake_request),
    )
    return pr, calls, patches


def test_github_list_fetches_every_page_inside_the_span():
    pr, calls, (p1, p2) = _harness(pages=3)
    with p1, p2:
        reviews = github_client.github_list(pr.get_reviews)
    assert len(reviews) == 6
    assert calls == [True, True, True]


def test_github_call_alone_leaves_the_pages_outside_the_span():
    """The control: the shape CR-02 described still behaves that way through github_call."""
    pr, calls, (p1, p2) = _harness(pages=3)
    with p1, p2:
        lazy = github_client.github_call(pr.get_reviews)
        assert calls == []
        list(lazy)
    assert calls == [False, False, False]


def test_github_list_limit_stops_paging():
    pr, calls, (p1, p2) = _harness(pages=3)
    with p1, p2:
        reviews = github_client.github_list(pr.get_reviews, limit=3)
    assert len(reviews) == 3
    assert calls == [True, True]


# --- vikunja#1451: the three reads that still paged outside the span ----------------------
#
# Each tool is driven end to end with a real PyGithub object whose pages come from a
# stubbed Requester, so the assertion is about requests PyGithub actually made, not
# about which helper the code names.

API = "https://api.github.com/repos/o/r"


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "t" * 36)
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.github import register

    register(MockMCP())
    return registered


def _paged(pages: int, item, wrap: str | None = None):
    """A Requester stub serving `pages` pages of two items each, recording span state."""
    state = {"in_span": False}
    calls: list[bool] = []

    @contextlib.contextmanager
    def fake_child(*_a, **_k):
        state["in_span"] = True
        try:
            yield None
        finally:
            state["in_span"] = False

    def fake_request(self, verb, url, parameters=None, headers=None, input=None, **kw):
        page = int((parameters or {}).get("page") or 1)
        if "page=" in url:
            page = int(url.split("page=")[1].split("&")[0])
        calls.append(state["in_span"])
        hdrs = {}
        if page < pages:
            base = url.split("?")[0]
            hdrs["link"] = f'<{base}?page={page + 1}>; rel="next"'
        items = [item(page * 10 + i) for i in range(2)]
        return hdrs, ({"total_count": pages * 2, wrap: items} if wrap else items)

    patches = (
        patch.object(github_client, "current_child", fake_child),
        patch.object(Requester, "requestJsonAndCheck", fake_request),
    )
    return calls, patches


def _requester():
    return github.Github(auth=github.Auth.Token("x" * 40))._Github__requester


def _gh_mock():
    return patch("githost_mcp.tools.github.get_github", return_value=MagicMock())


def test_pr_comments_page_inside_the_span(tools):
    calls, (p1, p2) = _paged(
        3,
        lambda i: {
            "id": i,
            "user": {"login": "u"},
            "body": "b",
            "created_at": "2026-11-01T00:00:00Z",
            "updated_at": "2026-11-01T00:00:00Z",
        },
    )
    pr = PullRequest(
        _requester(),
        {},
        {"url": f"{API}/pulls/1", "number": 1, "issue_url": f"{API}/issues/1"},
        completed=True,
    )
    with _gh_mock() as gh, p1, p2:
        gh.return_value.get_repo.return_value.get_pull.return_value = pr
        r = tools["github_pr_comments"]("o/r", 1)
    assert len(r["comments"]) == 6
    assert calls == [True, True, True]


@pytest.mark.parametrize("action", ["list_jobs", "get_run_logs"])
def test_workflow_jobs_page_inside_the_span(tools, action):
    calls, (p1, p2) = _paged(
        2,
        lambda i: {
            "id": i,
            "name": f"j{i}",
            "status": "completed",
            "conclusion": "success",
            "url": f"{API}/actions/jobs/{i}",
            "html_url": f"https://github.com/o/r/actions/runs/5/job/{i}",
            "started_at": "2026-11-01T00:00:00Z",
            "completed_at": "2026-11-01T00:01:00Z",
            "steps": [],
        },
        wrap="jobs",
    )
    run = WorkflowRun(
        _requester(), {}, {"id": 5, "jobs_url": f"{API}/actions/runs/5/jobs"}, completed=True
    )
    with _gh_mock() as gh, p1, p2:
        gh.return_value.get_repo.return_value.get_workflow_run.return_value = run
        r = tools["github_actions"]("o/r", action, run_id=5)
    assert "jobs" in r, r
    assert len(r["jobs"]) == 4
    assert calls == [True, True]


def _commit_with(runs):
    combined = MagicMock(state="failure", statuses=[], total_count=0)
    commit = MagicMock(sha="a" * 40)
    commit.get_combined_status.return_value = combined
    commit.get_check_runs.return_value = runs
    return commit


def _annotation(i):
    return {
        "path": "t.py",
        "start_line": i,
        "end_line": i,
        "annotation_level": "failure",
        "message": "m",
    }


def test_annotations_page_inside_the_span(tools):
    calls, (p1, p2) = _paged(3, _annotation)
    cr = CheckRun(
        _requester(),
        {},
        {"id": 7, "name": "test", "conclusion": "failure", "url": f"{API}/check-runs/7"},
        completed=True,
    )
    with _gh_mock() as gh, p1, p2:
        gh.return_value.get_repo.return_value.get_commit.return_value = _commit_with([cr])
        r = tools["github_commit_checks"]("o/r", "main", annotations=True)
    assert len(r["annotations"]) == 6
    assert r["annotations_truncated"] is False
    assert calls == [True, True, True]


def test_annotation_cap_stops_paging_and_flags_truncation(tools, monkeypatch):
    """The 50-annotation cap survives the move: here 3, so 3 + 1 items, two pages."""
    from githost_mcp.tools import github as github_tools

    monkeypatch.setattr(github_tools, "_MAX_ANNOTATIONS", 3)
    calls, (p1, p2) = _paged(5, _annotation)
    cr = CheckRun(
        _requester(),
        {},
        {"id": 7, "name": "test", "conclusion": "failure", "url": f"{API}/check-runs/7"},
        completed=True,
    )
    with _gh_mock() as gh, p1, p2:
        gh.return_value.get_repo.return_value.get_commit.return_value = _commit_with([cr])
        r = tools["github_commit_checks"]("o/r", "main", annotations=True)
    assert len(r["annotations"]) == 3
    assert r["annotations_truncated"] is True
    assert calls == [True, True]

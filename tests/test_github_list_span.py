"""github_list reads every page inside the call's span (CodeRabbit CR-02, p4).

github_call(pr.get_reviews) returned PyGithub's lazy PaginatedList and made no request;
every page was fetched later, outside the span. Probed 2026-11-08: 3 of 3 page requests
ran outside. This drives a real PaginatedList against a stubbed Requester.
"""

import contextlib
from unittest.mock import patch

import github
from github.PullRequest import PullRequest
from github.Requester import Requester

from githost_mcp._providers import github_client


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

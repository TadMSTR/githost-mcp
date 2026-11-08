"""Gitea CI reads (githost-scoped-2026-11 p1, Phase 3): bounded job log, commit status."""

import json

import httpx
import pytest
import respx

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config

API = "https://gitea.example.com/api/v1/repos/testowner/repo"
GA = f"{API}/actions"


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


# --- gitea_actions get_job_log ------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_is_bounded_and_tails_by_default(tools):
    text = "".join(f"line {i}\n" for i in range(5000))
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(return_value=httpx.Response(200, text=text))
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3)
    assert "log" not in r
    assert len(r["lines"]) == 500
    assert r["lines"][-1] == "line 4999"
    assert r["total_lines"] == 5000 and r["truncated"] is True


@pytest.mark.asyncio
async def test_job_log_is_scrubbed(tools):
    text = (
        "fetch https://ci:tok3nvalue99@gitea.example.com/x.git\n"
        "use gitea_fake_token_1234567890abcdef\n"
    )
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(return_value=httpx.Response(200, text=text))
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3)
    joined = "\n".join(r["lines"])
    assert "tok3nvalue99" not in joined
    assert "gitea_fake_token" not in joined


@pytest.mark.asyncio
async def test_job_log_range_and_grep(tools):
    text = "a\nb\nError: boom\nc\n"
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(return_value=httpx.Response(200, text=text))
        r = await tools["gitea_actions"](
            "testowner/repo", "get_job_log", job_id=3, mode="range", offset=1, log_limit=2
        )
        g = await tools["gitea_actions"](
            "testowner/repo", "get_job_log", job_id=3, grep="error", ignore_case=True
        )
    assert r["lines"] == ["b", "Error: boom"]
    assert g["segments"] == [{"from": 2, "to": 3, "lines": ["Error: boom"]}]


@pytest.mark.asyncio
async def test_job_log_bad_view_args_rejected_before_request(tools):
    # CR-17: a mock with no routes turned any request into an error the tool caught, so
    # the test passed even if validation ran after the request. Route it and assert it
    # was never called, and that the error is the validation one.
    with respx.mock:
        route = respx.get(f"{GA}/jobs/3/logs").mock(return_value=httpx.Response(200, text="x\n"))
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3, context=99)
    assert "context must be between" in r["error"]
    assert not route.called


@pytest.mark.asyncio
async def test_job_log_audit_has_no_content_or_grep_text(tools, tmp_path):
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(
            return_value=httpx.Response(200, text="PRIVATE-LINE\nneedle-q\n")
        )
        await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3, grep="needle-q")
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "PRIVATE-LINE" not in raw and "needle-q" not in raw
    rec = json.loads(raw.splitlines()[-1])
    assert rec["params"]["grep"] is True and rec["params"]["job_id"] == 3


@pytest.mark.asyncio
async def test_list_runs_limit_still_independent_of_log_limit(tools):
    with respx.mock:
        route = respx.get(f"{GA}/runs").mock(
            return_value=httpx.Response(200, json={"workflow_runs": []})
        )
        await tools["gitea_actions"]("testowner/repo", "list_runs", limit=5)
    assert route.calls.last.request.url.params["limit"] == "5"


# --- gitea_commit_status ------------------------------------------------------------------

# Live shape, host-forge/scripts main on 2026-11-07: Gitea names the per-context field
# `status`, not `state`.
STATUS = {
    "state": "failure",
    "sha": "91e53aec37b952e251bcb0d7c8c6af84526e96e6",
    "total_count": 1,
    "statuses": [
        {
            "id": 1,
            "status": "failure",
            "context": "ci/woodpecker/push/woodpecker",
            "description": "Pipeline failed",
            "target_url": "https://ci.example.com/repos/2/pipeline/1249",
            "created_at": "2026-11-07T21:00:00Z",
            "updated_at": "2026-11-07T21:05:00Z",
            "creator": {"login": "ted"},
            "url": "https://gitea.example.com/api/v1/...",
        }
    ],
}


@pytest.mark.asyncio
async def test_commit_status_by_sha(tools):
    sha = STATUS["sha"]
    with respx.mock:
        respx.get(f"{API}/commits/{sha}/status").mock(return_value=httpx.Response(200, json=STATUS))
        r = await tools["gitea_commit_status"]("testowner/repo", sha)
    assert r["state"] == "failure" and r["sha"] == sha
    assert r["statuses"] == [
        {
            "context": "ci/woodpecker/push/woodpecker",
            "state": "failure",
            "description": "Pipeline failed",
            "target_url": "https://ci.example.com/repos/2/pipeline/1249",
            "updated_at": "2026-11-07T21:05:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_commit_status_branch_with_slash_is_one_encoded_segment(tools):
    with respx.mock:
        route = respx.get(url__startswith=f"{API}/commits/").mock(
            return_value=httpx.Response(200, json=STATUS)
        )
        await tools["gitea_commit_status"]("testowner/repo", "feature/deploy-guardrails")
    path = route.calls.last.request.url.raw_path.split(b"?", 1)[0]
    assert path.endswith(b"/commits/feature%2Fdeploy-guardrails/status")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    ["", "-main", "../../admin", "a..b", "main?x=1", "a b", "a\nb", "x" * 256, "a//b", "a/./b"],
)
async def test_commit_status_rejects_bad_ref_before_request(tools, ref):
    with respx.mock:
        r = await tools["gitea_commit_status"]("testowner/repo", ref)
    assert "error" in r


@pytest.mark.asyncio
async def test_commit_status_hash_in_a_branch_name_is_encoded_not_a_fragment(tools):
    """`#` became valid in 0.21.0 (vikunja#1429); it must reach Gitea as %23 in the path."""
    with respx.mock:
        route = respx.get(url__startswith=f"{API}/commits/").mock(
            return_value=httpx.Response(200, json=STATUS)
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "main#x")
    assert "error" not in r
    url = route.calls.last.request.url
    assert url.raw_path.split(b"?", 1)[0].endswith(b"/commits/main%23x/status")
    assert url.fragment == ""


def _status_page(contexts, state):
    return {
        "sha": "a" * 40,
        "state": state,
        "total_count": len(contexts),
        "statuses": [{"context": c, "status": st} for c, st in contexts],
    }


@pytest.mark.asyncio
async def test_commit_status_reads_every_page_and_the_worst_state_wins(tools):
    """Gitea's combined state covers one page (vikunja#1430): a failure on page 2 must not
    read as success."""
    page1 = _status_page([(f"ci/{i}", "success") for i in range(50)], "success")
    page2 = _status_page([("ci/late", "failure")], "failure")
    with respx.mock:
        route = respx.get(url__startswith=f"{API}/commits/main/status").mock(
            side_effect=[
                httpx.Response(200, json=page1, headers={"X-Total-Count": "51"}),
                httpx.Response(200, json=page2, headers={"X-Total-Count": "51"}),
            ]
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "main")
    assert r["state"] == "failure"
    assert r["total_count"] == 51
    assert len(r["statuses"]) == 51
    assert r["statuses_truncated"] is False
    assert [c.request.url.params["page"] for c in route.calls] == ["1", "2"]


@pytest.mark.asyncio
async def test_commit_status_one_page_keeps_giteas_state(tools):
    with respx.mock:
        route = respx.get(url__startswith=f"{API}/commits/main/status").mock(
            return_value=httpx.Response(
                200,
                json=_status_page([("ci", "pending")], "pending"),
                headers={"X-Total-Count": "1"},
            )
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "main")
    assert r["state"] == "pending" and r["statuses_truncated"] is False
    assert len(route.calls) == 1


@pytest.mark.asyncio
async def test_commit_status_beyond_the_page_bound_is_flagged_truncated(tools):
    from githost_mcp.tools import gitea

    pages = [
        httpx.Response(
            200,
            json=_status_page([(f"c{p}-{i}", "success") for i in range(50)], "success"),
            headers={"X-Total-Count": "9999"},
        )
        for p in range(gitea.STATUS_MAX_PAGES)
    ]
    with respx.mock:
        route = respx.get(url__startswith=f"{API}/commits/main/status").mock(side_effect=pages)
        r = await tools["gitea_commit_status"]("testowner/repo", "main")
    assert len(route.calls) == gitea.STATUS_MAX_PAGES
    assert r["statuses_truncated"] is True
    assert r["total_count"] == 9999


@pytest.mark.parametrize(
    ("a", "b", "worst"),
    [
        ("success", "failure", "failure"),
        ("failure", "success", "failure"),
        ("pending", "success", "pending"),
        ("error", "failure", "error"),
        ("warning", "pending", "pending"),
        ("success", "weird", "weird"),
        ("failure", "weird", "failure"),
        (None, "success", "success"),
        ("success", None, "success"),
    ],
)
def test_worst_status_state(a, b, worst):
    from githost_mcp.tools.gitea import worst_status_state

    assert worst_status_state(a, b) == worst


@pytest.mark.asyncio
async def test_commit_status_bounds_and_scrubs_description(tools):
    body = dict(STATUS)
    body["statuses"] = [dict(STATUS["statuses"][0], description="https://u:pw77777@h/ " * 50)]
    with respx.mock:
        respx.get(url__startswith=f"{API}/commits/").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "main")
    desc = r["statuses"][0]["description"]
    assert "pw77777" not in desc and len(desc) <= 301


@pytest.mark.asyncio
async def test_commit_status_unknown_ref_is_an_error(tools):
    with respx.mock:
        respx.get(url__startswith=f"{API}/commits/").mock(
            return_value=httpx.Response(404, text="not found")
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "nope")
    assert "404" in r["error"]


# --- pre-audit fixes ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_log_download_is_capped_and_flagged(tools, monkeypatch):
    from githost_mcp import logview

    monkeypatch.setattr(logview, "DOWNLOAD_MAX", 30)
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(
            return_value=httpx.Response(200, text="".join(f"line {i}\n" for i in range(100)))
        )
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3, mode="head")
    assert r["download_cut"] is True
    assert sum(len(x) + 1 for x in r["lines"]) <= 31


@pytest.mark.asyncio
async def test_job_log_http_error_still_reported_through_the_capped_read(tools):
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(return_value=httpx.Response(404, text="no job"))
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3)
    assert "404" in r["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repo", ["../..", "owner/..", "owner/.", "./r", "owner/repo\n"])
async def test_repo_dot_segments_and_newline_refused(tools, repo):
    with respx.mock:
        r1 = await tools["gitea_commit_status"](repo, "main")
        r2 = await tools["gitea_actions"](repo, "list_runs")
        r3 = await tools["gitea_pr_list"](repo)
    for r in (r1, r2, r3):
        assert "'owner/repo' format" in r.get("error", "")


@pytest.mark.asyncio
async def test_commit_status_ref_with_trailing_newline_refused(tools):
    with respx.mock:
        r = await tools["gitea_commit_status"]("testowner/repo", "main\n")
    assert "ref must be" in r.get("error", "")


@pytest.mark.asyncio
async def test_job_log_from_a_compressing_server(tools):
    # CR-03: rebuilding a Response with `content-encoding: gzip` over decoded bytes raised
    # DecodingError, so every read from a compressing Gitea or proxy failed.
    import gzip

    body = gzip.compress(b"one\ntwo\n")
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(
            return_value=httpx.Response(200, content=body, headers={"content-encoding": "gzip"})
        )
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3)
    assert r["lines"] == ["one", "two"]


@pytest.mark.asyncio
async def test_job_log_cut_drops_partial_last_line(tools, monkeypatch):
    # F-03 at the Gitea call site.
    from githost_mcp import logview

    monkeypatch.setattr(logview, "DOWNLOAD_MAX", 12)
    with respx.mock:
        respx.get(f"{GA}/jobs/3/logs").mock(
            return_value=httpx.Response(200, text="first\nsecret-prefix-of-a-token\n")
        )
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3)
    assert r["lines"] == ["first"] and r["download_cut"] is True


@pytest.mark.asyncio
async def test_commit_status_bounds_and_scrubs_target_url(tools):
    """#1431 F-07: target_url is chosen by whoever posts the status."""
    body = dict(STATUS)
    body["statuses"] = [dict(STATUS["statuses"][0], target_url="https://u:pw88888@h/" + "x" * 2000)]
    with respx.mock:
        respx.get(url__startswith=f"{API}/commits/").mock(
            return_value=httpx.Response(200, json=body)
        )
        r = await tools["gitea_commit_status"]("testowner/repo", "main")
    url = r["statuses"][0]["target_url"]
    assert "pw88888" not in url and len(url) <= 301

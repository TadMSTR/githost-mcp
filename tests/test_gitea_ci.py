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
    with respx.mock:
        r = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=3, context=99)
    assert "error" in r


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
    assert route.calls.last.request.url.raw_path.endswith(
        b"/commits/feature%2Fdeploy-guardrails/status"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    ["", "-main", "../../admin", "a..b", "main?x=1", "main#x", "a b", "a\nb", "x" * 256, "a//b"],
)
async def test_commit_status_rejects_bad_ref_before_request(tools, ref):
    with respx.mock:
        r = await tools["gitea_commit_status"]("testowner/repo", ref)
    assert "error" in r


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

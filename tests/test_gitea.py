"""Tests for Gitea tools with respx HTTP mocks."""

import httpx
import pytest
import respx

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
    mock_data = [
        {
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "html_url": "https://gitea.example.com/testowner/repo/releases/tag/v1.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-05-01",
        }
    ]

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
    mock_response = {
        "id": 1,
        "tag_name": "v1.0.0",
        "html_url": "https://gitea.example.com/testowner/repo/releases/1",
    }

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


@pytest.mark.asyncio
async def test_gitea_pr_create_success(tools):
    fns = tools
    mock_response = {
        "number": 5,
        "title": "My feature",
        "html_url": "https://gitea.example.com/testowner/repo/pulls/5",
        "state": "open",
    }
    with respx.mock:
        respx.post("https://gitea.example.com/api/v1/repos/testowner/repo/pulls").mock(
            return_value=httpx.Response(201, json=mock_response)
        )
        result = await fns["gitea_pr_create"](
            "testowner/repo", title="My feature", head="feature-branch", base="main"
        )
    assert result["number"] == 5
    assert result["url"] == "https://gitea.example.com/testowner/repo/pulls/5"


@pytest.mark.asyncio
async def test_gitea_pr_get_success(tools):
    fns = tools
    mock_response = {
        "number": 3,
        "title": "Fix bug",
        "state": "open",
        "mergeable": True,
        "head": {"label": "feature"},
        "base": {"label": "main"},
        "html_url": "https://gitea.example.com/testowner/repo/pulls/3",
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-05-02T00:00:00Z",
        "labels": [{"name": "bug"}],
    }
    with respx.mock:
        respx.get("https://gitea.example.com/api/v1/repos/testowner/repo/pulls/3").mock(
            return_value=httpx.Response(200, json=mock_response)
        )
        result = await fns["gitea_pr_get"]("testowner/repo", 3)
    assert result["number"] == 3
    assert result["mergeable"] is True
    assert "bug" in result["labels"]


@pytest.mark.asyncio
async def test_gitea_pr_comment_success(tools):
    fns = tools
    mock_response = {
        "id": 42,
        "html_url": "https://gitea.example.com/testowner/repo/issues/3#issuecomment-42",
        "created_at": "2026-05-01T00:00:00Z",
    }
    with respx.mock:
        respx.post("https://gitea.example.com/api/v1/repos/testowner/repo/issues/3/comments").mock(
            return_value=httpx.Response(201, json=mock_response)
        )
        result = await fns["gitea_pr_comment"]("testowner/repo", 3, "LGTM")
    assert result["id"] == 42


@pytest.mark.asyncio
async def test_gitea_pr_merge_success(tools):
    fns = tools
    with respx.mock:
        respx.post("https://gitea.example.com/api/v1/repos/testowner/repo/pulls/3/merge").mock(
            return_value=httpx.Response(204)
        )
        result = await fns["gitea_pr_merge"]("testowner/repo", 3)
    assert result["merged"] is True
    assert result["pr_number"] == 3


@pytest.mark.asyncio
async def test_gitea_pr_merge_conflict(tools):
    fns = tools
    with respx.mock:
        respx.post("https://gitea.example.com/api/v1/repos/testowner/repo/pulls/3/merge").mock(
            return_value=httpx.Response(409, text="merge conflict")
        )
        result = await fns["gitea_pr_merge"]("testowner/repo", 3)
    assert "error" in result
    assert "merged" not in result


# --------------------------------------------------------------------------
# gitea_pr_review (method-dispatch)
# --------------------------------------------------------------------------

_GT_BASE = "https://gitea.example.com/api/v1/repos/testowner/repo/pulls/3"


@pytest.mark.asyncio
async def test_gitea_pr_review_get_diff(tools):
    with respx.mock:
        respx.get(f"{_GT_BASE}.diff").mock(
            return_value=httpx.Response(200, text="diff --git a/f b/f\n+x\n")
        )
        result = await tools["gitea_pr_review"]("testowner/repo", 3, "get_diff")
    assert result["diff"].startswith("diff --git")


@pytest.mark.asyncio
async def test_gitea_pr_review_get_files(tools):
    mock_files = [
        {"filename": "app.py", "status": "modified", "additions": 2, "deletions": 1, "changes": 3}
    ]
    with respx.mock:
        respx.get(f"{_GT_BASE}/files").mock(return_value=httpx.Response(200, json=mock_files))
        result = await tools["gitea_pr_review"]("testowner/repo", 3, "get_files")
    assert result["files"][0]["filename"] == "app.py"
    assert result["files"][0]["additions"] == 2


@pytest.mark.asyncio
async def test_gitea_pr_review_submit_approve_maps_event(tools):
    captured = {}

    def _capture(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 11, "state": "APPROVED"})

    with respx.mock:
        respx.post(f"{_GT_BASE}/reviews").mock(side_effect=_capture)
        result = await tools["gitea_pr_review"](
            "testowner/repo", 3, "submit_review", event="APPROVE"
        )
    assert result["review_id"] == 11
    # APPROVE is translated to Gitea's APPROVED
    assert captured["event"] == "APPROVED"


@pytest.mark.asyncio
async def test_gitea_pr_review_submit_requires_body_for_request_changes(tools):
    result = await tools["gitea_pr_review"](
        "testowner/repo", 3, "submit_review", event="REQUEST_CHANGES"
    )
    assert "error" in result
    assert "body is required" in result["error"]


@pytest.mark.asyncio
async def test_gitea_pr_review_dismiss(tools):
    with respx.mock:
        respx.post(f"{_GT_BASE}/reviews/5/dismissals").mock(
            return_value=httpx.Response(200, json={"id": 5})
        )
        result = await tools["gitea_pr_review"](
            "testowner/repo", 3, "dismiss_review", review_id=5, message="stale"
        )
    assert result["dismissed"] is True


@pytest.mark.asyncio
async def test_gitea_pr_review_rejects_bad_method(tools):
    result = await tools["gitea_pr_review"]("testowner/repo", 3, "nuke")
    assert "error" in result
    assert "method must be one of" in result["error"]


@pytest.mark.asyncio
async def test_gitea_pr_review_rejects_bad_repo(tools):
    result = await tools["gitea_pr_review"]("bad-no-slash", 3, "get_diff")
    assert "error" in result
    assert "owner/repo" in result["error"]


# --------------------------------------------------------------------------
# gitea_actions (method-dispatch)
# --------------------------------------------------------------------------

_GA_BASE = "https://gitea.example.com/api/v1/repos/testowner/repo/actions"


@pytest.mark.asyncio
async def test_gitea_actions_list_runs(tools):
    payload = {
        "total_count": 1,
        "workflow_runs": [
            {
                "id": 7,
                "status": "success",
                "conclusion": "success",
                "event": "push",
                "head_branch": "main",
                "run_number": 12,
                "display_title": "Fix bug",
                "html_url": "https://gitea.example.com/testowner/repo/actions/runs/7",
            }
        ],
    }
    with respx.mock:
        respx.get(url__startswith=f"{_GA_BASE}/runs").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await tools["gitea_actions"]("testowner/repo", "list_runs")
    assert result["runs"][0]["id"] == 7
    assert result["runs"][0]["run_number"] == 12


@pytest.mark.asyncio
async def test_gitea_actions_get_run(tools):
    run = {"id": 7, "status": "running", "conclusion": None, "event": "push"}
    with respx.mock:
        respx.get(f"{_GA_BASE}/runs/7").mock(return_value=httpx.Response(200, json=run))
        result = await tools["gitea_actions"]("testowner/repo", "get_run", run_id=7)
    assert result["id"] == 7
    assert result["status"] == "running"


@pytest.mark.asyncio
async def test_gitea_actions_list_jobs(tools):
    payload = {"total_count": 1, "jobs": [{"id": 1, "name": "build", "status": "success"}]}
    with respx.mock:
        respx.get(f"{_GA_BASE}/runs/7/jobs").mock(return_value=httpx.Response(200, json=payload))
        result = await tools["gitea_actions"]("testowner/repo", "list_jobs", run_id=7)
    assert result["jobs"][0]["name"] == "build"


@pytest.mark.asyncio
async def test_gitea_actions_get_job_log(tools):
    with respx.mock:
        respx.get(f"{_GA_BASE}/jobs/1/logs").mock(
            return_value=httpx.Response(200, text="step 1\nstep 2\n")
        )
        result = await tools["gitea_actions"]("testowner/repo", "get_job_log", job_id=1)
    assert "step 1" in result["log"]


@pytest.mark.asyncio
async def test_gitea_actions_dispatch_workflow(tools):
    with respx.mock:
        respx.post(f"{_GA_BASE}/workflows/ci.yml/dispatches").mock(return_value=httpx.Response(204))
        result = await tools["gitea_actions"](
            "testowner/repo", "dispatch_workflow", workflow="ci.yml", ref="main"
        )
    assert result["dispatched"] is True


@pytest.mark.asyncio
async def test_gitea_actions_rerun_run(tools):
    with respx.mock:
        respx.post(f"{_GA_BASE}/runs/7/rerun").mock(return_value=httpx.Response(201))
        result = await tools["gitea_actions"]("testowner/repo", "rerun_run", run_id=7)
    assert result["rerun_run"] is True


@pytest.mark.asyncio
async def test_gitea_actions_dispatch_requires_ref(tools):
    result = await tools["gitea_actions"]("testowner/repo", "dispatch_workflow", workflow="ci.yml")
    assert "error" in result
    assert "workflow and ref are required" in result["error"]


@pytest.mark.asyncio
async def test_gitea_actions_rejects_bad_method(tools):
    result = await tools["gitea_actions"]("testowner/repo", "cancel_run")
    assert "error" in result
    assert "method must be one of" in result["error"]


# --------------------------------------------------------------------------
# gitea_release_update / gitea_release_delete
# --------------------------------------------------------------------------

_REL_BASE = "https://gitea.example.com/api/v1/repos/testowner/repo/releases"


@pytest.mark.asyncio
async def test_gitea_release_update(tools):
    captured = {}

    def _capture(request):
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": 5,
                "tag_name": "v1.0.0",
                "name": "renamed",
                "html_url": f"{_REL_BASE}/5",
                "draft": False,
                "prerelease": True,
            },
        )

    with respx.mock:
        respx.get(f"{_REL_BASE}/tags/v1.0.0").mock(
            return_value=httpx.Response(200, json={"id": 5, "tag_name": "v1.0.0"})
        )
        respx.patch(f"{_REL_BASE}/5").mock(side_effect=_capture)
        result = await tools["gitea_release_update"](
            "testowner/repo", "v1.0.0", name="renamed", prerelease=True
        )
    assert result["name"] == "renamed"
    assert result["prerelease"] is True
    # only supplied fields are sent
    assert captured == {"name": "renamed", "prerelease": True}


@pytest.mark.asyncio
async def test_gitea_release_delete(tools):
    with respx.mock:
        respx.delete(f"{_REL_BASE}/tags/v1.0.0").mock(return_value=httpx.Response(204))
        result = await tools["gitea_release_delete"]("testowner/repo", "v1.0.0")
    assert result["deleted"] is True


@pytest.mark.asyncio
async def test_gitea_release_delete_not_found(tools):
    with respx.mock:
        respx.delete(f"{_REL_BASE}/tags/nope").mock(
            return_value=httpx.Response(404, text="not found")
        )
        result = await tools["gitea_release_delete"]("testowner/repo", "nope")
    assert "error" in result
    assert "deleted" not in result


@pytest.mark.asyncio
async def test_gitea_release_update_bad_repo(tools):
    result = await tools["gitea_release_update"]("bad-no-slash", "v1")
    assert "error" in result
    assert "owner/repo" in result["error"]

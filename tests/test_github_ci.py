"""GitHub CI reads (githost-scoped-2026-11 p1, Phase 3): job list, job log, commit checks."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from githost_mcp._providers import github_client
from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config

TOKEN = "ghp_fakefakefake123456789012345678901"
LOGS_URL = "https://api.github.com/repos/owner/repo/actions/jobs/77/logs"
# The live redirect target shape, 2026-11-07 (query elided there; a fake signature here).
BLOB = (
    "https://productionresultssa5.blob.core.windows.net/actions-results/abc/"
    "workflow-job-run-def/logs/job/job-logs.txt?sv=2024&sig=SIGNEDsecretVALUE"
)


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    reset_config()
    init_logging()
    github_client._client = None


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


def _passthrough(fn, *a, **kw):
    return fn(*a, **kw)


def _patch_gh(mock_gh):
    return (
        patch("githost_mcp.tools.github.get_github", return_value=mock_gh),
        patch("githost_mcp.tools.github.github_call", side_effect=_passthrough),
    )


def _redirect(location=BLOB):
    return respx.get(LOGS_URL).mock(
        return_value=httpx.Response(302, headers={"location": location})
    )


# --- get_job_log --------------------------------------------------------------------------


def test_job_log_follows_allowlisted_redirect_without_the_token(tools):
    text = "".join(f"2026-11-07T00:00:{i:02d}Z line {i}\n" for i in range(60)) + "##[error]boom\n"
    with respx.mock:
        api = _redirect()
        blob = respx.get(BLOB).mock(return_value=httpx.Response(200, text=text))
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert api.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    blob_req = blob.calls.last.request
    assert "authorization" not in blob_req.headers
    assert TOKEN not in str(blob_req.headers)
    assert r["mode"] == "tail"
    assert r["lines"][-1] == "##[error]boom"
    assert r["total_lines"] == 61


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/logs.txt",
        "https://productionresultssa5.blob.core.windows.net.evil.com/x",
        "https://someoneelse.blob.core.windows.net/x",
        "http://productionresultssa5.blob.core.windows.net/x",
        "https://u:p@productionresultssa5.blob.core.windows.net/x",
        "https://productionresultssa5.blob.core.windows.net:8443/x",
        "/relative/path",
        "",
    ],
)
def test_job_log_refuses_redirect_outside_allowlist(tools, location):
    with respx.mock(assert_all_called=False) as mock:
        api = mock.get(LOGS_URL).mock(
            return_value=httpx.Response(302, headers={"location": location})
        )
        anything_else = mock.route().mock(return_value=httpx.Response(200, text="LEAKED\n"))
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert api.call_count == 1
    assert anything_else.call_count == 0
    assert "error" in r
    assert "redirect" in r["error"]


def test_refusal_names_host_not_signed_query(tools):
    with respx.mock:
        _redirect("https://evil.example.com/x?sig=SIGNEDsecretVALUE")
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert "evil.example.com" in r["error"]
    assert "SIGNEDsecretVALUE" not in r["error"]


def test_githubusercontent_host_is_allowed():
    assert github_client.log_host_allowed(
        httpx.URL("https://pipelines.actions.githubusercontent.com/abc/logs")
    )


@pytest.mark.parametrize("code", [404, 410])
def test_job_log_expired_or_missing(tools, code):
    with respx.mock:
        respx.get(LOGS_URL).mock(return_value=httpx.Response(code))
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert "not available" in r["error"]


def test_job_log_download_is_bounded(tools, monkeypatch):
    monkeypatch.setattr(github_client, "LOG_DOWNLOAD_MAX", 100)
    with respx.mock:
        _redirect()
        respx.get(BLOB).mock(return_value=httpx.Response(200, text="x\n" * 500))
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert r["download_cut"] is True
    assert r["total_lines"] == 50


def test_job_log_is_scrubbed(tools):
    with respx.mock:
        _redirect()
        respx.get(BLOB).mock(
            return_value=httpx.Response(200, text=f"push https://x:{TOKEN}@github.com/o/r\n")
        )
        r = tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    assert TOKEN not in r["lines"][0]


def test_job_log_requires_job_id(tools):
    r = tools["github_actions"]("owner/repo", "get_job_log")
    assert "job_id is required" in r["error"]


@pytest.mark.parametrize("kwargs", [{"mode": "x"}, {"grep": "a\tb"}, {"job_id": 0}])
def test_job_log_bad_args_rejected_before_request(tools, kwargs):
    args = {"job_id": 77} | kwargs
    with respx.mock:
        r = tools["github_actions"]("owner/repo", "get_job_log", **args)
    assert "error" in r


def test_job_log_audit_has_no_content_or_grep_text(tools, tmp_path):
    with respx.mock:
        _redirect()
        respx.get(BLOB).mock(return_value=httpx.Response(200, text="PRIV-LINE\nneedle-z\n"))
        tools["github_actions"]("owner/repo", "get_job_log", job_id=77, grep="needle-z")
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "PRIV-LINE" not in raw and "needle-z" not in raw and "SIGNED" not in raw
    assert json.loads(raw.splitlines()[-1])["params"]["grep"] is True


# --- list_jobs ----------------------------------------------------------------------------


def _step(n, name, conclusion):
    s = MagicMock()
    s.number, s.name, s.status, s.conclusion = n, name, "completed", conclusion
    return s


def test_list_jobs_returns_steps(tools):
    job = MagicMock()
    job.id, job.name, job.status, job.conclusion = (
        125315120183,
        "test (3.12)",
        "completed",
        "failure",
    )
    job.started_at = datetime(2026, 11, 2, 13, 52, tzinfo=UTC)
    job.completed_at = None
    job.html_url = "https://github.com/owner/repo/actions/runs/5/job/125315120183"
    job.steps = [_step(1, "Set up job", "success"), _step(4, "pytest", "failure")]
    run = MagicMock()
    run.jobs.return_value = [job]
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow_run.return_value = run
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_actions"]("owner/repo", "list_jobs", run_id=5)
    j = r["jobs"][0]
    assert j["id"] == 125315120183 and j["conclusion"] == "failure"
    assert j["started_at"] == "2026-11-02T13:52:00+00:00" and j["completed_at"] is None
    assert j["steps"][1] == {
        "number": 4,
        "name": "pytest",
        "status": "completed",
        "conclusion": "failure",
    }


def test_list_jobs_requires_run_id(tools):
    gh = MagicMock()
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_actions"]("owner/repo", "list_jobs")
    assert "run_id is required" in r["error"]


# --- github_commit_checks -----------------------------------------------------------------


def _check_run(name, conclusion, annotations=()):
    cr = MagicMock()
    cr.id, cr.name, cr.status, cr.conclusion = hash(name) % 1000, name, "completed", conclusion
    cr.started_at = cr.completed_at = None
    cr.html_url = f"https://github.com/owner/repo/runs/{name}"
    cr.get_annotations.return_value = list(annotations)
    return cr


def _annotation(i):
    a = MagicMock()
    a.path, a.start_line, a.annotation_level = "tests/test_x.py", i, "failure"
    a.message = f"assert {i} == 0"
    return a


def _commit_gh(runs, statuses=()):
    combined = MagicMock()
    combined.state = "success"
    combined.statuses = list(statuses)
    commit = MagicMock()
    commit.sha = "a" * 40
    commit.get_combined_status.return_value = combined
    commit.get_check_runs.return_value = list(runs)
    gh = MagicMock()
    gh.get_repo.return_value.get_commit.return_value = commit
    return gh, commit


def test_commit_checks_status_and_runs(tools):
    st = MagicMock()
    st.context, st.state, st.target_url = "ci/woodpecker", "failure", "https://ci/x"
    st.description = "https://u:pw5555@h/ " * 40
    st.updated_at = datetime(2026, 11, 7, tzinfo=UTC)
    gh, commit = _commit_gh([_check_run("test", "failure")], [st])
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_commit_checks"]("owner/repo", "main")
    gh.get_repo.return_value.get_commit.assert_called_once_with("main")
    assert r["sha"] == "a" * 40
    assert r["statuses"][0]["state"] == "failure"
    assert "pw5555" not in r["statuses"][0]["description"]
    assert len(r["statuses"][0]["description"]) <= 301
    assert r["check_runs"][0]["conclusion"] == "failure"
    assert "annotations" not in r
    commit.get_check_runs.return_value[0].get_annotations.assert_not_called()


def test_commit_checks_annotations_only_from_failed_runs_and_capped(tools):
    ok = _check_run("lint", "success", [_annotation(999)])
    bad1 = _check_run("test-a", "failure", [_annotation(i) for i in range(40)])
    bad2 = _check_run("test-b", "timed_out", [_annotation(i) for i in range(40)])
    gh, _ = _commit_gh([ok, bad1, bad2])
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_commit_checks"]("owner/repo", "a" * 40, annotations=True)
    assert len(r["annotations"]) == 50
    assert r["annotations_truncated"] is True
    assert {a["check_run"] for a in r["annotations"]} == {"test-a", "test-b"}
    ok.get_annotations.assert_not_called()
    assert r["annotations"][0] == {
        "check_run": "test-a",
        "path": "tests/test_x.py",
        "start_line": 0,
        "level": "failure",
        "message": "assert 0 == 0",
    }


def test_commit_checks_caps_check_runs(tools):
    gh, _ = _commit_gh([_check_run(f"r{i}", "success") for i in range(120)])
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_commit_checks"]("owner/repo", "main")
    assert len(r["check_runs"]) == 100 and r["check_runs_truncated"] is True


@pytest.mark.parametrize("ref", ["", "-x", "a..b", "../x", "x y", "x" * 300])
def test_commit_checks_rejects_bad_ref(tools, ref):
    gh = MagicMock()
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_commit_checks"]("owner/repo", ref)
    assert "error" in r
    gh.get_repo.assert_not_called()


def test_commit_checks_rejects_bad_repo(tools):
    assert "error" in tools["github_commit_checks"]("bad", "main")


def test_signed_blob_query_never_reaches_the_httpx_log(tools, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="httpx"), respx.mock:
        _redirect()
        respx.get(BLOB).mock(return_value=httpx.Response(200, text="ok\n"))
        tools["github_actions"]("owner/repo", "get_job_log", job_id=77)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "productionresultssa5.blob.core.windows.net" in text  # the line is still there
    assert "SIGNEDsecretVALUE" not in text
    assert "api.github.com/repos/owner/repo/actions/jobs/77/logs" in text


@pytest.mark.parametrize("repo", ["../..", "owner/..", "owner/repo\n"])
def test_github_repo_dot_segments_and_newline_refused(tools, repo):
    gh = MagicMock()
    p1, p2 = _patch_gh(gh)
    with p1, p2:
        r = tools["github_commit_checks"](repo, "main")
    assert "error" in r
    gh.get_repo.assert_not_called()


def test_github_fork_org_dot_segment_and_newline_refused(tools):
    for org in ("..", ".", "org\n"):
        assert "error" in tools["github_fork"]("owner", "repo", org=org)


def test_log_host_allowlist_rejects_trailing_newline_host():
    from githost_mcp._providers.github_client import _is_log_host

    assert _is_log_host("productionresultssa5.blob.core.windows.net")
    assert not _is_log_host("productionresultssa5.blob.core.windows.net\n")

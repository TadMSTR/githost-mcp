"""The githost.<tool> span covers the work, with git and HTTP children (vikunja#1426).

Before 0.19.0 the span was opened inside AuditCtx.finish(), after the work: 23 µs long,
starting at the end of the call, with no children (trace
9113fefe1e69c4185a50818bcdaddc17). These tests drive real tools against an in-memory
span exporter and check where the span starts and ends and what sits under it.
"""

import time

import git
import httpx
import pytest
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import githost_mcp.observability as obs
from githost_mcp.audit import AuditCtx, init_logging
from githost_mcp.config import reset_config

# Built at runtime so the source holds no detector-shaped literal.
TOKEN = "gitea_tok_" + "0123456789abcdef" * 2


@pytest.fixture()
def exporter(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_ID", "test")
    monkeypatch.setenv("GITEA_URL", "https://gitea.example")
    monkeypatch.setenv("GITEA_TOKEN", TOKEN)
    reset_config()
    init_logging()
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(obs, "_tracer", provider.get_tracer("test"))
    yield exp
    reset_config()


def _register(module):
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    module.register(MockMCP())
    return registered


@pytest.fixture()
def pushed_repo(tmp_path):
    repo = git.Repo.init(tmp_path / "repo")
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "t@test.com").release()
    (tmp_path / "repo" / "f.txt").write_text("1")
    repo.index.add(["f.txt"])
    repo.index.commit("one")
    bare = git.Repo.init(tmp_path / "remote.git", bare=True)
    repo.create_remote("origin", bare.git_dir)
    branch = repo.active_branch.name
    repo.remotes.origin.push(branch)
    (tmp_path / "repo" / "f.txt").write_text("2")
    repo.index.add(["f.txt"])
    repo.index.commit("two")
    return repo, bare, branch


def _by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_git_push_span_covers_the_push_and_has_a_git_child(exporter, pushed_repo):
    from githost_mcp.tools import git_local

    repo, _bare, _branch = pushed_repo
    result = _register(git_local)["git_push"](repo.working_dir)
    assert "error" not in result, result

    (parent,) = _by_name(exporter, "githost.git_push")
    children = [
        s for s in _by_name(exporter, "git.push") if s.parent.span_id == parent.context.span_id
    ]
    assert children, "githost.git_push has no git.push child"
    child = children[0]
    assert parent.start_time <= child.start_time and child.end_time <= parent.end_time
    own_ms = (parent.end_time - parent.start_time) / 1e6
    assert abs(own_ms - parent.attributes["githost.duration_ms"]) <= 50, (
        f"span lasted {own_ms:.1f} ms but recorded duration_ms "
        f"{parent.attributes['githost.duration_ms']}"
    )
    assert child.attributes["githost.remote"] == "origin"


def test_span_starts_when_the_ctx_is_created_not_at_finish(exporter):
    ac = AuditCtx("git_status", "local", "/repo", {})
    time.sleep(0.06)
    ac.finish("ok")
    (span,) = _by_name(exporter, "githost.git_status")
    assert (span.end_time - span.start_time) / 1e6 >= 50


@respx.mock
async def test_http_tool_gets_an_http_child_with_no_token_or_query(exporter):
    from githost_mcp.tools import gitea

    respx.get(url__regex=r"https://gitea\.example/api/v1/repos/o/r/pulls.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await _register(gitea)["gitea_pr_list"]("o/r", state="open", limit=5)
    assert "error" not in result, result

    (parent,) = _by_name(exporter, "githost.gitea_pr_list")
    (child,) = _by_name(exporter, "http.gitea")
    assert child.parent.span_id == parent.context.span_id
    assert child.attributes["http.request.method"] == "GET"
    assert child.attributes["http.response.status_code"] == 200
    assert child.attributes["url.template"] == "/api/v1/repos/{}/{}/pulls"
    assert child.attributes["server.address"] == "gitea.example"
    for span in (parent, child):
        flat = repr(dict(span.attributes))
        assert TOKEN not in flat
        assert "?" not in flat and "state=" not in flat and "limit=" not in flat


def test_exception_path_ends_the_span_with_error_status(exporter):
    with pytest.raises(RuntimeError), AuditCtx("git_push", "local", "/repo", {}):
        raise RuntimeError("boom")
    (span,) = _by_name(exporter, "githost.git_push")
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["githost.result"] == "error:RuntimeError"


def test_error_result_marks_the_span_error_and_denied_does_not(exporter):
    AuditCtx("git_push", "local", "/repo", {}).finish("error:PushRejected", "rejected")
    AuditCtx("git_add", "local", "/repo", {}).finish("denied:write_glob", "nope")
    (err,) = _by_name(exporter, "githost.git_push")
    (denied,) = _by_name(exporter, "githost.git_add")
    assert err.status.status_code == StatusCode.ERROR
    assert denied.status.status_code != StatusCode.ERROR
    assert denied.attributes["githost.reason_class"]


def test_a_ctx_left_unfinished_by_a_with_block_still_ends_its_span(exporter):
    with AuditCtx("git_status", "local", "/repo", {}):
        pass  # a code path that returned without finish()
    (span,) = _by_name(exporter, "githost.git_status")
    assert span.attributes["githost.result"] == "error:unfinished"
    assert span.end_time is not None


def test_child_span_records_the_exception_type_not_its_message(exporter):
    ac = AuditCtx("git_push", "local", "/repo", {})
    with pytest.raises(ValueError), ac.child("git.push", **{"githost.remote": "origin"}):
        raise ValueError("https://user:secret@host/x rejected")
    ac.finish("error:ValueError")
    (child,) = _by_name(exporter, "git.push")
    assert child.status.status_code == StatusCode.ERROR
    assert child.attributes["error.type"] == "ValueError"
    assert "secret" not in repr(dict(child.attributes))
    assert not child.events, "the exception must not be recorded as a span event"


def test_trace_id_in_the_audit_entry_is_the_tool_spans(exporter, tmp_path):
    import json

    AuditCtx("git_status", "local", "/repo", {}).finish("ok")
    (span,) = _by_name(exporter, "githost.git_status")
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert entry["trace_id"] == format(span.context.trace_id, "032x")


def test_no_otel_path_still_works_and_child_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    reset_config()
    init_logging()
    monkeypatch.setattr(obs, "_tracer", None)
    ac = AuditCtx("git_status", "local", "/repo", {})
    with ac.child("git.push") as span:
        assert span is None
    ac.finish("ok")
    assert (tmp_path / "audit.jsonl").read_text().strip()


def test_an_unwritable_audit_dir_does_not_leak_the_span(exporter, tmp_path, monkeypatch):
    """CR-04: os.makedirs sat outside the write's try. Its OSError escaped finish() after
    `finished` was set and before the span ended, so the span never closed."""
    import githost_mcp.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_audit_log_path", str(tmp_path / "nodir" / "audit.jsonl"))

    def refuse(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(audit_mod.os, "makedirs", refuse)
    ac = AuditCtx("git_status", "local", "/repo", {})
    ac.finish("ok")  # must not raise
    (span,) = _by_name(exporter, "githost.git_status")
    assert span.end_time is not None
    assert audit_mod._current_ctx.get() is not ac


def test_client_private_transport_attrs_exist():
    """CR-07: httptrace wraps httpx's private _transport and _mounts."""
    import httpx

    for client in (httpx.Client(), httpx.AsyncClient()):
        assert hasattr(client, "_transport") and isinstance(client._mounts, dict)


@pytest.mark.parametrize("factory", ["sync_client", "async_client"])
def test_traced_clients_keep_env_proxies(monkeypatch, factory):
    """CR-07: passing transport= made httpx drop HTTP(S)_PROXY / NO_PROXY."""
    from githost_mcp import httptrace

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "gitea.internal")
    client = getattr(httptrace, factory)("gitea", timeout=5)
    wrapper = (httptrace.TracingTransport, httptrace.TracingAsyncTransport)
    proxied = [t for t in client._mounts.values() if t is not None]
    assert proxied, "env proxy mount missing"
    assert all(isinstance(t, wrapper) for t in proxied)
    assert isinstance(client._transport, wrapper)
    assert None in client._mounts.values(), "NO_PROXY entry missing"


def test_traced_client_without_proxy_env_still_traces(monkeypatch):
    from githost_mcp import httptrace

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name.lower(), raising=False)
        monkeypatch.delenv(name, raising=False)
    client = httptrace.sync_client("gitea")
    assert isinstance(client._transport, httptrace.TracingTransport)


def test_an_httpx_without_the_private_attrs_gets_an_untraced_client(monkeypatch):
    """Self-check OV-12 (round 2): if httpx moves _transport/_mounts, the client still works,
    untraced, with one warning, instead of every HTTP tool failing at construction."""
    from githost_mcp import httptrace

    class Moved:
        pass

    warnings = []
    monkeypatch.setattr(httptrace, "_private_attrs_missing_logged", False)
    monkeypatch.setattr(httptrace.log, "warning", lambda event, **kw: warnings.append(event))
    for _ in range(2):
        client = Moved()
        httptrace._wrap_transports(client, lambda inner: inner)
        assert not hasattr(client, "_transport")
    assert warnings == ["httptrace_untraced_httpx_internals_moved"]

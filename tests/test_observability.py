"""Tests for observability init and emit paths.

All telemetry is opt-in; these tests exercise the disabled no-op paths, the metric
emitters with mocked instruments, and the child-span attribute bounding. The
OTEL-endpoint init path is intentionally not driven here — it sets global providers and
background threads that would leak across tests. Span behaviour is driven against an
in-memory exporter in test_spans.py.

The Loki push, Prometheus endpoint and NATS publisher were removed in 0.19.0
(githost-scoped-2026-11 p2); test_removed_backends_are_gone pins that.
"""

from unittest.mock import MagicMock

import pytest

import githost_mcp.observability as obs
from githost_mcp.config import reset_config

_GLOBALS = [
    "_tracer",
    "_meter",
    "_tool_calls_counter",
    "_tool_denied_counter",
    "_tool_duration_histogram",
    "_release_targets_counter",
]


@pytest.fixture(autouse=True)
def _isolate_globals(monkeypatch):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AGENT_ID", "tester")
    reset_config()
    saved = {name: getattr(obs, name) for name in _GLOBALS}
    yield
    for name, value in saved.items():
        setattr(obs, name, value)
    reset_config()


def test_init_sync_all_disabled_is_noop():
    obs.init_sync()
    assert obs._tracer is None
    assert obs._tool_calls_counter is None


def test_init_otel_no_endpoint_returns_early():
    obs._init_otel()
    assert obs._tracer is None


def test_removed_backends_are_gone():
    """No Prometheus listener, Loki push or NATS publisher remains to be started."""
    for name in (
        "_init_prometheus",
        "_init_loki",
        "_push_loki",
        "_init_nats",
        "_publish_nats",
        "emit_tool_event",
        "init_async",
        "loki_or_nats_configured",
        "METRICS_BIND_ADDR",
    ):
        assert not hasattr(obs, name), f"{name} should have been removed"


def test_emit_tool_event_all_disabled():
    obs.emit_tool_event_sync("git_push", "local", "repo", "ok", 10)  # no instruments -> no-op


def test_emit_tool_event_drives_the_otel_instruments():
    obs._tool_calls_counter = MagicMock()
    obs._tool_duration_histogram = MagicMock()
    obs._tool_denied_counter = MagicMock()
    obs.emit_tool_event_sync("git_push", "local", "repo", "ok", 25)
    obs._tool_calls_counter.add.assert_called_once()
    obs._tool_duration_histogram.record.assert_called_once_with(
        25, {"tool": "git_push", "provider": "local"}
    )
    obs._tool_denied_counter.add.assert_not_called()


def test_emit_tool_event_swallows_instrument_errors():
    obs._tool_calls_counter = MagicMock()
    obs._tool_calls_counter.add.side_effect = RuntimeError("metric down")
    obs._tool_denied_counter = MagicMock()
    obs._tool_denied_counter.add.side_effect = RuntimeError("metric down")
    obs.emit_tool_event_sync("git_push", "local", "repo", "error:X", 25, "other")  # no raise


def test_emit_release_target_disabled_noop():
    obs.emit_release_target("pypi", "ok")  # no counters -> no-op


def test_emit_release_target_drives_counter():
    obs._release_targets_counter = MagicMock()
    obs.emit_release_target("pypi", "ok")
    obs._release_targets_counter.add.assert_called_once_with(1, {"target": "pypi", "result": "ok"})


def test_emit_release_target_swallows_errors():
    obs._release_targets_counter = MagicMock()
    obs._release_targets_counter.add.side_effect = RuntimeError("boom")
    obs.emit_release_target("pypi", "error")  # must not raise


# --- child-span attribute bounding ------------------------------------------


def test_child_attrs_keep_only_known_keys():
    attrs = obs.bounded_child_attrs(
        {
            "http.request.method": "POST",
            "url.template": "/api/v1/repos/{}/{}/pulls",
            "url.full": "https://gitea/api/v1/repos/o/r/pulls?token=abc",
            "http.request.body": "{}",
        }
    )
    assert attrs == {"http.request.method": "POST", "url.template": "/api/v1/repos/{}/{}/pulls"}


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pass@host/x",  # userinfo and scheme punctuation
        "/repos/x?token=abc",  # query string
        "a" * 129,  # over the length bound
        "has space",
        "line\nbreak",
    ],
)
def test_child_attr_values_that_are_not_plain_are_dropped(value):
    assert obs.bounded_child_attrs({"server.address": value}) == {}


def test_child_attr_ints_and_bools_are_kept():
    assert obs.bounded_child_attrs({"http.response.status_code": 404}) == {
        "http.response.status_code": 404
    }


def test_span_helpers_are_noops_without_a_tracer():
    assert obs._tracer is None
    assert obs.start_tool_span("git_push") is None
    obs.end_tool_span(None, {"a": 1}, error=True)  # no raise
    with obs.child_span(None, "git.push", {}) as span:
        assert span is None
    obs.set_child_attrs(None, {"http.response.status_code": 200})

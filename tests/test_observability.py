"""Tests for observability emit/init paths.

All telemetry is opt-in; these tests exercise the disabled no-op paths, the Loki
config/push flow (respx), the missing-optional-dependency fallbacks, and the emit
functions with mocked backends. The OTEL-endpoint init path is intentionally not
driven here — it sets global providers and background threads that would leak
across tests; it is covered by the `[otel]` extra's own suite.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

import githost_mcp.observability as obs
from githost_mcp.config import reset_config

_GLOBALS = [
    "_tracer",
    "_meter",
    "_tool_calls_counter",
    "_tool_duration_histogram",
    "_release_targets_counter",
    "_prom_tool_calls",
    "_prom_tool_duration",
    "_prom_release_targets",
    "_loki_url",
    "_loki_static_labels",
    "_nats_client",
    "_nats_prefix",
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
    assert obs._prom_tool_calls is None
    assert obs._loki_url == ""


def test_init_loki_parses_url_and_labels(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://loki:3100/")
    monkeypatch.setenv("LOKI_LABELS", "env=prod, team=forge, malformed")
    reset_config()
    obs._init_loki()
    assert obs._loki_url == "http://loki:3100"
    assert obs._loki_static_labels["app"] == "githost-mcp"
    assert obs._loki_static_labels["env"] == "prod"
    assert obs._loki_static_labels["team"] == "forge"
    assert "malformed" not in obs._loki_static_labels


def test_init_prometheus_missing_dep_is_swallowed(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9185")
    reset_config()
    obs._init_prometheus()  # prometheus_client absent -> except path, no port bound
    assert obs._prom_tool_calls is None


@pytest.mark.asyncio
async def test_init_nats_missing_dep_is_swallowed(monkeypatch):
    monkeypatch.setenv("NATS_URL", "nats://localhost:4222")
    reset_config()
    await obs._init_nats()  # nats absent -> except path
    assert obs._nats_client is None


def test_init_otel_no_endpoint_returns_early():
    obs._init_otel()
    assert obs._tracer is None


@pytest.mark.asyncio
async def test_push_loki_disabled_noop():
    obs._loki_url = ""
    await obs._push_loki({"tool": "x"}, "msg")  # returns without HTTP


@pytest.mark.asyncio
async def test_push_loki_posts_to_endpoint():
    obs._loki_url = "http://loki:3100"
    obs._loki_static_labels = {"app": "githost-mcp"}
    with respx.mock:
        route = respx.post("http://loki:3100/loki/api/v1/push").mock(
            return_value=httpx.Response(204)
        )
        await obs._push_loki({"tool": "git_push"}, "hello")
    assert route.called


@pytest.mark.asyncio
async def test_push_loki_swallows_transport_error():
    obs._loki_url = "http://loki:3100"
    with respx.mock:
        respx.post("http://loki:3100/loki/api/v1/push").mock(side_effect=httpx.ConnectError("down"))
        await obs._push_loki({}, "hello")  # must not raise


@pytest.mark.asyncio
async def test_publish_nats_disabled_noop():
    obs._nats_client = None
    await obs._publish_nats("tool.git_push", {"a": 1})  # returns without publish


@pytest.mark.asyncio
async def test_publish_nats_publishes_with_prefix():
    client = AsyncMock()
    obs._nats_client = client
    obs._nats_prefix = "githost"
    await obs._publish_nats("tool.git_push", {"a": 1})
    client.publish.assert_awaited_once()
    subject = client.publish.await_args.args[0]
    assert subject == "githost.tool.git_push"


@pytest.mark.asyncio
async def test_emit_tool_event_all_disabled():
    await obs.emit_tool_event("git_push", "local", "repo", "ok", 10)  # no backends -> no-op


@pytest.mark.asyncio
async def test_emit_tool_event_drives_all_backends():
    obs._tracer = MagicMock()
    obs._tool_calls_counter = MagicMock()
    obs._tool_duration_histogram = MagicMock()
    obs._prom_tool_calls = MagicMock()
    obs._prom_tool_duration = MagicMock()
    obs._nats_client = AsyncMock()
    obs._loki_url = "http://loki:3100"
    obs._loki_static_labels = {"app": "githost-mcp"}
    with respx.mock:
        respx.post("http://loki:3100/loki/api/v1/push").mock(return_value=httpx.Response(204))
        await obs.emit_tool_event("git_push", "local", "repo", "ok", 25)
    obs._tool_calls_counter.add.assert_called_once()
    obs._prom_tool_calls.labels.assert_called_once()
    obs._nats_client.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_emit_tool_event_swallows_backend_errors():
    obs._tracer = MagicMock()
    obs._tracer.start_as_current_span.side_effect = RuntimeError("otel down")
    obs._tool_calls_counter = MagicMock()
    obs._tool_calls_counter.add.side_effect = RuntimeError("metric down")
    obs._prom_tool_calls = MagicMock()
    obs._prom_tool_calls.labels.side_effect = RuntimeError("prom down")
    await obs.emit_tool_event("git_push", "local", "repo", "ok", 25)  # must not raise


def test_emit_release_target_disabled_noop():
    obs.emit_release_target("pypi", "ok")  # no counters -> no-op


def test_emit_release_target_drives_counters():
    obs._release_targets_counter = MagicMock()
    obs._prom_release_targets = MagicMock()
    obs.emit_release_target("pypi", "ok")
    obs._release_targets_counter.add.assert_called_once()
    obs._prom_release_targets.labels.assert_called_once_with(target="pypi", result="ok")


def test_emit_release_target_swallows_errors():
    obs._release_targets_counter = MagicMock()
    obs._release_targets_counter.add.side_effect = RuntimeError("boom")
    obs._prom_release_targets = MagicMock()
    obs._prom_release_targets.labels.side_effect = RuntimeError("boom")
    obs.emit_release_target("pypi", "error")  # must not raise

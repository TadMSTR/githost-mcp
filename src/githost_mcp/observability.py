"""
OpenTelemetry traces and metrics, opt-in via OTEL_EXPORTER_OTLP_ENDPOINT.

Nothing activates unless the endpoint is set, and an import error for the optional
`otel` extra is swallowed so the server starts without it.

The Loki push, the Prometheus endpoint and the NATS publisher were removed in 0.19.0.
None had a consumer: the async emitter that fed Loki and NATS had no caller, and no
scrape job read ports 9620-9628 (githost-scoped-2026-11 p2, #1426 (id 1509), #530
(id 613)). OTel is the one live path; the audit JSONL is the durable record.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

import structlog

from .config import get_config

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# OTEL (opentelemetry-sdk + exporter)
# ---------------------------------------------------------------------------

_tracer: Any = None
_meter: Any = None
_tool_calls_counter: Any = None
_tool_denied_counter: Any = None
_tool_duration_histogram: Any = None
_release_targets_counter: Any = None


def _init_otel() -> None:
    global _tracer, _meter, _tool_calls_counter, _tool_denied_counter
    global _tool_duration_histogram, _release_targets_counter
    config = get_config()
    if not config.otel_endpoint:
        return
    try:
        from opentelemetry import metrics, trace  # type: ignore
        from opentelemetry.sdk.metrics import MeterProvider  # type: ignore
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore

        resource = Resource.create({"service.name": config.otel_service_name})

        if config.otel_protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,  # type: ignore
            )
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,  # type: ignore
            )

            span_exporter = OTLPSpanExporter(endpoint=config.otel_endpoint)
            metric_exporter = OTLPMetricExporter(endpoint=config.otel_endpoint)
        else:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,  # type: ignore
            )
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,  # type: ignore
            )

            span_exporter = OTLPSpanExporter(endpoint=config.otel_endpoint)
            metric_exporter = OTLPMetricExporter(endpoint=config.otel_endpoint)

        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tp)
        _tracer = trace.get_tracer(config.otel_service_name)

        mp = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
        )
        metrics.set_meter_provider(mp)
        bind_meter(metrics.get_meter(config.otel_service_name))
        log.info("otel_initialized", endpoint=config.otel_endpoint)
    except Exception as exc:
        log.warning("otel_init_failed", error=str(exc))


def bind_meter(meter: Any) -> None:
    """Create the githost instruments on `meter`. Split out so tests can bind a meter
    backed by an in-memory reader and assert on real counters."""
    global _meter, _tool_calls_counter, _tool_denied_counter
    global _tool_duration_histogram, _release_targets_counter
    _meter = meter
    _tool_calls_counter = meter.create_counter(
        "githost.tool.calls",
        description="Number of githost-mcp tool calls",
    )
    # reason_class is bounded by construction: classify_reason() can only return a member
    # of errors.REASON_CLASSES. The unbounded detail (exception messages, which carry repo
    # paths and branch names) stays in the audit record's `reason` field and is never an
    # attribute here.
    _tool_denied_counter = meter.create_counter(
        "githost.tool.denied",
        description="githost-mcp tool calls that did not succeed, by reason class",
    )
    _tool_duration_histogram = meter.create_histogram(
        "githost.tool.duration",
        unit="ms",
        description="Duration of githost-mcp tool calls",
    )
    _release_targets_counter = meter.create_counter(
        "githost.release.targets",
        description="Number of release target publishes",
    )


# ---------------------------------------------------------------------------
# Spans
#
# The githost.<tool> span used to be opened inside AuditCtx.finish(), after the work was
# done: 23 µs long, starting at the very end of the call, with no children (trace
# 9113fefe1e69c4185a50818bcdaddc17). It is now opened when the AuditCtx is created and
# ended by finish(), and git and HTTP calls inside it get child spans.
#
# Trace propagation itself already worked and is not touched: FastMCP carries
# `traceparent` in the request `_meta` and starts its server span from it, and the tool
# span is created as a child of whatever span is current, which is that server span.
# ---------------------------------------------------------------------------

#: Child-span attribute keys, and nothing else. Values are bounded below. A URL, a query
#: string, a request or response body, a commit message or a file name never belongs in a
#: span attribute: the span leaves this host for SigNoz.
CHILD_ATTR_KEYS = frozenset(
    {
        "githost.op",
        "githost.remote",
        "githost.api.call",
        "http.request.method",
        "http.response.status_code",
        "server.address",
        "url.template",
    }
)
_ATTR_STR_MAX = 128
_ATTR_TEXT_RE = re.compile(r"\A[A-Za-z0-9_.:/{}\-]*\Z")


def _bounded_attr(value: Any) -> Any:
    """Keep ints and bools; keep a string only if it is short and plain, else drop it."""
    if isinstance(value, bool | int):
        return value
    if isinstance(value, str) and len(value) <= _ATTR_STR_MAX and _ATTR_TEXT_RE.match(value):
        return value
    return None


def bounded_child_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """The subset of `attrs` that may go on a child span: known keys, plain values."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key not in CHILD_ATTR_KEYS:
            continue
        shown = _bounded_attr(value)
        if shown is not None:
            out[key] = shown
    return out


def start_tool_span(tool: str) -> Any:
    """Open `githost.<tool>` as a child of the current span; None when OTel is off."""
    if _tracer is None:
        return None
    try:
        return _tracer.start_span(f"githost.{tool}")
    except Exception as exc:
        log.warning("otel_span_failed", error=str(exc))
        return None


def end_tool_span(span: Any, attrs: dict[str, Any], error: bool) -> None:
    """Set the final attributes, mark an error, and end the span. Never raises."""
    if span is None:
        return
    try:
        span.set_attributes({k: v for k, v in attrs.items() if v is not None})
        if error:
            from opentelemetry.trace import Status, StatusCode  # type: ignore

            span.set_status(Status(StatusCode.ERROR))
    except Exception as exc:
        log.warning("otel_span_failed", error=str(exc))
    finally:
        try:
            span.end()
        except Exception as exc:  # pragma: no cover - defence in depth
            log.warning("otel_span_end_failed", error=str(exc))


@contextmanager
def child_span(parent: Any, name: str, attrs: dict[str, Any]) -> Iterator[Any]:
    """A child of `parent` around one git or HTTP operation. A no-op without a parent.

    The exception, if any, propagates unchanged. Only its type is recorded: the message
    can carry a remote URL or a server's response text, and span data leaves the host.
    """
    if parent is None or _tracer is None:
        yield None
        return
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.trace import Status, StatusCode  # type: ignore

        span = _tracer.start_span(
            name,
            context=trace.set_span_in_context(parent),
            attributes=bounded_child_attrs(attrs),
        )
    except Exception as exc:
        log.warning("otel_span_failed", error=str(exc))
        yield None
        return
    try:
        yield span
    except BaseException as exc:
        try:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
        except Exception:  # pragma: no cover - defence in depth
            pass
        raise
    finally:
        with suppress(Exception):
            span.end()


def set_child_attrs(span: Any, attrs: dict[str, Any]) -> None:
    """Add bounded attributes to a child span after the fact (e.g. a status code)."""
    if span is None:
        return
    with suppress(Exception):
        span.set_attributes(bounded_child_attrs(attrs))


def trace_id_hex(span: Any) -> str | None:
    """The 32-hex trace id of `span`, else of the current span, else None.

    Without githost's own tracer the current span is FastMCP's server span, which still
    carries the trace scoped-mcp started, so an audit line joins to SigNoz either way.
    """
    try:
        from opentelemetry import trace  # type: ignore

        ctx = (span if span is not None else trace.get_current_span()).get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Metrics — called by AuditCtx.finish() after each tool invocation
# ---------------------------------------------------------------------------


def emit_tool_event_sync(
    tool: str,
    provider: str,
    repo_basename: str,
    result: str,
    duration_ms: int,
    reason_class: str | None = None,
) -> None:
    """OTel metrics for one finished tool call. The span is AuditCtx's own now.

    Sync because AuditCtx.finish() is: most tools are sync functions that FastMCP runs on
    a worker thread with no event loop. The OTel metric API is thread-safe.

    `reason_class` is set only for a call that did not succeed, and must be a member of
    errors.REASON_CLASSES — it is a metric attribute.

    Each instrument is guarded independently: telemetry must never be able to fail a
    tool call that otherwise worked.
    """
    agent_id = get_config().agent_id

    if _tool_calls_counter is not None:
        try:
            _tool_calls_counter.add(
                1, {"tool": tool, "provider": provider, "agent_id": agent_id, "result": result}
            )
            _tool_duration_histogram.record(duration_ms, {"tool": tool, "provider": provider})
        except Exception as exc:
            log.warning("otel_metric_failed", error=str(exc))

    if reason_class is not None and _tool_denied_counter is not None:
        try:
            _tool_denied_counter.add(
                1, {"tool": tool, "agent_id": agent_id, "reason_class": reason_class}
            )
        except Exception as exc:
            log.warning("otel_denied_metric_failed", error=str(exc))


def emit_release_target(target: str, result: str) -> None:
    if _release_targets_counter is not None:
        try:
            _release_targets_counter.add(1, {"target": target, "result": result})
        except Exception as exc:
            log.warning("otel_release_metric_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def init_sync() -> None:
    """Call at process start, after init_logging()."""
    _init_otel()
    _warn_if_removed_backend_configured()


def _warn_if_removed_backend_configured() -> None:
    """One warning naming any removed-backend variable that is still set.

    Names only: LOKI_URL and NATS_URL can carry credentials in their userinfo.
    """
    removed = get_config().removed_backend_env
    if removed:
        log.warning(
            "config_ignored_removed_backend",
            variables=removed,
            detail=(
                "githost-mcp 0.19.0 removed the Loki, Prometheus and NATS backends; these "
                "variables are ignored. OTel (OTEL_EXPORTER_OTLP_ENDPOINT) is the telemetry "
                "path, and the audit JSONL is the durable record."
            ),
        )

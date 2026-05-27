"""
OTEL, Loki, Prometheus, and NATS observability — all opt-in via env vars.

None of these activate unless the corresponding env var is set. Import errors
for optional dependencies are silenced so the server starts without them.
"""

from __future__ import annotations

import json
import os
import time
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
_tool_duration_histogram: Any = None
_release_targets_counter: Any = None


def _init_otel() -> None:
    global _tracer, _meter, _tool_calls_counter, _tool_duration_histogram, _release_targets_counter
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
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter  # type: ignore
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter  # type: ignore
            span_exporter = OTLPSpanExporter(endpoint=config.otel_endpoint)
            metric_exporter = OTLPMetricExporter(endpoint=config.otel_endpoint)
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # type: ignore
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter  # type: ignore
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
        meter = metrics.get_meter(config.otel_service_name)
        _tool_calls_counter = meter.create_counter(
            "githost.tool.calls",
            description="Number of githost-mcp tool calls",
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
        log.info("otel_initialized", endpoint=config.otel_endpoint)
    except Exception as exc:
        log.warning("otel_init_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Prometheus (/metrics scrape endpoint)
# ---------------------------------------------------------------------------

_prom_tool_calls: Any = None
_prom_tool_duration: Any = None
_prom_release_targets: Any = None


def _init_prometheus() -> None:
    global _prom_tool_calls, _prom_tool_duration, _prom_release_targets
    config = get_config()
    if not config.metrics_port:
        return
    try:
        from prometheus_client import Counter, Histogram, start_http_server  # type: ignore

        _prom_tool_calls = Counter(
            "githost_tool_calls_total",
            "Total githost-mcp tool calls",
            ["tool", "provider", "agent_id", "result"],
        )
        _prom_tool_duration = Histogram(
            "githost_tool_duration_ms",
            "githost-mcp tool call duration in ms",
            ["tool", "provider"],
        )
        _prom_release_targets = Counter(
            "githost_release_targets_total",
            "Total release target publishes",
            ["target", "result"],
        )
        start_http_server(config.metrics_port)
        log.info("prometheus_started", port=config.metrics_port)
    except Exception as exc:
        log.warning("prometheus_init_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Loki log push
# ---------------------------------------------------------------------------

_loki_url: str = ""
_loki_static_labels: dict[str, str] = {}


def _init_loki() -> None:
    global _loki_url, _loki_static_labels
    config = get_config()
    if not config.loki_url:
        return
    _loki_url = config.loki_url.rstrip("/")
    _loki_static_labels = {"app": "githost-mcp"}
    for pair in config.loki_labels.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            _loki_static_labels[k.strip()] = v.strip()
    log.info("loki_configured", url=_loki_url)


async def _push_loki(labels: dict[str, str], message: str) -> None:
    if not _loki_url:
        return
    try:
        import httpx  # already a core dep

        all_labels = {**_loki_static_labels, **labels}
        ts_ns = str(int(time.time() * 1e9))
        payload = {
            "streams": [{"stream": all_labels, "values": [[ts_ns, message]]}]
        }
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{_loki_url}/loki/api/v1/push",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        log.warning("loki_push_failed", error=str(exc))


# ---------------------------------------------------------------------------
# NATS publishing
# ---------------------------------------------------------------------------

_nats_client: Any = None
_nats_prefix: str = "githost"


async def _init_nats() -> None:
    global _nats_client, _nats_prefix
    config = get_config()
    if not config.nats_url:
        return
    try:
        import nats  # type: ignore

        _nats_client = await nats.connect(config.nats_url)
        _nats_prefix = config.nats_subject_prefix
        log.info("nats_connected", url=config.nats_url)
    except Exception as exc:
        log.warning("nats_init_failed", error=str(exc))


async def _publish_nats(subject_suffix: str, data: dict) -> None:
    if _nats_client is None:
        return
    try:
        subject = f"{_nats_prefix}.{subject_suffix}"
        await _nats_client.publish(subject, json.dumps(data).encode())
    except Exception as exc:
        log.warning("nats_publish_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Unified emit — called by audit.py after each tool invocation
# ---------------------------------------------------------------------------

async def emit_tool_event(
    tool: str,
    provider: str,
    repo_basename: str,
    result: str,
    duration_ms: int,
) -> None:
    config = get_config()
    agent_id = config.agent_id

    # OTEL span + metrics
    if _tracer is not None:
        try:
            from opentelemetry import trace  # type: ignore

            with _tracer.start_as_current_span(f"githost.{tool}") as span:
                span.set_attributes({
                    "githost.tool": tool,
                    "githost.provider": provider,
                    "githost.agent_id": agent_id,
                    "githost.repo": repo_basename,
                    "githost.result": result,
                    "githost.duration_ms": duration_ms,
                })
        except Exception as exc:
            log.warning("otel_span_failed", error=str(exc))

    if _tool_calls_counter is not None:
        try:
            _tool_calls_counter.add(1, {"tool": tool, "provider": provider, "agent_id": agent_id, "result": result})
            _tool_duration_histogram.record(duration_ms, {"tool": tool, "provider": provider})
        except Exception as exc:
            log.warning("otel_metric_failed", error=str(exc))

    # Prometheus
    if _prom_tool_calls is not None:
        try:
            _prom_tool_calls.labels(tool=tool, provider=provider, agent_id=agent_id, result=result).inc()
            _prom_tool_duration.labels(tool=tool, provider=provider).observe(duration_ms)
        except Exception as exc:
            log.warning("prometheus_record_failed", error=str(exc))

    # Loki
    if _loki_url:
        loki_labels = {"agent_id": agent_id, "tool": tool, "provider": provider}
        msg = json.dumps({"tool": tool, "provider": provider, "result": result, "duration_ms": duration_ms})
        await _push_loki(loki_labels, msg)

    # NATS
    if _nats_client is not None:
        await _publish_nats(
            f"tool.{tool}",
            {"tool": tool, "provider": provider, "agent_id": agent_id, "repo": repo_basename,
             "result": result, "duration_ms": duration_ms},
        )


def emit_release_target(target: str, result: str) -> None:
    if _release_targets_counter is not None:
        try:
            _release_targets_counter.add(1, {"target": target, "result": result})
        except Exception as exc:
            log.warning("otel_release_metric_failed", error=str(exc))
    if _prom_release_targets is not None:
        try:
            _prom_release_targets.labels(target=target, result=result).inc()
        except Exception as exc:
            log.warning("prometheus_release_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def init_sync() -> None:
    """Call at process start (before event loop) for sync init."""
    _init_otel()
    _init_prometheus()
    _init_loki()


async def init_async() -> None:
    """Call inside async lifespan for async init."""
    await _init_nats()

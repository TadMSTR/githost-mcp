"""Per-call scope: correlation ids from scoped-mcp, and the open-span safety net.

scoped-mcp (>= 1.20.0, `forward_identity: true` on the githost-mcp module) adds three
request headers to every proxied call:

  X-Forge-Run-Id      the dispatcher run, a UUID
  X-Forge-Task-Id     the task-queue task, a UUID
  X-Forge-Session-Id  scoped-mcp's per-client session id

githost-mcp's own MCP session id is no use for grouping calls: scoped-mcp opens a new
HTTP connection per proxied call, so every call gets a fresh session. These ids are the
ones that span a build.

**They are attribution, not authorization.** Anyone holding this process's bearer token
can send any value. In normal operation only scoped-mcp holds it, but a same-uid process
that reads the token could forge a run id (vikunja#1193, agent isolation). Nothing in
githost-mcp may gate on them; they are recorded, and that is all.

CallScopeMiddleware reads the headers once per call, before the tool runs, into a
ContextVar. FastMCP runs a sync tool on an AnyIO worker thread with a copy of the
context, so the tool sees the value, and AuditCtx objects created there register in the
scope's list, which the middleware can still see after the tool returns (probed against
fastmcp 3.4.11, githost-scoped-2026-11 p2 Phase 0). Any AuditCtx the tool left
unfinished is finished there with `error:unfinished`, so no span stays open.

Outside an HTTP request (stdio, or a test calling a tool function directly) there is no
scope: the ids are absent and AuditCtx simply has no safety net.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Wire contract shared with scoped-mcp (scoped_mcp/modules/mcp_proxy_config.py): the
# header names and the session-id pattern below must match it. A rename on either side
# silently records null here, with no correlation_invalid.
RUN_ID_HEADER = "x-forge-run-id"
TASK_ID_HEADER = "x-forge-task-id"
SESSION_ID_HEADER = "x-forge-session-id"

# Canonical dashed UUID, nothing else. Matched with fullmatch: `$` would accept a
# trailing newline.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
#: scoped-mcp's session id is a uuid5 today; the pattern leaves room for the process
#: default it falls back to on stdio. 128 characters (plan constant).
MAX_SESSION_ID_CHARS = 128
_SESSION_RE = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_SESSION_ID_CHARS}}}")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")


def valid_uuid(value: str | None) -> str | None:
    """The value lowercased if it is a canonical UUID, else None."""
    if value is None or not _UUID_RE.fullmatch(value):
        return None
    return value.lower()


def valid_session_id(value: str | None) -> str | None:
    if value is None or not _SESSION_RE.fullmatch(value):
        return None
    return value


def valid_trace_id(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    return lowered if _TRACE_ID_RE.fullmatch(lowered) else None


@dataclass(frozen=True)
class Correlation:
    run_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    #: Names of headers that were present but failed validation. The value itself is
    #: never recorded: an invalid value is arbitrary caller text.
    invalid: tuple[str, ...] = ()


_EMPTY = Correlation()


def correlation_from_headers(headers: dict[str, str]) -> Correlation:
    """Validate the three headers. A missing header is absent; a bad one is named."""
    invalid: list[str] = []

    def take(name: str, check) -> str | None:
        raw = headers.get(name)
        if raw is None:
            return None
        value = check(raw)
        if value is None:
            invalid.append(name.removeprefix("x-forge-").replace("-", "_"))
        return value

    return Correlation(
        run_id=take(RUN_ID_HEADER, valid_uuid),
        task_id=take(TASK_ID_HEADER, valid_uuid),
        session_id=take(SESSION_ID_HEADER, valid_session_id),
        invalid=tuple(invalid),
    )


@dataclass
class CallScope:
    correlation: Correlation = _EMPTY
    open_ctxs: list[Any] = field(default_factory=list)


_scope: contextvars.ContextVar[CallScope | None] = contextvars.ContextVar(
    "githost_call_scope", default=None
)


def current_scope() -> CallScope | None:
    return _scope.get()


def current_correlation() -> Correlation:
    scope = _scope.get()
    return scope.correlation if scope is not None else _EMPTY


def register(ctx: Any) -> None:
    """Record an AuditCtx in the current call's scope, if there is one."""
    scope = _scope.get()
    if scope is not None:
        scope.open_ctxs.append(ctx)


_header_read_failed_logged = False


def _read_headers() -> dict[str, str]:
    global _header_read_failed_logged
    try:
        from fastmcp.server.dependencies import get_http_headers

        # Lowercased keys; x-forge-* is not in its default exclude set (Phase 0).
        return {k.lower(): v for k, v in get_http_headers().items()}
    except Exception as e:
        # Outside a request get_http_headers() returns {} without raising, so this is a
        # real fault (a FastMCP API change, say). Every entry would silently lose its ids,
        # so say so, once per process.
        if not _header_read_failed_logged:
            _header_read_failed_logged = True
            log.warning("correlation_header_read_failed", error_class=type(e).__name__)
        return {}


def _finish_leftovers(scope: CallScope) -> None:
    for ctx in scope.open_ctxs:
        if not getattr(ctx, "finished", True):
            log.warning("audit_ctx_unfinished", tool=getattr(ctx, "tool", None))
            try:
                ctx.finish("error:unfinished")
            except Exception as exc:  # pragma: no cover - defence in depth
                log.warning("audit_ctx_unfinished_finish_failed", error=str(exc))


try:
    from fastmcp.server.middleware import Middleware as _Middleware
except Exception:  # pragma: no cover - fastmcp is a hard dependency
    _Middleware = object  # type: ignore[assignment,misc]


class CallScopeMiddleware(_Middleware):  # type: ignore[misc,valid-type]
    """Opens a CallScope for each tool call; closes any span the tool left open."""

    async def on_call_tool(self, context, call_next):
        scope = CallScope(correlation=correlation_from_headers(_read_headers()))
        token = _scope.set(scope)
        try:
            return await call_next(context)
        finally:
            _finish_leftovers(scope)
            _scope.reset(token)

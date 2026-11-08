"""httpx clients whose requests become child spans of the current tool span.

Every outbound Gitea and Woodpecker call, and the GitHub job-log fetch, goes through
`async_client()` / `sync_client()`. The wrapping transport opens one child span per
request with the method, host, response status and a **path template**, never the path
itself. PyGithub and python-gitlab calls get their child span in github_call() and
gitlab_call() instead (they use requests, not httpx).

The template keeps a path segment only if it is one of the API words in
`_PATH_WORDS`; every other segment (owner, repo, tag, number, ".diff" suffix) becomes
`{}`. So the template is built from a fixed vocabulary by construction. An owner, repo or
tag that happens to equal one of those words (a repo named `config`, say) is kept, so the
guarantee is that only `_PATH_WORDS` members can appear, not that no repo name ever does.
No token, userinfo or query string can reach a span attribute through it.
The query string is dropped before templating, and so is the host's userinfo.

Env proxies still work: the client is built without a `transport=` argument, so httpx
mounts its env-proxy transports as usual, and each of them is wrapped too. A proxied
request is traced like any other.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from . import audit
from .observability import set_child_attrs

log = structlog.get_logger(__name__)

#: API path words kept verbatim in a url.template. Everything else becomes `{}`.
_PATH_WORDS = frozenset(
    {
        "actions",
        "api",
        "commits",
        "comments",
        "config",
        "dismissals",
        "dispatches",
        "files",
        "info",
        "issues",
        "jobs",
        "logs",
        "lookup",
        "merge",
        "orgs",
        "pipelines",
        "pulls",
        "queue",
        "releases",
        "repos",
        "rerun",
        "rerun-failed-jobs",
        "reviews",
        "runs",
        "status",
        "tags",
        "v1",
        "workflows",
    }
)
_TEMPLATE_MAX_SEGMENTS = 12


def path_template(path: str) -> str:
    """`/api/v1/repos/host-forge/scripts/pulls/12` → `/api/v1/repos/{}/{}/pulls/{}`."""
    segments = [s for s in path.split("?", 1)[0].split("/") if s]
    shown = [s if s in _PATH_WORDS else "{}" for s in segments[:_TEMPLATE_MAX_SEGMENTS]]
    if len(segments) > _TEMPLATE_MAX_SEGMENTS:
        shown.append("{more}")
    return "/" + "/".join(shown)


def _request_attrs(request: httpx.Request) -> dict[str, Any]:
    return {
        "http.request.method": request.method,
        "server.address": request.url.host,
        "url.template": path_template(request.url.path),
    }


class TracingAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, provider: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._provider = provider
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        with audit.current_child(f"http.{self._provider}", **_request_attrs(request)) as span:
            response = await self._inner.handle_async_request(request)
            set_child_attrs(span, {"http.response.status_code": response.status_code})
            return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class TracingTransport(httpx.BaseTransport):
    def __init__(self, provider: str, inner: httpx.BaseTransport | None = None) -> None:
        self._provider = provider
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with audit.current_child(f"http.{self._provider}", **_request_attrs(request)) as span:
            response = self._inner.handle_request(request)
            set_child_attrs(span, {"http.response.status_code": response.status_code})
            return response

    def close(self) -> None:
        self._inner.close()


_private_attrs_missing_logged = False


def _wrap_transports(client: httpx.Client | httpx.AsyncClient, wrap: Any) -> None:
    """Wrap the client's own transport and every env-proxy mount in a tracing transport.

    Passing `transport=` to httpx turns env proxies off (`allow_env_proxies = trust_env and
    transport is None`), so the client is built without one and its transports are wrapped
    afterwards. That keeps HTTP(S)_PROXY / NO_PROXY, and every kwarg (`verify`, `cert`,
    ...) applies to the inner transports exactly as it would without tracing. `_transport`
    and `_mounts` are private to httpx (0.27 and 0.28 both have them);
    test_spans.py::test_client_private_transport_attrs_exist fails if they move, and an
    httpx without them gets an untraced client and one warning, never a failure.
    """
    global _private_attrs_missing_logged
    transport = getattr(client, "_transport", None)
    mounts = getattr(client, "_mounts", None)
    if transport is None or not isinstance(mounts, dict):
        # An httpx that moved them: keep the working client untraced rather than fail
        # every HTTP tool. Said once per process so the lost child spans are visible.
        if not _private_attrs_missing_logged:
            _private_attrs_missing_logged = True
            log.warning("httptrace_untraced_httpx_internals_moved")
        return
    client._transport = wrap(transport)  # type: ignore[union-attr]
    client._mounts = {  # type: ignore[union-attr]
        pattern: (wrap(inner) if inner is not None else None) for pattern, inner in mounts.items()
    }


def async_client(provider: str, **kwargs: Any) -> httpx.AsyncClient:
    """httpx.AsyncClient(**kwargs) whose requests are traced as `http.<provider>`."""
    client = httpx.AsyncClient(**kwargs)
    _wrap_transports(client, lambda inner: TracingAsyncTransport(provider, inner))
    return client


def sync_client(provider: str, **kwargs: Any) -> httpx.Client:
    """httpx.Client(**kwargs) whose requests are traced as `http.<provider>`."""
    client = httpx.Client(**kwargs)
    _wrap_transports(client, lambda inner: TracingTransport(provider, inner))
    return client

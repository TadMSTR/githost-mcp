"""httpx clients whose requests become child spans of the current tool span.

Every outbound Gitea and Woodpecker call, and the GitHub job-log fetch, goes through
`async_client()` / `sync_client()`. The wrapping transport opens one child span per
request with the method, host, response status and a **path template**, never the path
itself. PyGithub and python-gitlab calls get their child span in github_call() and
gitlab_call() instead (they use requests, not httpx).

The template keeps a path segment only if it is one of the API words in
`_PATH_WORDS`; every other segment (owner, repo, tag, number, ".diff" suffix) becomes
`{}`. So the template is built from a fixed vocabulary by construction: no repo name,
branch, tag, token, userinfo or query string can reach a span attribute through it.
The query string is dropped before templating, and so is the host's userinfo.

Env proxies still work: httpx mounts proxy transports from the environment beside the
one passed here. A proxied request is simply not traced.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import audit
from .observability import set_child_attrs

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


def async_client(provider: str, **kwargs: Any) -> httpx.AsyncClient:
    """httpx.AsyncClient(**kwargs) whose requests are traced as `http.<provider>`."""
    return httpx.AsyncClient(transport=TracingAsyncTransport(provider), **kwargs)


def sync_client(provider: str, **kwargs: Any) -> httpx.Client:
    """httpx.Client(**kwargs) whose requests are traced as `http.<provider>`."""
    return httpx.Client(transport=TracingTransport(provider), **kwargs)

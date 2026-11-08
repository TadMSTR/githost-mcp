"""PyGithub wrapper with credential masking on auth errors."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
import structlog

from ..config import get_config
from ..logview import DOWNLOAD_MAX, decode_capped, read_capped

log = structlog.get_logger(__name__)

_client: Any = None


def get_github():
    """Return a cached Github instance. Raises ValueError on missing token."""
    global _client
    if _client is not None:
        return _client
    config = get_config()
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is not set")
    try:
        from github import Auth, Github  # type: ignore

        # The positional form emits a DeprecationWarning on every call and is
        # removed in PyGithub 3.x.
        _client = Github(auth=Auth.Token(config.github_token))
        return _client
    except Exception as e:
        raise ValueError(f"GitHub client init failed: {type(e).__name__}") from None


def github_call(fn, *args, **kwargs):
    """Execute a PyGithub call, masking credentials from any exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        config = get_config()
        if config.github_token and config.github_token in msg:
            msg = msg.replace(config.github_token, "***")
        raise ValueError(msg) from None


# GitHub Actions job logs: `GET /repos/{o}/{r}/actions/jobs/{id}/logs` answers 302 with a
# short-lived signed URL on a blob host (productionresultssa<N>.blob.core.windows.net,
# observed 2026-11-07; older runs used *.actions.githubusercontent.com). The redirect is
# never followed automatically: the GitHub token must not be sent to the blob host, and a
# Location outside these hosts is refused rather than fetched.
# (githost-scoped-2026-11 p1, Phase 3)
GITHUB_API = "https://api.github.com"
_LOG_HOST_RES = (
    re.compile(r"\Aproductionresultssa\d+\.blob\.core\.windows\.net\Z"),
    re.compile(r"\A[a-z0-9-]+\.actions\.githubusercontent\.com\Z"),
)
# The download is bounded before the line view ever sees it (logview.DOWNLOAD_MAX). A job
# log past this is cut, and the caller is told. Module-level so tests can lower it.
LOG_DOWNLOAD_MAX = DOWNLOAD_MAX
_REDIRECTS = (301, 302, 303, 307, 308)


def log_host_allowed(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and not url.userinfo
        and url.port in (None, 443)
        and _is_log_host(url.host)
    )


class _SignedLogUrlFilter(logging.Filter):
    """Strip the signed query from Actions log-host URLs in httpx's request log line.

    httpx logs every request URL at INFO, and that log reaches the app log file, which is
    shipped to Loki. The blob URL's query string is a (short-lived) signed credential.
    Only the matching argument is replaced; record.args keeps its shape, so the format
    string still renders.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(isinstance(a, httpx.URL) for a in args):
            record.args = tuple(
                a.copy_with(query=b"redacted")
                if isinstance(a, httpx.URL) and a.query and _is_log_host(a.host)
                else a
                for a in args
            )
        return True


def _is_log_host(host: str | None) -> bool:
    return any(rx.match(host or "") for rx in _LOG_HOST_RES)


_URL_WITH_QUERY_RE = re.compile(r"https://([A-Za-z0-9.-]+)(:\d+)?([^\s'\"?]*)\?[^\s'\"]*")


def _redact_signed_urls(text: str) -> str:
    def repl(m: re.Match) -> str:
        if _is_log_host(m.group(1).lower()):
            return f"https://{m.group(1)}{m.group(2) or ''}{m.group(3)}?redacted"
        return m.group(0)

    return _URL_WITH_QUERY_RE.sub(repl, text)


class _SignedLogUrlTraceFilter(logging.Filter):
    """Strip the signed query from Actions log-host URLs in httpcore's DEBUG trace lines.

    httpcore logs `receive_response_headers.complete return_value=(..., [(b'Location',
    b'https://...?sig=...')])` as one pre-formatted string, so the 302's signed blob URL
    reached the app log at LOG_LEVEL=DEBUG (CodeRabbit CR-04, PR #34). The message is
    rewritten only when it carries no args, so a formatted record keeps its shape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args and isinstance(record.msg, str):
            record.msg = _redact_signed_urls(record.msg)
        return True


_URL_LOG_FILTER = _SignedLogUrlFilter()
if _URL_LOG_FILTER not in logging.getLogger("httpx").filters:
    logging.getLogger("httpx").addFilter(_URL_LOG_FILTER)

# Logger filters don't propagate: each httpcore module logs on its own named logger.
_HTTPCORE_LOGGERS = (
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_TRACE_LOG_FILTER = _SignedLogUrlTraceFilter()
for _name in _HTTPCORE_LOGGERS:
    if _TRACE_LOG_FILTER not in logging.getLogger(_name).filters:
        logging.getLogger(_name).addFilter(_TRACE_LOG_FILTER)


def fetch_job_log(repo: str, job_id: int) -> tuple[str, bool]:
    """Return (log text, cut) for one GitHub Actions job.

    `repo` must already have passed the 'owner/repo' check. Raises ValueError with a
    caller-safe message on any failure.
    """
    config = get_config()
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is not set")
    api_headers = {
        "Authorization": f"Bearer {config.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # No client-level headers or auth: the only request that carries the token is the
    # one with api_headers passed explicitly.
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        resp = client.get(
            f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs", headers=api_headers
        )
        if resp.status_code in (404, 410):
            raise ValueError(
                f"GitHub job log not available (HTTP {resp.status_code}): the job does not "
                "exist, or its logs have expired"
            )
        if resp.status_code in (401, 403):
            raise ValueError(f"GitHub refused the job log request (HTTP {resp.status_code})")
        if resp.status_code not in _REDIRECTS:
            raise ValueError(f"Unexpected GitHub response for job log: HTTP {resp.status_code}")
        location = resp.headers.get("location", "")
        try:
            target = httpx.URL(location)
        except Exception:
            raise ValueError("GitHub job log redirect has an unparseable Location") from None
        if not log_host_allowed(target):
            # Name the host only: the query string is a signed credential.
            raise ValueError(
                f"Refused GitHub job log redirect to host {target.host or '(none)'!r}: "
                "not an allowlisted Actions log host"
            )
        with client.stream("GET", target) as blob:
            if blob.status_code != 200:
                raise ValueError(f"Job log download failed: HTTP {blob.status_code}")
            data, cut = read_capped(blob.iter_bytes(), LOG_DOWNLOAD_MAX)
            return decode_capped(data, cut), cut

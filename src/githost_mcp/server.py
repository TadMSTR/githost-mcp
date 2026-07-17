"""
githost-mcp FastMCP server — tool registration and startup.

45 tools across 8 categories: local git, GitHub, Gitea, GitLab, release,
registry (PyPI/npm), Woodpecker CI, and audit query.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

from .audit import init_logging
from .config import get_config
from .observability import init_async, init_sync
from .tools import audit_query, git_local, gitea, github, gitlab, registry, release, woodpecker

log = structlog.get_logger(__name__)

# Hosts treated as loopback for the non-loopback fail-closed guard in main().
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# audit.py/security.py credential scrubbing only redacts tokens longer than 4 chars
# (`if t and len(t) > 4`) — a shorter GITHOST_MCP_AUTH_TOKEN would silently defeat that
# and could appear in cleartext in logs/audit trail. Require enough length that the
# scrub floor is never in play, well short of what secrets.token_hex(32) produces.
_MIN_AUTH_TOKEN_LENGTH = 16


@asynccontextmanager
async def lifespan(app):
    await init_async()
    log.info("githost_mcp_started")
    yield
    log.info("githost_mcp_stopped")


_config = get_config()

# Auth is gated on GITHOST_MCP_AUTH_TOKEN being set, independent of transport —
# stdio mode has no HTTP surface so this only matters when TRANSPORT=http.
# Option B: per-request identity would hook here (swap this static verifier for
# one that resolves AGENT_ID from a request header instead of the process env).
_auth = None
if _config.auth_token:
    _auth = StaticTokenVerifier(
        tokens={_config.auth_token: {"sub": "scoped-mcp", "client_id": "cli"}}
    )

mcp = FastMCP(
    name="githost-mcp",
    instructions=(
        "Unified git MCP server: local git operations (gitpython), GitHub, Gitea, and GitLab "
        "release management, PyPI/npm publishing, Woodpecker CI, and coordinated release "
        "orchestration. Every call is logged to a structured JSONL audit trail tagged with "
        "AGENT_ID. Write operations require ALLOWED_REPO_ROOTS to be configured."
    ),
    lifespan=lifespan,
    auth=_auth,
)

# Register tools from each module
git_local.register(mcp)
github.register(mcp)
gitea.register(mcp)
gitlab.register(mcp)
release.register(mcp)
registry.register(mcp)
woodpecker.register(mcp)
audit_query.register(mcp)

# Sync init (logging, OTEL, Prometheus) runs at import time so it's ready before first tool call
init_logging()
init_sync()


def main() -> None:
    config = get_config()
    if config.transport == "http":
        if config.http_host not in _LOOPBACK_HOSTS and not config.allow_nonloopback:
            raise RuntimeError(
                f"Refusing to bind githost-mcp HTTP transport to non-loopback host "
                f"{config.http_host!r}. Set GITHOST_MCP_ALLOW_NONLOOPBACK=1 to override."
            )
        if not config.auth_token:
            raise RuntimeError(
                "Refusing to start githost-mcp HTTP transport without GITHOST_MCP_AUTH_TOKEN "
                "set. HTTP mode must not run with an unauthenticated, reachable port."
            )
        if len(config.auth_token) < _MIN_AUTH_TOKEN_LENGTH:
            raise RuntimeError(
                f"GITHOST_MCP_AUTH_TOKEN is too short ({len(config.auth_token)} chars, need "
                f">= {_MIN_AUTH_TOKEN_LENGTH}) to be reliably redacted by the credential filter. "
                'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
            )
        mcp.run(transport="http", host=config.http_host, port=config.http_port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()

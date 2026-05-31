"""
githost-mcp FastMCP server — tool registration and startup.

39 tools across 8 categories: local git, GitHub, Gitea, GitLab, release,
registry (PyPI/npm), Woodpecker CI, and audit query.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP

from .audit import init_logging
from .observability import init_async, init_sync
from .tools import audit_query, git_local, gitea, github, gitlab, registry, release, woodpecker

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app):
    await init_async()
    log.info("githost_mcp_started")
    yield
    log.info("githost_mcp_stopped")


mcp = FastMCP(
    name="githost-mcp",
    instructions=(
        "Unified git MCP server: local git operations (gitpython), GitHub, Gitea, and GitLab "
        "release management, PyPI/npm publishing, Woodpecker CI, and coordinated release "
        "orchestration. Every call is logged to a structured JSONL audit trail tagged with "
        "AGENT_ID. Write operations require ALLOWED_REPO_ROOTS to be configured."
    ),
    lifespan=lifespan,
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
    mcp.run()


if __name__ == "__main__":
    main()

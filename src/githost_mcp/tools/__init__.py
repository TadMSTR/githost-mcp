"""githost-mcp tool modules.

register_all() is the one registration path. server.py uses it to serve the tools, and
`githost-mcp --list-tools` (cli.py) uses it to list them, so the two cannot disagree.
Importing the tool modules has no side effects: no config read, no log file, no env var.
"""

from __future__ import annotations

from . import audit_query, git_local, gitea, github, gitlab, registry, release, woodpecker

TOOL_MODULES = (git_local, github, gitea, gitlab, release, registry, woodpecker, audit_query)


def register_all(mcp) -> None:
    """Register every githost tool on `mcp`."""
    for module in TOOL_MODULES:
        module.register(mcp)

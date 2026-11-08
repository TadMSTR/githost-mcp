"""The `githost-mcp` console script.

With no arguments it runs the server, as before. `--list-tools [--json]` prints every
registered tool with its kind (`read` | `effect`) and whether it is in the HITL class,
then exits. host-forge/scripts `grant-lint.py --tools` and `githost-grant-gate.sh`
consume the JSON form (githost-scoped-2026-11 p3).

Listing must work anywhere the wheel is installed, with an empty environment, and leave
nothing behind. So it never imports `githost_mcp.server`: that module reads the config,
opens the app log and warns about AUDIT_SIGNING_KEY at import. It registers the tools on
a bare FastMCP instead, through the same register_all() the server uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

EXIT_OK = 0
EXIT_ERROR = 2


def tool_catalog() -> dict:
    """`{"server", "version", "tools": [{"name", "kind", "hitl"}]}`, sorted by name.

    Raises ValueError for a served tool that is in neither READ_TOOLS nor EFFECT_TOOLS.
    The closed-world test stops that shipping, but a gate must not guess a kind.
    """
    from fastmcp import FastMCP

    from . import __version__, effects
    from .tools import register_all

    mcp = FastMCP(name="githost-mcp")
    register_all(mcp)
    names = sorted(t.name for t in asyncio.run(mcp.list_tools(run_middleware=False)))
    tools = []
    for name in names:
        if name in effects.EFFECT_TOOLS:
            kind = "effect"
        elif name in effects.READ_TOOLS:
            kind = "read"
        else:
            raise ValueError(f"tool {name!r} is in neither READ_TOOLS nor EFFECT_TOOLS")
        tools.append({"name": name, "kind": kind, "hitl": name in effects.HITL_TOOLS})
    return {"server": "githost-mcp", "version": __version__, "tools": tools}


def _print_table(catalog: dict) -> None:
    width = max(len(t["name"]) for t in catalog["tools"])
    for t in catalog["tools"]:
        hitl = "hitl" if t["hitl"] else ""
        print(f"{t['name']:<{width}}  {t['kind']:<6}  {hitl}".rstrip())
    total = len(catalog["tools"])
    gated = sum(t["hitl"] for t in catalog["tools"])
    print(f"# {catalog['server']} {catalog['version']}: {total} tools, {gated} hitl")


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(
        prog="githost-mcp",
        description="Run the githost-mcp server, or list its tools.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print every tool with its kind and HITL class, then exit",
    )
    parser.add_argument("--json", action="store_true", help="with --list-tools: emit JSON")
    args = parser.parse_args(argv)

    if args.json and not args.list_tools:
        parser.error("--json needs --list-tools")

    if args.list_tools:
        try:
            catalog = tool_catalog()
        except Exception as exc:  # a gate reads the exit code, not a traceback
            print(f"githost-mcp --list-tools: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if args.json:
            print(json.dumps(catalog, indent=2))
        else:
            _print_table(catalog)
        return EXIT_OK

    from .server import main as serve

    serve()
    return None


if __name__ == "__main__":
    sys.exit(main())

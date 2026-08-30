"""Tests for the FastMCP server module (import-time registration, lifespan, main)."""

import importlib
import pathlib
import re
from unittest.mock import patch

import pytest

from githost_mcp.config import reset_config


def _import_server(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    reset_config()
    import githost_mcp.server as srv

    return srv


def _reload_server(monkeypatch, tmp_path):
    """Like _import_server, but forces re-execution of module-level code
    (mcp/auth construction) so it picks up env set in this test."""
    srv = _import_server(monkeypatch, tmp_path)
    importlib.reload(srv)
    return srv


def test_server_module_builds_named_mcp(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    assert srv.mcp.name == "githost-mcp"


def test_server_main_invokes_run(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    with patch.object(srv.mcp, "run") as run:
        srv.main()
    run.assert_called_once()


@pytest.mark.asyncio
async def test_server_lifespan_runs_async_init(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    async with srv.lifespan(object()):
        pass  # entering and exiting exercises init_async + start/stop logging


def test_main_stdio_calls_run_with_no_transport_kwargs(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.delenv("TRANSPORT", raising=False)
    reset_config()
    with patch.object(srv.mcp, "run") as run:
        srv.main()
    run.assert_called_once_with()


def test_main_http_calls_run_with_host_and_port(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "s3cr3t-token-value")
    reset_config()
    with patch.object(srv.mcp, "run") as run:
        srv.main()
    run.assert_called_once_with(transport="http", host="127.0.0.1", port=8620)


def test_main_http_without_auth_token_fails_closed(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.delenv("GITHOST_MCP_AUTH_TOKEN", raising=False)
    reset_config()
    with (
        patch.object(srv.mcp, "run") as run,
        pytest.raises(RuntimeError, match="GITHOST_MCP_AUTH_TOKEN"),
    ):
        srv.main()
    run.assert_not_called()


def test_main_http_short_auth_token_fails_closed(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "short")
    reset_config()
    with (
        patch.object(srv.mcp, "run") as run,
        pytest.raises(RuntimeError, match="too short"),
    ):
        srv.main()
    run.assert_not_called()


def test_main_http_min_length_auth_token_allowed(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "a" * 16)
    reset_config()
    with patch.object(srv.mcp, "run") as run:
        srv.main()
    run.assert_called_once_with(transport="http", host="127.0.0.1", port=8620)


def test_main_http_nonloopback_host_fails_closed(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.delenv("GITHOST_MCP_ALLOW_NONLOOPBACK", raising=False)
    reset_config()
    with patch.object(srv.mcp, "run") as run, pytest.raises(RuntimeError, match="non-loopback"):
        srv.main()
    run.assert_not_called()


def test_main_http_nonloopback_host_allowed_with_explicit_override(monkeypatch, tmp_path):
    srv = _import_server(monkeypatch, tmp_path)
    monkeypatch.setenv("TRANSPORT", "http")
    monkeypatch.setenv("HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("HTTP_PORT", "8620")
    monkeypatch.setenv("GITHOST_MCP_ALLOW_NONLOOPBACK", "1")
    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "s3cr3t-token-value")
    reset_config()
    with patch.object(srv.mcp, "run") as run:
        srv.main()
    run.assert_called_once_with(transport="http", host="0.0.0.0", port=8620)


def test_auth_is_none_when_token_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHOST_MCP_AUTH_TOKEN", raising=False)
    srv = _reload_server(monkeypatch, tmp_path)
    assert srv.mcp.auth is None


def test_auth_is_static_token_verifier_when_token_set(monkeypatch, tmp_path):
    from fastmcp.server.auth import StaticTokenVerifier

    monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", "s3cr3t-token-value")
    srv = _reload_server(monkeypatch, tmp_path)
    assert isinstance(srv.mcp.auth, StaticTokenVerifier)

    # Restore a clean (no-auth) module state so later reimports in this
    # session default to unauthenticated stdio, matching a fresh process.
    monkeypatch.delenv("GITHOST_MCP_AUTH_TOKEN", raising=False)
    _reload_server(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# Documented tool count
#
# Added with git_branch_delete_remote (0.13.0), which found the README headline
# already stale by two: it claimed 63 while 65 were registered, even though every
# per-section count under it was correct. Three places record this one fact —
# the README headline, its section headers, and the code — and only the headline
# had drifted, silently, because nothing re-derived it.
# ---------------------------------------------------------------------------


def _registered_tool_names() -> set[str]:
    """Every @mcp.tool function across the tool modules.

    Counted by importing and registering rather than by grepping decorators, so
    a tool that stops being registered stops counting.
    """
    import importlib

    names: set[str] = set()

    class CountingMCP:
        def tool(self, fn):
            names.add(fn.__name__)
            return fn

    for mod in (
        "git_local",
        "github",
        "gitea",
        "gitlab",
        "woodpecker",
        "release",
        "registry",
        "audit_query",
    ):
        importlib.import_module(f"githost_mcp.tools.{mod}").register(CountingMCP())
    return names


def test_readme_headline_tool_count_matches_the_code():
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text()
    match = re.search(r"^## Tools \((\d+) total\)", readme, re.MULTILINE)
    assert match, "README must carry a '## Tools (N total)' headline"
    assert int(match.group(1)) == len(_registered_tool_names()), (
        f"README headline says {match.group(1)} tools, code registers "
        f"{len(_registered_tool_names())}"
    )


def test_readme_section_counts_sum_to_the_headline():
    """The headline drifted while the sections stayed right, so check both ends."""
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text()
    headline = int(re.search(r"^## Tools \((\d+) total\)", readme, re.MULTILINE).group(1))
    tools_section = readme.split("## Tools (", 1)[1].split("\n## ", 1)[0]
    sections = [int(n) for n in re.findall(r"^### .+ \((\d+)\)", tools_section, re.MULTILINE)]
    assert sections, "expected per-capability '### Name (N)' headers under ## Tools"
    assert sum(sections) == headline, (
        f"section counts {sections} sum to {sum(sections)}, headline says {headline}"
    )

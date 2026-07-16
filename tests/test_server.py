"""Tests for the FastMCP server module (import-time registration, lifespan, main)."""

import importlib
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

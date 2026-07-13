"""Tests for the FastMCP server module (import-time registration, lifespan, main)."""

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

"""Integration tests for the HTTP transport's bearer-token auth gate.

Exercises the real server.py auth wiring end-to-end via FastMCP's ASGI app
(mcp.http_app()) and a Starlette TestClient, rather than re-implementing the
StaticTokenVerifier construction in isolation.
"""

import importlib

from starlette.testclient import TestClient

from githost_mcp.config import reset_config

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_HEADERS = {"Accept": "application/json, text/event-stream"}


def _reload_server_with_token(monkeypatch, tmp_path, token):
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    if token:
        monkeypatch.setenv("GITHOST_MCP_AUTH_TOKEN", token)
    else:
        monkeypatch.delenv("GITHOST_MCP_AUTH_TOKEN", raising=False)
    reset_config()

    import githost_mcp.server as srv

    importlib.reload(srv)
    return srv


def test_http_endpoint_rejects_missing_token(monkeypatch, tmp_path):
    srv = _reload_server_with_token(monkeypatch, tmp_path, "good-token-value")
    app = srv.mcp.http_app()
    with TestClient(app) as client:
        r = client.post("/mcp/", json=_INIT_BODY, headers=_HEADERS)
    assert r.status_code == 401


def test_http_endpoint_rejects_wrong_token(monkeypatch, tmp_path):
    srv = _reload_server_with_token(monkeypatch, tmp_path, "good-token-value")
    app = srv.mcp.http_app()
    with TestClient(app) as client:
        r = client.post(
            "/mcp/", json=_INIT_BODY, headers={**_HEADERS, "Authorization": "Bearer wrong-token"}
        )
    assert r.status_code == 401


def test_http_endpoint_accepts_correct_token(monkeypatch, tmp_path):
    srv = _reload_server_with_token(monkeypatch, tmp_path, "good-token-value")
    app = srv.mcp.http_app()
    with TestClient(app) as client:
        r = client.post(
            "/mcp/",
            json=_INIT_BODY,
            headers={**_HEADERS, "Authorization": "Bearer good-token-value"},
        )
    assert r.status_code == 200


def test_http_endpoint_unauthenticated_when_no_token_configured(monkeypatch, tmp_path):
    """No GITHOST_MCP_AUTH_TOKEN set → stdio-mode default, no auth gate on the app."""
    srv = _reload_server_with_token(monkeypatch, tmp_path, None)
    app = srv.mcp.http_app()
    with TestClient(app) as client:
        r = client.post("/mcp/", json=_INIT_BODY, headers=_HEADERS)
    assert r.status_code == 200

    # Restore a clean (no-auth) module state for any subsequent reimports.
    _reload_server_with_token(monkeypatch, tmp_path, None)

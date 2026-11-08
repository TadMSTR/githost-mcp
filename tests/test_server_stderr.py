"""The HTTP server starts and stops with nothing below WARNING on stderr (vikunja#1437).

PM2 writes stderr to githost-mcp-<agent>-error.log. Before 0.21.0 every start wrote the
FastMCP banner, an "Update available" box and uvicorn's INFO lines there.
"""

import os
import signal
import socket
import subprocess
import sys
import time


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_server_start_and_stop_write_no_info_to_stderr(tmp_path):
    port = _free_port()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TRANSPORT": "http",
        "HTTP_HOST": "127.0.0.1",
        "HTTP_PORT": str(port),
        "GITHOST_MCP_AUTH_TOKEN": "t" * 64,
        "AUDIT_LOG_FILE": str(tmp_path / "audit.jsonl"),
        "LOG_FILE": str(tmp_path / "app.log"),
        "AUDIT_SIGNING_KEY": "testsecret1234567890abcdef12345678",
        "AGENT_ID": "test",
        "WORKSPACE_POLICY_PATH": "/nonexistent/workspace-policy.yml",
        "ALLOWED_REPO_ROOTS": str(tmp_path),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "githost_mcp.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        assert proc.poll() is None, "server exited during start"
    finally:
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=20)
    assert "INFO" not in err, err
    assert "Update available" not in err, err
    assert "FastMCP" not in err, err

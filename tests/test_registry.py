"""Tests for tools/registry.py: npm_publish/pypi_publish subprocess invocation."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from githost_mcp.audit import init_logging
from githost_mcp.config import reset_config

FAKE_PYPI_TOKEN = "pypi-fakefakefakefakefakefakefakefakefake"
FAKE_NPM_TOKEN = "npm_fakefakefakefakefakefakefakefake"


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    monkeypatch.setenv("AUDIT_SIGNING_KEY", "testsecret1234567890abcdef12345678")
    monkeypatch.setenv("AGENT_ID", "test")
    monkeypatch.setenv("PYPI_TOKEN", FAKE_PYPI_TOKEN)
    monkeypatch.setenv("NPM_TOKEN", FAKE_NPM_TOKEN)
    reset_config()
    init_logging()


@pytest.fixture()
def tools():
    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    from githost_mcp.tools.registry import register

    register(MockMCP())
    return registered


def _audit_entries(tmp_path):
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture()
def pkg_repo(tmp_path):
    repo = tmp_path / "pkg"
    dist = repo / "dist"
    dist.mkdir(parents=True)
    (dist / "pkg-1.0.0-py3-none-any.whl").write_text("fake wheel")
    return repo


def _mock_run(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# --- pypi_publish -----------------------------------------------------------


def test_pypi_publish_success(tools, pkg_repo):
    check_ok = _mock_run(returncode=0, stdout="Checking dist...PASSED")
    upload_ok = _mock_run(returncode=0, stdout="Uploaded pkg-1.0.0")
    with patch(
        "githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_ok]
    ) as run:
        result = tools["pypi_publish"](str(pkg_repo))
    assert "error" not in result
    assert result["target"] == "pypi"
    assert run.call_count == 2  # twine check, twine upload — dist non-empty, no build step


def test_pypi_publish_builds_when_dist_empty(tools, tmp_path):
    repo = tmp_path / "pkg2"
    (repo / "dist").mkdir(parents=True)
    build_ok = _mock_run(returncode=0)
    check_ok = _mock_run(returncode=0)
    upload_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run",
        side_effect=[build_ok, check_ok, upload_ok],
    ) as run:
        result = tools["pypi_publish"](str(repo))
    assert "error" not in result
    assert run.call_count == 3
    build_cmd = run.call_args_list[0].args[0]
    assert build_cmd[:3] == ["python", "-m", "build"]


def test_pypi_publish_build_failure(tools, tmp_path):
    repo = tmp_path / "pkg3"
    (repo / "dist").mkdir(parents=True)
    build_fail = _mock_run(returncode=1, stderr="setup.py not found")
    with patch("githost_mcp.tools.registry.subprocess.run", return_value=build_fail):
        result = tools["pypi_publish"](str(repo))
    assert "error" in result
    assert "Build failed" in result["error"]


def test_pypi_publish_upload_failure(tools, pkg_repo):
    check_ok = _mock_run(returncode=0)
    upload_fail = _mock_run(returncode=1, stderr="403 Forbidden: invalid credentials")
    with patch("githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_fail]):
        result = tools["pypi_publish"](str(pkg_repo))
    assert "error" in result
    assert "Upload failed" in result["error"]


def test_pypi_publish_token_never_in_subprocess_argv(tools, pkg_repo):
    """PYPI_TOKEN must be passed via env, never as a CLI argument (AGENTS.md)."""
    check_ok = _mock_run(returncode=0)
    upload_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_ok]
    ) as run:
        tools["pypi_publish"](str(pkg_repo))
    upload_call = run.call_args_list[1]
    cmd_args = upload_call.args[0]
    assert FAKE_PYPI_TOKEN not in cmd_args
    assert upload_call.kwargs["env"]["TWINE_PASSWORD"] == FAKE_PYPI_TOKEN


def test_pypi_publish_audit_log_scrubs_token_on_failure(tools, pkg_repo, tmp_path):
    """Even if a subprocess leaks the token into stderr, the audit trail must not."""
    check_ok = _mock_run(returncode=0)
    upload_fail = _mock_run(returncode=1, stderr=f"403 Forbidden token={FAKE_PYPI_TOKEN}")
    with patch("githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_fail]):
        tools["pypi_publish"](str(pkg_repo))
    entries = _audit_entries(tmp_path)
    assert len(entries) == 1
    assert FAKE_PYPI_TOKEN not in json.dumps(entries[0])


def test_pypi_publish_unknown_target(tools, pkg_repo):
    result = tools["pypi_publish"](str(pkg_repo), target="npm")
    assert "error" in result
    assert "Unknown target" in result["error"]


def test_pypi_publish_gitea_requires_url(tools, pkg_repo, monkeypatch):
    monkeypatch.delenv("GITEA_URL", raising=False)
    reset_config()
    result = tools["pypi_publish"](str(pkg_repo), target="gitea")
    assert "error" in result
    assert "GITEA_URL" in result["error"]


def test_pypi_publish_missing_token(tools, pkg_repo, monkeypatch):
    monkeypatch.delenv("PYPI_TOKEN", raising=False)
    reset_config()
    result = tools["pypi_publish"](str(pkg_repo))
    assert "error" in result
    assert "Token" in result["error"]


def test_pypi_publish_missing_dist_dir(tools, tmp_path):
    repo = tmp_path / "nodist"
    repo.mkdir()
    result = tools["pypi_publish"](str(repo))
    assert "error" in result
    assert "dist directory not found" in result["error"]


def test_pypi_publish_path_outside_allowed_root_blocked(tools, tmp_path):
    outside = tmp_path.parent / "outside-repo"
    os.makedirs(outside / "dist", exist_ok=True)
    result = tools["pypi_publish"](str(outside))
    assert "error" in result
    assert "not under any allowed root" in result["error"]


# --- npm_publish -------------------------------------------------------------


@pytest.fixture()
def npm_repo(tmp_path):
    repo = tmp_path / "npmpkg"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "pkg", "version": "1.0.0"}')
    return repo


def test_npm_publish_success(tools, npm_repo):
    which_ok = _mock_run(returncode=0, stdout="/usr/bin/npm")
    publish_ok = _mock_run(returncode=0, stdout="+ pkg@1.0.0")
    with patch("githost_mcp.tools.registry.subprocess.run", side_effect=[which_ok, publish_ok]):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" not in result


def test_npm_publish_failure(tools, npm_repo):
    which_ok = _mock_run(returncode=0)
    publish_fail = _mock_run(returncode=1, stderr="npm ERR! 403 Forbidden")
    with patch("githost_mcp.tools.registry.subprocess.run", side_effect=[which_ok, publish_fail]):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" in result
    assert "npm publish failed" in result["error"]


def test_npm_publish_npm_not_found(tools, npm_repo):
    which_missing = _mock_run(returncode=1)
    with patch("githost_mcp.tools.registry.subprocess.run", return_value=which_missing):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" in result
    assert "npm is not found" in result["error"]


def test_npm_publish_missing_package_json(tools, tmp_path):
    repo = tmp_path / "nopkgjson"
    repo.mkdir()
    result = tools["npm_publish"](str(repo))
    assert "error" in result
    assert "package.json not found" in result["error"]


def test_npm_publish_missing_token(tools, npm_repo, monkeypatch):
    monkeypatch.delenv("NPM_TOKEN", raising=False)
    reset_config()
    result = tools["npm_publish"](str(npm_repo))
    assert "error" in result
    assert "NPM_TOKEN" in result["error"]


def test_npm_publish_token_never_in_subprocess_argv(tools, npm_repo):
    which_ok = _mock_run(returncode=0)
    publish_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run", side_effect=[which_ok, publish_ok]
    ) as run:
        tools["npm_publish"](str(npm_repo))
    publish_call = run.call_args_list[1]
    cmd_args = publish_call.args[0]
    assert FAKE_NPM_TOKEN not in cmd_args
    assert publish_call.kwargs["env"]["NPM_TOKEN"] == FAKE_NPM_TOKEN


def test_npm_publish_custom_registry_and_tag(tools, npm_repo):
    which_ok = _mock_run(returncode=0)
    publish_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run", side_effect=[which_ok, publish_ok]
    ) as run:
        result = tools["npm_publish"](str(npm_repo), registry="https://npm.example.com", tag="beta")
    publish_cmd = run.call_args_list[1].args[0]
    assert "--registry" in publish_cmd and "https://npm.example.com" in publish_cmd
    assert "--tag" in publish_cmd and "beta" in publish_cmd
    assert result["registry"] == "https://npm.example.com"

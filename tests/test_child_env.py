"""Publish children get the allowlisted environment, proved with real children (vikunja#610).

`python -m build` runs the repository's own build backend, and `npm pack`/`npm publish` run
its lifecycle scripts: repo code. Each test plants forge credentials in the parent, runs the
real tool under clean_env() with repo code that writes the environment it received, and
checks no credential arrived. Each has a control that runs the same child with the parent's
whole environment, so a test that cannot fail shows up as a failing control.
"""

import json
import os
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest

from githost_mcp.config import reset_config
from githost_mcp.security import CHILD_ENV_ALLOWLIST, clean_env

PLANTED = {
    "GITHUB_TOKEN": "ghp_" + "Pl4" * 12,
    "GITEA_TOKEN": "gitea" + "Pl4" * 10,
    "AUDIT_SIGNING_KEY": "signing" + "Pl4" * 10,
    "GITHOST_MCP_AUTH_TOKEN": "bearer" + "Pl4" * 10,
}
# Checked by name only: its value is a small fd number.
PM2_IPC = {"NODE_CHANNEL_FD": "3"}

# An in-tree PEP 517 backend: it writes os.environ next to the wheel it pretends to build.
BACKEND = """
import json, os, zipfile

def get_requires_for_build_wheel(config_settings=None):
    return []

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with open(os.path.join(wheel_directory, "backend-env.json"), "w") as f:
        json.dump(dict(os.environ), f)
    name = "envdump-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as z:
        z.writestr("envdump/__init__.py", "")
    return name
"""


@pytest.fixture()
def planted(monkeypatch, tmp_path):
    for name, value in {**PLANTED, **PM2_IPC}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    reset_config()
    yield
    reset_config()


def _backend_pkg(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "envdump_backend.py").write_text(BACKEND)
    (pkg / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "envdump_backend"\nbackend-path = ["."]\n'
    )
    return pkg


def _build(pkg, out, env):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--outdir",
            str(out),
            str(pkg),
        ],
        cwd=pkg,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_clean(received: dict):
    for name, value in PLANTED.items():
        assert name not in received, name
        assert value not in json.dumps(received), name
    for name in PM2_IPC:
        assert name not in received, name
    assert set(received) - {"PWD", "SHLVL", "_", "OLDPWD"} - set(CHILD_ENV_ALLOWLIST) <= {
        # Set by python -m build / the interpreter itself for the backend subprocess.
        "PEP517_BUILD_BACKEND",
        "_PYPROJECT_HOOKS_BUILD_BACKEND",
        "_PYPROJECT_HOOKS_BACKEND_PATH_JSON",
        "PYTHONNOUSERSITE",
        "PYTHONSAFEPATH",
        "PIP_BUILD_TRACKER",
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
    }


def test_build_backend_receives_no_forge_credential(planted, tmp_path):
    pkg = _backend_pkg(tmp_path)
    result = _build(pkg, tmp_path / "dist", clean_env())
    assert result.returncode == 0, result.stderr
    _assert_clean(json.loads((tmp_path / "dist" / "backend-env.json").read_text()))


def test_build_backend_control_with_the_whole_environment(planted, tmp_path):
    """The 0.20.0 behaviour: repo code saw every credential."""
    pkg = _backend_pkg(tmp_path)
    result = _build(pkg, tmp_path / "dist", dict(os.environ))
    assert result.returncode == 0, result.stderr
    received = json.loads((tmp_path / "dist" / "backend-env.json").read_text())
    assert received["GITHUB_TOKEN"] == PLANTED["GITHUB_TOKEN"]


def _npm_pkg(tmp_path):
    pkg = tmp_path / "npkg"
    pkg.mkdir()
    script = (
        "node -e \"require('fs').writeFileSync('prepack-env.json', JSON.stringify(process.env))\""
    )
    (pkg / "package.json").write_text(
        json.dumps({"name": "envdump-npm", "version": "0.0.1", "scripts": {"prepack": script}})
    )
    return pkg


def _pack(pkg, env):
    return subprocess.run(
        ["npm", "pack", "--dry-run"], cwd=pkg, env=env, capture_output=True, text=True, timeout=120
    )


npm_required = pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")


@npm_required
def test_npm_lifecycle_script_receives_no_forge_credential(planted, tmp_path):
    pkg = _npm_pkg(tmp_path)
    result = _pack(pkg, {**clean_env(), "NPM_TOKEN": "npm_" + "Pl4" * 12})
    assert result.returncode == 0, result.stderr
    received = json.loads((pkg / "prepack-env.json").read_text())
    _assert_npm_clean(received)


def _assert_npm_clean(received: dict):
    for name, value in PLANTED.items():
        assert name not in received, name
        assert value not in json.dumps(received), name
    for name in PM2_IPC:
        assert name not in received, name
    # npm adds its own npm_* / NODE / INIT_CWD / COLOR variables for a lifecycle script.
    foreign = {
        k
        for k in received
        if k not in CHILD_ENV_ALLOWLIST
        and not k.startswith("npm_")
        and k not in {"NPM_TOKEN", "INIT_CWD", "NODE", "COLOR", "EDITOR", "PWD", "SHLVL", "_"}
    }
    assert not foreign, foreign


@npm_required
def test_npm_lifecycle_script_control_with_the_whole_environment(planted, tmp_path):
    pkg = _npm_pkg(tmp_path)
    result = _pack(pkg, dict(os.environ))
    assert result.returncode == 0, result.stderr
    received = json.loads((pkg / "prepack-env.json").read_text())
    assert received["GITEA_TOKEN"] == PLANTED["GITEA_TOKEN"]


def test_pypi_publish_build_child_gets_the_allowlist(planted, tmp_path, monkeypatch):
    """The tool passes clean_env() to every child it starts, the build included."""
    from githost_mcp.tools import registry

    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(tmp_path))
    monkeypatch.setenv("PYPI_TOKEN", "pypi-AgE" + "Pl4" * 20)
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    reset_config()
    pkg = tmp_path / "p"
    (pkg / "dist").mkdir(parents=True)
    envs = []

    def fake_run(cmd, *a, **kw):
        envs.append(kw.get("env"))
        if cmd[:3] == ["python", "-m", "build"]:
            (pkg / "dist" / "p-0.0.1-py3-none-any.whl").write_text("")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    registered = {}

    class MockMCP:
        def tool(self, fn):
            registered[fn.__name__] = fn
            return fn

    registry.register(MockMCP())
    with patch("githost_mcp.tools.registry.subprocess.run", fake_run):
        result = registered["pypi_publish"](str(pkg))
    assert "error" not in result, result
    assert len(envs) == 3  # build, twine check, twine upload
    for env in envs:
        assert env is not None
        assert "GITHUB_TOKEN" not in env and "GITEA_TOKEN" not in env
        assert set(env) <= set(CHILD_ENV_ALLOWLIST) | {"TWINE_PASSWORD", "TWINE_USERNAME"}

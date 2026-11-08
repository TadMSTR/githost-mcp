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


class _NpmRunner:
    """A subprocess.run stand-in that answers by argv: `npm publish` and `npm config get`."""

    def __init__(self, publish=None, config_stdout="https://registry.npmjs.org/\n", config_rc=0):
        self.publish = publish or _mock_run(returncode=0, stdout="+ pkg@1.0.0")
        self.config = _mock_run(returncode=config_rc, stdout=config_stdout)
        self.calls = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[:2] == ["npm", "publish"]:
            return self.publish
        if cmd[:3] == ["npm", "config", "get"]:
            return self.config
        raise AssertionError(f"unexpected subprocess: {cmd}")

    def call(self, *prefix):
        return next((c, k) for c, k in self.calls if c[: len(prefix)] == list(prefix))


@pytest.fixture()
def npm_on_path(monkeypatch):
    monkeypatch.setattr("githost_mcp.tools.registry.shutil.which", lambda *a, **k: "/usr/bin/npm")


def test_npm_publish_success(tools, npm_repo, npm_on_path):
    with patch("githost_mcp.tools.registry.subprocess.run", _NpmRunner()):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" not in result


def test_npm_publish_failure(tools, npm_repo, npm_on_path):
    runner = _NpmRunner(publish=_mock_run(returncode=1, stderr="npm ERR! 403 Forbidden"))
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" in result
    assert "npm publish failed" in result["error"]


def test_npm_publish_npm_not_found(tools, npm_repo, monkeypatch):
    monkeypatch.setattr("githost_mcp.tools.registry.shutil.which", lambda *a, **k: None)
    with patch("githost_mcp.tools.registry.subprocess.run") as run:
        result = tools["npm_publish"](str(npm_repo))
    assert "error" in result
    assert "npm is not found" in result["error"]
    run.assert_not_called()


def test_npm_publish_looks_for_npm_on_the_childs_path(tools, npm_repo, monkeypatch):
    seen = {}

    def which(name, path=None):
        seen["path"] = path
        return "/usr/bin/npm"

    monkeypatch.setenv("PATH", "/opt/node/bin:/usr/bin")
    monkeypatch.setattr("githost_mcp.tools.registry.shutil.which", which)
    with patch("githost_mcp.tools.registry.subprocess.run", _NpmRunner()):
        tools["npm_publish"](str(npm_repo))
    assert seen["path"] == "/opt/node/bin:/usr/bin"


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


def test_npm_publish_token_never_in_subprocess_argv(tools, npm_repo, npm_on_path):
    runner = _NpmRunner()
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        tools["npm_publish"](str(npm_repo))
    cmd_args, kwargs = runner.call("npm", "publish")
    assert FAKE_NPM_TOKEN not in cmd_args
    assert kwargs["env"]["NPM_TOKEN"] == FAKE_NPM_TOKEN


def test_npm_publish_custom_registry_and_tag(tools, npm_repo, npm_on_path):
    runner = _NpmRunner()
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        result = tools["npm_publish"](str(npm_repo), registry="https://npm.example.com", tag="beta")
    publish_cmd, _ = runner.call("npm", "publish")
    assert "--registry" in publish_cmd and "https://npm.example.com" in publish_cmd
    assert "--tag" in publish_cmd and "beta" in publish_cmd
    assert result["registry"] == "https://npm.example.com"


# --- NODE_CHANNEL_FD env stripping (GHOST-11) --------------------------------


_PM2_IPC_ENV_VARS = ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE", "NODE_UNIQUE_ID")


def test_npm_publish_strips_pm2_ipc_vars(tools, npm_repo, monkeypatch, npm_on_path):
    """Regression for GHOST-11: the npm child must not inherit PM2's IPC vars."""
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    runner = _NpmRunner()
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        tools["npm_publish"](str(npm_repo))
    _, kwargs = runner.call("npm", "publish")
    publish_env = kwargs["env"]
    for var in _PM2_IPC_ENV_VARS:
        assert var not in publish_env
    assert publish_env["NPM_TOKEN"] == FAKE_NPM_TOKEN


def test_pypi_publish_upload_strips_pm2_ipc_vars(tools, pkg_repo, monkeypatch):
    """Regression for GHOST-11: the twine upload child must not inherit PM2's IPC vars."""
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    check_ok = _mock_run(returncode=0)
    upload_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_ok]
    ) as run:
        tools["pypi_publish"](str(pkg_repo))
    upload_env = run.call_args_list[1].kwargs["env"]
    for var in _PM2_IPC_ENV_VARS:
        assert var not in upload_env
    assert upload_env["TWINE_PASSWORD"] == FAKE_PYPI_TOKEN


def test_pypi_publish_build_and_check_strip_pm2_ipc_vars(tools, tmp_path, monkeypatch):
    """The bare `python -m build` and `twine check` calls also need env= stripped."""
    for var in _PM2_IPC_ENV_VARS:
        monkeypatch.setenv(var, "leaked")
    repo = tmp_path / "pkg-build"
    (repo / "dist").mkdir(parents=True)
    build_ok = _mock_run(returncode=0)
    check_ok = _mock_run(returncode=0)
    upload_ok = _mock_run(returncode=0)
    with patch(
        "githost_mcp.tools.registry.subprocess.run",
        side_effect=[build_ok, check_ok, upload_ok],
    ) as run:
        tools["pypi_publish"](str(repo))
    build_env = run.call_args_list[0].kwargs["env"]
    check_env = run.call_args_list[1].kwargs["env"]
    for var in _PM2_IPC_ENV_VARS:
        assert var not in build_env
        assert var not in check_env


# --- vikunja#1340: dist_dir, registry and tag are validated -----------------------------


@pytest.mark.parametrize("dist_dir", ["../x", "/etc", "a/../../x", "", "dist\x00x"])
def test_pypi_publish_refuses_a_dist_dir_outside_the_repo(tools, pkg_repo, dist_dir):
    with patch("githost_mcp.tools.registry.subprocess.run") as run:
        result = tools["pypi_publish"](str(pkg_repo), dist_dir=dist_dir)
    assert "error" in result
    run.assert_not_called()


def test_pypi_publish_refuses_a_symlinked_dist_that_leaves_the_repo(tools, tmp_path):
    repo = tmp_path / "linked"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "evil-1.0.0-py3-none-any.whl").write_text("x")
    (repo / "dist").symlink_to(outside)
    with patch("githost_mcp.tools.registry.subprocess.run") as run:
        result = tools["pypi_publish"](str(repo))
    assert "resolves outside repo_path" in result["error"]
    run.assert_not_called()


def test_pypi_publish_accepts_a_nested_dist_dir(tools, tmp_path):
    from githost_mcp.tools.registry import validate_dist_dir

    (tmp_path / "r" / "build" / "dist").mkdir(parents=True)
    assert validate_dist_dir(str(tmp_path / "r"), "build/dist") == str(
        (tmp_path / "r" / "build" / "dist").resolve()
    )


@pytest.mark.parametrize(
    "registry",
    [
        "http://registry.example.com/",
        "https://user:pw@registry.example.com/",
        "https://tok@registry.example.com/",
        "https://" + "ab12" * 10 + "/x@registry.example.com/",
        "https:// space.example.com/",
        "ftp://registry.example.com/",
        "https://",
    ],
)
def test_npm_publish_refuses_a_bad_registry(tools, npm_repo, registry, npm_on_path):
    with patch("githost_mcp.tools.registry.subprocess.run") as run:
        result = tools["npm_publish"](str(npm_repo), registry=registry)
    assert "registry must be" in result["error"]
    run.assert_not_called()


@pytest.mark.parametrize("tag", ["--access=public", "a b", "x" * 65, "", "beta;rm", "../x"])
def test_npm_publish_refuses_a_bad_tag(tools, npm_repo, tag, npm_on_path):
    with patch("githost_mcp.tools.registry.subprocess.run") as run:
        result = tools["npm_publish"](str(npm_repo), tag=tag)
    assert "tag must be" in result["error"]
    run.assert_not_called()


# --- vikunja#1435, #1434 F-03/F-11: registry effects -------------------------------------


def test_npm_publish_records_the_registry_npm_resolved(tools, npm_repo, tmp_path, npm_on_path):
    """.npmrc names another registry: the effect says so, not registry.npmjs.org."""
    runner = _NpmRunner(config_stdout="https://npm.pkg.github.com/\n")
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        tools["npm_publish"](str(npm_repo))
    cmd, kwargs = runner.call("npm", "config", "get")
    assert cmd == ["npm", "config", "get", "registry"]
    assert kwargs["cwd"] == str(npm_repo.resolve())
    assert kwargs["env"]["NPM_TOKEN"] == FAKE_NPM_TOKEN
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert _audit_entries(tmp_path)[-1]["effect"]["target"] == "npm.pkg.github.com"


def test_npm_publish_registry_lookup_failure_is_incomplete(tools, npm_repo, tmp_path, npm_on_path):
    runner = _NpmRunner(config_rc=1, config_stdout="")
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        result = tools["npm_publish"](str(npm_repo))
    assert "error" not in result
    entry = _audit_entries(tmp_path)[-1]
    assert entry["result"] == "ok"
    assert entry["effect"]["target"] is None
    assert "target" in entry["effect_incomplete"]


def test_npm_publish_with_a_registry_records_its_host_only(tools, npm_repo, tmp_path, npm_on_path):
    runner = _NpmRunner()
    with patch("githost_mcp.tools.registry.subprocess.run", runner):
        tools["npm_publish"](str(npm_repo), registry="https://npm.example.com/path")
    assert _audit_entries(tmp_path)[-1]["effect"]["target"] == "npm.example.com"
    assert not [c for c, _ in runner.calls if c[:3] == ["npm", "config", "get"]]


def test_npm_identity_refuses_a_symlink(tmp_path):
    from githost_mcp.tools.registry import npm_identity

    (tmp_path / "real.json").write_text('{"name": "pkg", "version": "1.0.0"}')
    (tmp_path / "package.json").symlink_to(tmp_path / "real.json")
    assert npm_identity(str(tmp_path)) == (None, None)


def test_npm_identity_does_not_block_on_a_fifo(tmp_path):
    import threading

    from githost_mcp.tools.registry import npm_identity

    os.mkfifo(tmp_path / "package.json")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", npm_identity(str(tmp_path))))
    t.start()
    t.join(5)
    assert not t.is_alive(), "npm_identity blocked on a FIFO"
    assert out["r"] == (None, None)


@pytest.mark.parametrize("body", ['["pkg", "1.0.0"]', '"pkg"', "null", "{" + " " * (1 << 20) + "}"])
def test_npm_identity_refuses_non_objects_and_oversize(tmp_path, body):
    from githost_mcp.tools.registry import npm_identity

    (tmp_path / "package.json").write_text(body)
    assert npm_identity(str(tmp_path)) == (None, None)


def test_npm_identity_reads_a_normal_package_json(tmp_path):
    from githost_mcp.tools.registry import npm_identity

    (tmp_path / "package.json").write_text('{"name": "@s/pkg", "version": "1.2.3"}')
    assert npm_identity(str(tmp_path)) == ("@s/pkg", "1.2.3")


def test_dist_identity_one_package_wheel_and_sdist(tmp_path):
    from githost_mcp.tools.registry import dist_identity

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "My_Pkg-1.0.0-py3-none-any.whl").write_text("")
    (dist / "my_pkg-1.0.0.tar.gz").write_text("")
    # One (name, version) once names are normalised: a wheel and its sdist.
    assert dist_identity(str(dist)) == ("My_Pkg", "1.0.0")


@pytest.mark.parametrize(
    "files",
    [
        ["a-1.0.0-py3-none-any.whl", "b-1.0.0-py3-none-any.whl"],
        ["a-1.0.0-py3-none-any.whl", "a-1.0.1-py3-none-any.whl"],
    ],
)
def test_dist_identity_two_packages_or_versions_is_unknown(tmp_path, tools, files):
    from githost_mcp.tools.registry import dist_identity

    for f in files:
        (tmp_path / f).write_text("")
    assert dist_identity(str(tmp_path)) == (None, None)


def test_publish_output_is_scrubbed_before_it_is_cut(tools, pkg_repo):
    """A credential straddling the 500-character cut must not leave its prefix."""
    token = "ghp_" + "Cut9" * 9
    pad = "x" * (500 - 21) + " "
    check_ok = _mock_run(returncode=0)
    upload_fail = _mock_run(returncode=1, stderr=pad + token + " tail")
    with patch("githost_mcp.tools.registry.subprocess.run", side_effect=[check_ok, upload_fail]):
        result = tools["pypi_publish"](str(pkg_repo))
    assert token[:12] not in result["error"]


# --- audit F-04 / CR-20: the registry npm picks for this package -------------------------


class _KeyedNpmRunner(_NpmRunner):
    """Answers `npm config get <key>` per key; an unset key prints `undefined`."""

    def __init__(self, config: dict):
        super().__init__()
        self.keys = config

    def __call__(self, cmd, *args, **kwargs):
        if cmd[:3] == ["npm", "config", "get"]:
            self.calls.append((cmd, kwargs))
            return _mock_run(returncode=0, stdout=self.keys.get(cmd[3], "undefined") + "\n")
        return super().__call__(cmd, *args, **kwargs)


_GENERIC = {"registry": "https://registry.npmjs.org/"}


@pytest.mark.parametrize(
    ("package_json", "config", "registry_arg", "target"),
    [
        # A scoped package goes to its scope's registry, not the generic one.
        (
            {"name": "@acme/pkg", "version": "1.0.0"},
            {**_GENERIC, "@acme:registry": "https://npm.acme.example/"},
            None,
            "npm.acme.example",
        ),
        # A scope with no registry of its own uses the generic one.
        ({"name": "@acme/pkg", "version": "1.0.0"}, _GENERIC, None, "registry.npmjs.org"),
        # publishConfig sets the registry: precedence over the rest is npm's call.
        (
            {
                "name": "pkg",
                "version": "1.0.0",
                "publishConfig": {"registry": "https://x.example/"},
            },
            _GENERIC,
            None,
            None,
        ),
        # A scope registry competing with an explicit argument: not said for certain.
        (
            {"name": "@acme/pkg", "version": "1.0.0"},
            {**_GENERIC, "@acme:registry": "https://npm.acme.example/"},
            "https://registry.npmjs.org/",
            None,
        ),
        # Unscoped, explicit argument: the argument's host.
        ({"name": "pkg", "version": "1.0.0"}, _GENERIC, "https://r.example/", "r.example"),
        # An ambiguous authority in .npmrc has no host to record.
        (
            {"name": "pkg", "version": "1.0.0"},
            {"registry": "https://2130706433/x@registry.npmjs.org/"},
            None,
            None,
        ),
    ],
)
def test_npm_publish_target_follows_npms_registry_choice(
    tools, tmp_path, npm_on_path, package_json, config, registry_arg, target
):
    import json

    repo = tmp_path / "npmpkg"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps(package_json))
    with patch("githost_mcp.tools.registry.subprocess.run", _KeyedNpmRunner(config)):
        result = tools["npm_publish"](str(repo), registry=registry_arg)
    assert "error" not in result
    entry = _audit_entries(tmp_path)[-1]
    assert entry["effect"]["target"] == target
    if target is None:
        assert "target" in entry["effect_incomplete"]


def test_dist_identity_unknown_with_an_unreadable_file_name(tmp_path):
    # CR-09: twine uploads every file in dist/; a name the pattern can't read means the
    # (package, version) would describe only part of the upload.
    from githost_mcp.tools.registry import dist_identity

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "pkg-1.0.0-py3-none-any.whl").write_text("x")
    (dist / "pkg-1.0.0.tar.gz").write_text("x")
    assert dist_identity(str(dist)) == ("pkg", "1.0.0")
    (dist / ".gitignore").write_text("*")  # dotfiles are not uploaded
    assert dist_identity(str(dist)) == ("pkg", "1.0.0")
    (dist / "other.zip").write_text("x")
    assert dist_identity(str(dist)) == (None, None)

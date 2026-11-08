"""`githost-mcp --list-tools [--json]` (githost-scoped-2026-11 p3).

host-forge/scripts `grant-lint.py --tools` and `githost-grant-gate.sh` read this output
to learn the tool surface and the HITL class before githost-mcp restarts on a new wheel.
So it has to be exact (the served set, nothing more or less), and it has to run from a
bare venv with no env vars and leave no file behind.
"""

import asyncio
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import jsonschema
import pytest

from githost_mcp import __version__, cli, effects
from githost_mcp.config import reset_config

REPO = Path(__file__).resolve().parent.parent
CONSOLE_SCRIPT = Path(sys.executable).parent / "githost-mcp"

CATALOG_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["server", "version", "tools"],
    "properties": {
        "server": {"const": "githost-mcp"},
        "version": {"type": "string", "minLength": 1},
        "tools": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "kind", "hitl"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "kind": {"enum": ["read", "effect"]},
                    "hitl": {"type": "boolean"},
                },
            },
        },
    },
}


def _served(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    reset_config()
    import githost_mcp.server as srv

    return {t.name for t in asyncio.run(srv.mcp.list_tools(run_middleware=False))}


def test_the_catalog_is_exactly_what_the_server_serves(monkeypatch, tmp_path):
    catalog = cli.tool_catalog()
    assert {t["name"] for t in catalog["tools"]} == _served(monkeypatch, tmp_path)


def test_the_catalog_carries_the_classes():
    catalog = cli.tool_catalog()
    jsonschema.validate(catalog, CATALOG_SCHEMA)
    names = [t["name"] for t in catalog["tools"]]
    assert names == sorted(names)
    assert catalog["version"] == __version__
    for tool in catalog["tools"]:
        expected_kind = "effect" if tool["name"] in effects.EFFECT_TOOLS else "read"
        assert tool["kind"] == expected_kind, tool
        assert tool["hitl"] is (tool["name"] in effects.HITL_TOOLS), tool
    assert {t["name"] for t in catalog["tools"] if t["hitl"]} == effects.HITL_TOOLS


def test_an_unclassified_tool_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setattr(effects, "READ_TOOLS", effects.READ_TOOLS - {"git_status"})
    with pytest.raises(ValueError, match="git_status"):
        cli.tool_catalog()


def test_json_output(capsys):
    assert cli.main(["--list-tools", "--json"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    jsonschema.validate(catalog, CATALOG_SCHEMA)
    assert catalog == cli.tool_catalog()


def test_table_output(capsys):
    assert cli.main(["--list-tools"]) == 0
    out = capsys.readouterr().out.splitlines()
    total = len(cli.tool_catalog()["tools"])
    assert len(out) == total + 1
    assert out[-1] == f"# githost-mcp {__version__}: {total} tools, {len(effects.HITL_TOOLS)} hitl"
    rows = {line.split()[0]: line.split()[1:] for line in out[:-1]}
    assert rows["git_status"] == ["read"]
    assert rows["git_push"] == ["effect"]
    assert rows["release"] == ["effect", "hitl"]


def test_json_needs_list_tools(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--json"])
    assert exc.value.code == 2
    assert "--json needs --list-tools" in capsys.readouterr().err


def test_an_internal_error_exits_2(monkeypatch, capsys):
    def boom():
        raise RuntimeError("registration failed")

    monkeypatch.setattr(cli, "tool_catalog", boom)
    assert cli.main(["--list-tools", "--json"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError: registration failed" in captured.err


def test_no_arguments_runs_the_server(monkeypatch):
    calls = []
    import githost_mcp.server as srv

    monkeypatch.setattr(srv, "main", lambda: calls.append("served"))
    assert cli.main([]) is None
    assert calls == ["served"]


def test_the_console_script_points_at_the_cli():
    with open(REPO / "pyproject.toml", "rb") as fh:
        scripts = tomllib.load(fh)["project"]["scripts"]
    assert scripts["githost-mcp"] == "githost_mcp.cli:main"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param([str(CONSOLE_SCRIPT)], id="console-script"),
        pytest.param([sys.executable, "-I", "-m", "githost_mcp.cli"], id="module"),
    ],
)
def test_runs_with_an_empty_environment_and_writes_nothing(tmp_path, command):
    """No env var at all, a fresh HOME and cwd: exit 0, clean stderr, no file created.

    Importing githost_mcp.server would fail this: it opens the app log and warns about
    AUDIT_SIGNING_KEY on stderr at import.
    """
    if not Path(command[0]).exists():
        pytest.fail(f"{command[0]} is not installed in this venv")
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [*command, "--list-tools", "--json"],
        cwd=home,
        env={"HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    catalog = json.loads(result.stdout)
    jsonschema.validate(catalog, CATALOG_SCHEMA)
    assert catalog == cli.tool_catalog()
    assert list(home.rglob("*")) == [], "listing the tools must not create any file"


def test_the_empty_environment_control_detects_a_write(tmp_path):
    """The control for the test above: the server import, which does write, is caught."""
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        [sys.executable, "-I", "-c", "import githost_mcp.server"],
        cwd=home,
        env={"HOME": str(home), "LOG_FILE": str(home / "app.log")},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert list(home.rglob("*")) != []
    assert os.path.exists(home / "app.log")

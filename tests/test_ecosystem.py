"""ecosystem.config.js starts exactly the six live githost-mcp agents (vikunja#367).

harlock and steward are decommissioned. venv-deploy.sh `--recreate` used to run a bare
`pm2 start ecosystem.config.js`, which started every app in the file, both of them
included. host-forge/scripts' venv-deploy.sh `_register githost-mcp` row lists the same six
(scripts/tests/test_venv_deploy_recreate.bats pins that side).
"""

import json
import pathlib
import shutil
import subprocess

import pytest

LIVE_AGENTS = ["developer", "sysadmin", "security", "writer", "research", "fleet"]
ECOSYSTEM = pathlib.Path(__file__).resolve().parent.parent / "ecosystem.config.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ecosystem_apps_are_the_six_live_agents(tmp_path):
    script = (
        "process.env.HOME = process.argv[2];"
        "const os = require('os'); os.homedir = () => process.argv[2];"
        "const eco = require(process.argv[1]);"
        "console.log(JSON.stringify(eco.apps.map(a => [a.name, a.env.HTTP_PORT])));"
    )
    out = subprocess.run(
        ["node", "-e", script, str(ECOSYSTEM), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    apps = json.loads(out.stdout)
    assert [name for name, _ in apps] == [f"githost-mcp-{a}" for a in LIVE_AGENTS]
    ports = [port for _, port in apps]
    assert "8625" not in ports and "8627" not in ports


def test_agents_map_keys_are_the_six_live_agents():
    """Without node: read the AGENTS map's keys from the source."""
    import re

    text = ECOSYSTEM.read_text()
    block = text[text.index("const AGENTS = {") : text.index("};", text.index("const AGENTS = {"))]
    keys = re.findall(r"^\s+(\w+): \{ httpPort: \d+ \},$", block, re.M)
    assert keys == LIVE_AGENTS

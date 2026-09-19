"""The version is declared twice; pin them together.

Added 2026-09-19 because they had already drifted: `pyproject.toml` read 0.13.0 while
`__init__.py` still read 0.9.0 — four minor versions apart, with nothing to catch it.
A stale `__version__` is how a deployed version gets misreported.

Read pyproject.toml from disk rather than importlib.metadata: metadata comes
from the installed dist-info, which goes stale the moment the source is bumped
without a reinstall — so a parity test built on it can pass while the two
declarations actually disagree.
"""

import pathlib
import tomllib

import githost_mcp

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_package_version_matches_pyproject():
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert githost_mcp.__version__ == declared


def test_changelog_documents_the_current_version():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert f"## [{githost_mcp.__version__}]" in changelog

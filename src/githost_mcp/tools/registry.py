"""PyPI and npm registry publish tools (2 tools)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from urllib.parse import urlsplit

import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..security import clean_env, scrub, validate_write_path

log = structlog.get_logger(__name__)

_PYPI_URL = "https://upload.pypi.org/legacy/"
_TESTPYPI_URL = "https://test.pypi.org/legacy/"

# Package names and versions as they appear in a distribution file name or package.json.
# Anything that does not match is recorded as unknown rather than copied into the effect.
_DIST_RE = re.compile(r"([A-Za-z0-9_.]+)-(\d[A-Za-z0-9_.+!]*?)(?:-.+\.whl|\.tar\.gz|\.zip)")
_NPM_NAME_RE = re.compile(r"(@[a-z0-9._-]+/)?[a-z0-9._-]{1,214}")
_VERSION_RE = re.compile(r"[0-9A-Za-z.+!_-]{1,64}")


def dist_identity(dist_path: str) -> tuple[str | None, str | None]:
    """(package, version) from the first distribution file name in `dist_path`."""
    try:
        names = sorted(os.listdir(dist_path))
    except OSError:
        return None, None
    for name in names:
        m = _DIST_RE.fullmatch(name)
        if m:
            return m.group(1), m.group(2)
    return None, None


def npm_identity(repo_path: str) -> tuple[str | None, str | None]:
    """(name, version) from package.json, each only if it has the expected shape."""
    try:
        with open(os.path.join(repo_path, "package.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    name, version = data.get("name"), data.get("version")
    return (
        name if isinstance(name, str) and _NPM_NAME_RE.fullmatch(name) else None,
        version if isinstance(version, str) and _VERSION_RE.fullmatch(version) else None,
    )


def register(mcp) -> None:
    @mcp.tool
    def pypi_publish(
        repo_path: str,
        target: str = "pypi",
        dist_dir: str = "dist",
    ) -> dict:
        """Build and publish a Python package to PyPI, TestPyPI, or a Gitea registry.

        Tokens are injected via environment variables, never via command-line arguments.
        Requires twine to be installed in the active Python environment.

        Args:
            repo_path: Absolute path to the Python package directory.
            target: 'pypi', 'testpypi', or 'gitea' (default: pypi).
            dist_dir: Directory containing built distributions (default: dist).
        """
        ac = AuditCtx(
            "pypi_publish", "registry", repo_path, {"repo_path": repo_path, "target": target}
        )
        try:
            repo_path = validate_write_path(repo_path)
            config = get_config()
            if target == "pypi":
                token = config.pypi_token
                repo_url = _PYPI_URL
            elif target == "testpypi":
                token = config.pypi_test_token
                repo_url = _TESTPYPI_URL
            elif target == "gitea":
                token = config.gitea_token
                if not config.gitea_url:
                    raise ValueError("GITEA_URL is not set")
                repo_url = f"{config.gitea_url.rstrip('/')}/api/packages/{config.gitea_owner}/pypi"
            else:
                raise ValueError(f"Unknown target '{target}'. Use: pypi, testpypi, gitea")

            if not token:
                raise ValueError(f"Token for target '{target}' is not set")

            dist_path = os.path.join(repo_path, dist_dir)
            if not os.path.isdir(dist_path):
                raise ValueError(f"dist directory not found: {dist_path}")

            # Build with pip first if dist is empty
            dist_files = os.listdir(dist_path)
            if not dist_files:
                build_result = subprocess.run(
                    ["python", "-m", "build", "--outdir", dist_path],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=clean_env(),
                )
                if build_result.returncode != 0:
                    raise ValueError(f"Build failed: {build_result.stderr[:500]}")

            # twine check
            check_result = subprocess.run(
                ["twine", "check", f"{dist_path}/*"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                env=clean_env(),
            )
            if check_result.returncode != 0:
                raise ValueError(f"Twine check failed: {check_result.stderr[:500]}")

            # twine upload — token via env, never in args
            upload_env = {**clean_env(), "TWINE_PASSWORD": token, "TWINE_USERNAME": "__token__"}
            upload_result = subprocess.run(
                ["twine", "upload", "--repository-url", repo_url, f"{dist_path}/*"],
                cwd=repo_path,
                env=upload_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if upload_result.returncode != 0:
                raise ValueError(f"Upload failed: {upload_result.stderr[:500]}")

            package, version = dist_identity(dist_path)
            ac.effect(target=target, package=package, version=version)
            ac.finish("ok")
            return {"target": target, "dist_dir": dist_path, "stdout": upload_result.stdout[:500]}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    def npm_publish(
        repo_path: str,
        registry: str | None = None,
        tag: str | None = None,
    ) -> dict:
        """Publish an npm package. Token is injected via environment, not CLI args.

        Args:
            repo_path: Absolute path to the npm package directory (must contain package.json).
            registry: Registry URL (default: https://registry.npmjs.org/).
            tag: Publish tag (default: 'latest').
        """
        ac = AuditCtx(
            "npm_publish", "registry", repo_path, {"repo_path": repo_path, "registry": registry}
        )
        try:
            repo_path = validate_write_path(repo_path)
            config = get_config()
            if not config.npm_token:
                raise ValueError("NPM_TOKEN is not set")

            package_json = os.path.join(repo_path, "package.json")
            if not os.path.isfile(package_json):
                raise ValueError(f"package.json not found: {package_json}")

            # Check npm is available
            which_result = subprocess.run(["which", "npm"], capture_output=True, text=True)
            if which_result.returncode != 0:
                raise ValueError("npm is not found in PATH — install Node.js on this host")

            cmd = ["npm", "publish"]
            if registry:
                cmd += ["--registry", registry]
            if tag:
                cmd += ["--tag", tag]

            # Token via environment, not in command args
            pub_env = {**clean_env(), "NPM_TOKEN": config.npm_token}
            result = subprocess.run(
                cmd,
                cwd=repo_path,
                env=pub_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise ValueError(f"npm publish failed: {result.stderr[:500]}")

            package, version = npm_identity(repo_path)
            # The registry is recorded as its host only (a URL may carry userinfo).
            ac.effect(
                target=urlsplit(registry).hostname if registry else "registry.npmjs.org",
                package=package,
                version=version,
            )
            ac.finish("ok")
            return {
                "stdout": result.stdout[:500],
                "registry": registry or "https://registry.npmjs.org/",
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

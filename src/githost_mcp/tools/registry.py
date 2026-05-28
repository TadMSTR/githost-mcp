"""PyPI and npm registry publish tools (2 tools)."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..security import validate_write_path

log = structlog.get_logger(__name__)

_PYPI_URL = "https://upload.pypi.org/legacy/"
_TESTPYPI_URL = "https://test.pypi.org/legacy/"


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
        ac = AuditCtx("pypi_publish", "registry", repo_path, {"repo_path": repo_path, "target": target})
        try:
            validate_write_path(repo_path)
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
            )

            # twine upload — token via env, never in args
            upload_env = {**os.environ, "TWINE_PASSWORD": token, "TWINE_USERNAME": "__token__"}
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

            ac.finish("ok")
            return {"target": target, "dist_dir": dist_path, "stdout": upload_result.stdout[:500]}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def npm_publish(
        repo_path: str,
        registry: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> dict:
        """Publish an npm package. Token is injected via environment, not CLI args.

        Args:
            repo_path: Absolute path to the npm package directory (must contain package.json).
            registry: Registry URL (default: https://registry.npmjs.org/).
            tag: Publish tag (default: 'latest').
        """
        ac = AuditCtx("npm_publish", "registry", repo_path, {"repo_path": repo_path, "registry": registry})
        try:
            validate_write_path(repo_path)
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
            pub_env = {**os.environ, "NPM_TOKEN": config.npm_token}
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

            ac.finish("ok")
            return {"stdout": result.stdout[:500], "registry": registry or "https://registry.npmjs.org/"}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

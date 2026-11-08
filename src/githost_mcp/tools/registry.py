"""PyPI and npm registry publish tools (2 tools)."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess

import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..errors import InvalidArgument
from ..security import clean_env, scrub, split_authority, url_host, validate_write_path

log = structlog.get_logger(__name__)

_PYPI_URL = "https://upload.pypi.org/legacy/"
_TESTPYPI_URL = "https://test.pypi.org/legacy/"

# Package names and versions as they appear in a distribution file name or package.json.
# Anything that does not match is recorded as unknown rather than copied into the effect.
_DIST_RE = re.compile(r"([A-Za-z0-9_.]+)-(\d[A-Za-z0-9_.+!]*?)(?:-.+\.whl|\.tar\.gz|\.zip)")
_NPM_NAME_RE = re.compile(r"(@[a-z0-9._-]+/)?[a-z0-9._-]{1,214}")
_VERSION_RE = re.compile(r"[0-9A-Za-z.+!_-]{1,64}")


# A package.json larger than this is not read for the effect (it is never this big).
_PACKAGE_JSON_MAX = 1 << 20
# An npm dist-tag: npm's own rule is looser, but these are what forge publishes with.
_NPM_TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def dist_identity(dist_path: str) -> tuple[str | None, str | None]:
    """(package, version) of what `dist/` holds, or (None, None) when it is not one.

    twine uploads every file in `dist/`. Two packages, or two versions of one, cannot be
    named by a single (package, version), so the effect records null and AuditCtx names
    both in `effect_incomplete` (#1434 F-11), instead of naming the first file only.
    """
    try:
        names = sorted(os.listdir(dist_path))
    except OSError:
        return None, None
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for name in names:
        if name.startswith("."):  # `twine upload dist/*` skips dotfiles
            continue
        m = _DIST_RE.fullmatch(name)
        if not m:
            # twine uploads it too, and its name can't be read: the pair would be a guess
            # about part of what went up (CR-09).
            return None, None
        found.setdefault((_normalized(m.group(1)), m.group(2)), (m.group(1), m.group(2)))
    if len(found) != 1:
        return None, None
    return next(iter(found.values()))


def _read_package_json(repo_path: str) -> dict | None:
    """package.json as a dict, or None.

    Read after the publish, so it never fails the call and never hangs it (#1434 F-03): a
    regular file only (lstat, then an O_NOFOLLOW|O_NONBLOCK open and fstat, so a symlink
    or a FIFO swapped in is refused, not followed or blocked on), at most 1 MiB, and a JSON
    object.
    """
    path = os.path.join(repo_path, "package.json")
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            raw = f.read(_PACKAGE_JSON_MAX + 1)
        if len(raw) > _PACKAGE_JSON_MAX:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def npm_identity(repo_path: str) -> tuple[str | None, str | None]:
    """(name, version) from package.json, each only if it has the expected shape."""
    data = _read_package_json(repo_path)
    if data is None:
        return None, None
    name, version = data.get("name"), data.get("version")
    return (
        name if isinstance(name, str) and _NPM_NAME_RE.fullmatch(name) else None,
        version if isinstance(version, str) and _VERSION_RE.fullmatch(version) else None,
    )


def validate_dist_dir(repo_path: str, dist_dir: str) -> str:
    """The real path of `dist_dir`, which must be a relative path that stays in the repo.

    `os.path.join(repo_path, "/x")` is "/x", and "../x" leaves the repo: either uploaded
    whatever sat there (vikunja#1340). A symlinked `dist` that resolves outside the repo is
    refused the same way.
    """
    if not dist_dir or os.path.isabs(dist_dir) or "\x00" in dist_dir:
        raise InvalidArgument("dist_dir must be a relative path inside repo_path")
    if ".." in os.path.normpath(dist_dir).split(os.sep):
        raise InvalidArgument("dist_dir must not contain '..'")
    real_repo = os.path.realpath(repo_path)
    real = os.path.realpath(os.path.join(real_repo, dist_dir))
    if os.path.commonpath([real_repo, real]) != real_repo:
        raise InvalidArgument("dist_dir resolves outside repo_path")
    return real


def validate_registry(registry: str | None) -> None:
    """An npm registry must be an https:// URL with a host and no userinfo (vikunja#1340).

    None (npm's configured registry) passes."""
    if registry is None:
        return
    if (
        not registry.startswith("https://")
        or any(c.isspace() or ord(c) < 0x20 for c in registry)
        or split_authority(registry[len("https://") :])[0] is not None
        or url_host(registry) is None
    ):
        raise InvalidArgument("registry must be an https:// URL with a host and no credentials")


def validate_npm_tag(tag: str | None) -> None:
    """None (npm's default tag) passes."""
    if tag is not None and not _NPM_TAG_RE.fullmatch(tag):
        raise InvalidArgument("tag must be 1-64 of letters, digits, '.', '_' or '-'")


def _npm_config_get(repo_path: str, env: dict, key: str) -> str | None:
    """`npm config get <key>` in the repo (its .npmrc included), or None when unset."""
    try:
        result = subprocess.run(
            ["npm", "config", "get", key],
            cwd=repo_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or value in ("", "undefined", "null"):
        return None
    return value


def npm_publish_target(repo_path: str, env: dict, registry: str | None) -> str | None:
    """The host npm published to, or None when it can't be said for certain.

    npm takes the registry from package.json `publishConfig` first, then the package
    scope's `@scope:registry`, then `registry` (the argument, or .npmrc). Recording the
    generic registry for a scoped or publishConfig package named a host npm never used
    (audit F-04), so:
    - a `publishConfig` that sets a registry: None (its precedence over the argument is
      npm-version dependent);
    - a scoped package whose scope has a registry: that host, or None when an explicit
      `registry` argument competes with it;
    - otherwise the argument's host, or the generic `npm config get registry` host.
    Host only: a registry URL may carry userinfo. An ambiguous URL has no host (url_host).
    """
    data = _read_package_json(repo_path)
    if data is None:
        return None
    publish_config = data.get("publishConfig")
    if isinstance(publish_config, dict) and any(
        key == "registry" or key.endswith(":registry") for key in publish_config
    ):
        return None
    name = data.get("name")
    if isinstance(name, str) and name.startswith("@") and "/" in name:
        scope = name.split("/", 1)[0]
        scoped = _npm_config_get(repo_path, env, f"{scope}:registry")
        if scoped is not None:
            return None if registry else url_host(scoped)
    if registry:
        return url_host(registry)
    generic = _npm_config_get(repo_path, env, "registry")
    return url_host(generic) if generic else None


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
            dist_dir: Directory containing built distributions, relative to repo_path and
                inside it (default: dist).
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

            dist_path = validate_dist_dir(repo_path, dist_dir)
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
                    raise ValueError(f"Build failed: {scrub(build_result.stderr)[:500]}")

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
                raise ValueError(f"Twine check failed: {scrub(check_result.stderr)[:500]}")

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
                raise ValueError(f"Upload failed: {scrub(upload_result.stderr)[:500]}")

            package, version = dist_identity(dist_path)
            ac.effect(target=target, package=package, version=version)
            ac.finish("ok")
            return {
                "target": target,
                "dist_dir": dist_path,
                # Scrubbed before the cut: cutting first can split a credential.
                "stdout": scrub(upload_result.stdout)[:500],
            }
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
            registry: Registry URL, https:// with no credentials (default: whatever npm's
                configuration names, .npmrc included; the audit effect records its host).
            tag: Publish tag, 1-64 of [A-Za-z0-9._-] (default: 'latest').
        """
        ac = AuditCtx(
            "npm_publish", "registry", repo_path, {"repo_path": repo_path, "registry": registry}
        )
        try:
            repo_path = validate_write_path(repo_path)
            validate_registry(registry)
            validate_npm_tag(tag)
            config = get_config()
            if not config.npm_token:
                raise ValueError("NPM_TOKEN is not set")

            package_json = os.path.join(repo_path, "package.json")
            if not os.path.isfile(package_json):
                raise ValueError(f"package.json not found: {package_json}")

            # Check npm is available, on the PATH the child will get.
            if shutil.which("npm", path=clean_env().get("PATH")) is None:
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
                raise ValueError(f"npm publish failed: {scrub(result.stderr)[:500]}")

            package, version = npm_identity(repo_path)
            # The registry npm resolved for this package, as a host, or null (with
            # effect_incomplete) when publishConfig or a scope registry leaves it in doubt.
            ac.effect(
                target=npm_publish_target(repo_path, pub_env, registry),
                package=package,
                version=version,
            )
            ac.finish("ok")
            return {
                "stdout": scrub(result.stdout)[:500],
                "registry": registry or "npm's configured registry",
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

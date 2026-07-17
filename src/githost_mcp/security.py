"""Path allowlist validation and credential masking."""

from __future__ import annotations

import os
from pathlib import Path

from .config import get_config

# PM2 sets these variables in the process environment for its own IPC channel.
# If they leak into spawned children, any Node.js child (npm, twine's helper
# scripts run under `python -m build`, or any node CLI) inherits a stray file
# descriptor and SIGABRTs during process teardown — 100% reproducible via the
# shelling-out tool, 0% via a direct shell. Strip them before exec so shelled-out
# commands run in a clean environment. (HLOPS-1, GHOST-11)
_PM2_IPC_ENV_VARS = (
    "NODE_CHANNEL_FD",
    "NODE_CHANNEL_SERIALIZATION_MODE",
    "NODE_UNIQUE_ID",
)


def clean_env() -> dict:
    """Return a copy of the current environment with PM2 IPC vars stripped."""
    return {k: v for k, v in os.environ.items() if k not in _PM2_IPC_ENV_VARS}


def validate_write_path(repo_path: str) -> None:
    """Raise ValueError if repo_path is not under an allowed root."""
    config = get_config()
    if not config.allowed_repo_roots:
        raise ValueError(
            "Write operations are disabled: ALLOWED_REPO_ROOTS is not set. "
            "Set ALLOWED_REPO_ROOTS to a comma-separated list of allowed directories."
        )
    try:
        resolved = Path(repo_path).resolve()
    except Exception as e:
        raise ValueError(f"Invalid repo path: {e}") from None

    for root in config.allowed_repo_roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue

    raise ValueError(
        f"Path '{repo_path}' is not under any allowed root. Allowed: {config.allowed_repo_roots}"
    )


def validate_read_path(repo_path: str) -> None:
    """Raise ValueError if repo_path is not under an allowed root (read)."""
    config = get_config()
    if not config.allowed_repo_roots:
        raise ValueError(
            "Read operations are restricted: ALLOWED_REPO_ROOTS is not set. "
            "Set ALLOWED_REPO_ROOTS to a comma-separated list of allowed directories."
        )
    try:
        resolved = Path(repo_path).resolve()
    except Exception as e:
        raise ValueError(f"Invalid repo path: {e}") from None

    for root in config.allowed_repo_roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue

    raise ValueError(
        f"Path '{repo_path}' is not under any allowed root (read). "
        f"Allowed: {config.allowed_repo_roots}"
    )


def mask_credentials(text: str) -> str:
    """Replace known credential values with *** in text."""
    config = get_config()
    result = text
    for token in [
        config.github_token,
        config.gitea_token,
        config.gitlab_token,
        config.woodpecker_token,
        config.pypi_token,
        config.pypi_test_token,
        config.npm_token,
        config.audit_signing_key,
        config.auth_token,
    ]:
        if token and len(token) > 4:
            result = result.replace(token, "***")
    return result

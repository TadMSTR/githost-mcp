"""Path allowlist validation and credential masking."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import get_config

# Any URL userinfo component (`scheme://user:token@host`). mask_credentials() only
# replaces githost-mcp's *own configured* token values, so a credential a human
# embedded in a git remote by hand — a one-off PAT, say — survives that pass
# entirely. Remote URLs reach callers through GitPython's PushInfo.summary and
# exception text, so the userinfo is redacted by shape rather than by value.
# (SC-14, third recurrence; see githost-mcp-reliability-batch-2026-07 audit.)
#
# scp-style remotes (`git@github.com:owner/repo.git`) have no scheme and are left
# readable — that is the form every forge remote actually uses.
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")

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


def redact_url_credentials(text: str) -> str:
    """Strip the userinfo component from any scheme-qualified URL in text.

    Complements mask_credentials(), which can only redact tokens it already knows
    about from config. Use both on anything derived from git remote output.
    """
    return _URL_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}***@", text)


def scrub(text: str) -> str:
    """Full credential scrub for caller-facing strings: known tokens + URL userinfo."""
    return redact_url_credentials(mask_credentials(text))

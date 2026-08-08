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

# Phase 3 (workspace-policy plan, vikunja#349) adds validate_write_globs() and wires it
# into git_add/git_commit to enforce Config.write_globs/write_globs_deny. Until that
# lands, an agent whose policy grant carries a non-empty write_globs would otherwise get
# unrestricted write across its full allowed_write_roots — e.g. writer's two full
# container-root trees instead of the docs/samples paths the glob was meant to scope to
# (githost-workspace-policy-2026-08 audit, MEDIUM). Flip this to True in the same change
# that adds glob enforcement.
_GLOB_ENFORCEMENT_IMPLEMENTED = False


def clean_env() -> dict:
    """Return a copy of the current environment with PM2 IPC vars stripped."""
    return {k: v for k, v in os.environ.items() if k not in _PM2_IPC_ENV_VARS}


def _validate_path(
    repo_path: str, roots: list[str], *, verb: str, list_name: str, source: str
) -> None:
    if not roots:
        raise ValueError(
            f"{verb} operations are disabled: no {list_name} resolved (source: {source}). "
            "Set ALLOWED_REPO_ROOTS, or grant this agent via the workspace policy or manifest."
        )
    try:
        resolved = Path(repo_path).resolve()
    except Exception as e:
        raise ValueError(f"Invalid repo path: {e}") from None

    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue

    raise ValueError(
        f"Path '{repo_path}' is not under any allowed root ({list_name}, source: {source}). "
        f"Allowed: {roots}"
    )


def validate_write_path(repo_path: str) -> None:
    """Raise ValueError if repo_path is not under an allowed write root.

    Also fails closed if this agent's grant carries write_globs/write_globs_deny but
    the running code has no glob-enforcement path yet (_GLOB_ENFORCEMENT_IMPLEMENTED):
    without this, an unenforced glob is silently equivalent to unrestricted write
    across the full allowed_write_roots, not the narrower scope the glob promises.
    """
    config = get_config()
    if not _GLOB_ENFORCEMENT_IMPLEMENTED and (config.write_globs or config.write_globs_deny):
        raise ValueError(
            "Write operations are disabled: this agent's grant is scoped by write_globs, "
            "but glob enforcement is not implemented in this githost-mcp version. "
            "Refusing to grant unrestricted write across allowed_write_roots instead of "
            "silently ignoring the scope. (source: " + config.allowlist_source + ")"
        )
    _validate_path(
        repo_path,
        config.allowed_write_roots,
        verb="Write",
        list_name="allowed_write_roots",
        source=config.allowlist_source,
    )


def validate_read_path(repo_path: str) -> None:
    """Raise ValueError if repo_path is not under an allowed read root."""
    config = get_config()
    _validate_path(
        repo_path,
        config.allowed_read_roots,
        verb="Read",
        list_name="allowed_read_roots",
        source=config.allowlist_source,
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

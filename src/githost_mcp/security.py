"""Path allowlist validation and credential masking."""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
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

# Phase 3 (workspace-policy plan, vikunja#349) added validate_write_globs() and wired it
# into git_add/git_commit to enforce Config.write_globs/write_globs_deny. Before this
# landed, an agent whose policy grant carried a non-empty write_globs would otherwise have
# gotten unrestricted write across its full allowed_write_roots — e.g. writer's two full
# container-root trees instead of the docs/samples paths the glob was meant to scope to
# (githost-workspace-policy-2026-08 audit, MEDIUM). The guard below stays in place as a
# fail-closed backstop even now that enforcement exists — if this ever regresses back to
# False, write_globs-scoped agents get denied instead of silently widened.
_GLOB_ENFORCEMENT_IMPLEMENTED = True


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


class WriteGlobDenied(ValueError):
    """Raised by validate_write_globs() when a path fails write_globs allow/deny scope.

    A distinct type (rather than a bare ValueError) so callers can log/audit a policy
    denial differently from an unrelated failure — git_add/git_commit use this to write
    a `denied:write_glob` audit result instead of the generic `error:ValueError` other
    exceptions get, so the trail shows *why* the write failed, not just that it did.
    """

    def __init__(self, repo_path: str, denied_paths: list[str], source: str) -> None:
        self.denied_paths = denied_paths
        super().__init__(
            f"Write denied by policy write_globs scope for '{repo_path}': "
            f"{denied_paths} (source: {source})"
        )


def validate_write_globs(repo_path: str, paths: list[str]) -> None:
    """Raise WriteGlobDenied if any path fails this agent's write_globs allow/deny scope.

    Absence of both write_globs and write_globs_deny means unrestricted within the
    agent's write roots (e.g. sysadmin, developer) — this only narrows an agent that
    already carries a glob scope in its grant (e.g. writer). The deny list is
    evaluated after the allow list and wins: a path must match an allow pattern (when
    any are configured) and must not match a deny pattern.

    Patterns are plain fnmatch globs, not path-aware doublestar globs — `**/*.md`
    requires a literal `/` before the filename and will not match a bare top-level
    `README.md`. The workspace policy accounts for this by pairing `**/*.md` with
    separate `README*`/`CHANGELOG*` entries for root-level files.
    """
    config = get_config()
    allow = config.write_globs
    deny = config.write_globs_deny
    if not allow and not deny:
        return

    denied: list[str] = []
    for path in paths:
        normalized = path.replace(os.sep, "/")
        passes_allow = not allow or any(fnmatch(normalized, pattern) for pattern in allow)
        hits_deny = bool(deny) and any(fnmatch(normalized, pattern) for pattern in deny)
        if not passes_allow or hits_deny:
            denied.append(path)

    if denied:
        raise WriteGlobDenied(repo_path, denied, config.allowlist_source)


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

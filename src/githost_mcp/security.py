"""Path allowlist validation and credential masking."""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path

from .config import get_config
from .errors import (
    BranchNameInvalid,
    InvalidArgument,
    PathNotAllowed,
    RemoteNameInvalid,
    RemoteUrlRejected,
    WriteGlobDenied,
)

# Re-exported: these are raised here and caught by tools/, which has always imported
# them from this module. They live in errors.py now so audit.py can classify them
# without importing security.
__all__ = [
    "BranchNameInvalid",
    "InvalidArgument",
    "PathNotAllowed",
    "RemoteNameInvalid",
    "RemoteUrlRejected",
    "WriteGlobDenied",
    "clean_env",
    "mask_credentials",
    "redact_url_credentials",
    "scrub",
    "validate_branch_name",
    "validate_read_path",
    "validate_remote_name",
    "validate_remote_url",
    "validate_write_globs",
    "validate_write_path",
]

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
        raise PathNotAllowed(
            f"{verb} operations are disabled: no {list_name} resolved (source: {source}). "
            "Set ALLOWED_REPO_ROOTS, or grant this agent via the workspace policy or manifest."
        )
    try:
        resolved = Path(repo_path).resolve()
    except Exception as e:
        raise InvalidArgument(f"Invalid repo path: {e}") from None

    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return
        except ValueError:
            continue

    raise PathNotAllowed(
        f"Path '{repo_path}' is not under any allowed root ({list_name}, source: {source}). "
        f"Allowed: {roots}"
    )


def validate_write_path(repo_path: str) -> None:
    """Raise PathNotAllowed if repo_path is not under an allowed write root.

    Also fails closed if this agent's grant carries write_globs/write_globs_deny but
    the running code has no glob-enforcement path yet (_GLOB_ENFORCEMENT_IMPLEMENTED):
    without this, an unenforced glob is silently equivalent to unrestricted write
    across the full allowed_write_roots, not the narrower scope the glob promises.
    """
    config = get_config()
    if not _GLOB_ENFORCEMENT_IMPLEMENTED and (config.write_globs or config.write_globs_deny):
        raise PathNotAllowed(
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
    """Raise PathNotAllowed if repo_path is not under an allowed read root."""
    config = get_config()
    _validate_path(
        repo_path,
        config.allowed_read_roots,
        verb="Read",
        list_name="allowed_read_roots",
        source=config.allowlist_source,
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

    Each path is normalized with os.path.normpath() before matching, and any path
    whose normalized form is absolute or still starts with `..` is denied outright,
    independent of glob match. Without this, a traversal-shaped argument like
    `docs/../src/exploit.py` can textually match an allow glob such as `docs/**` —
    `fnmatch` has no path-segment awareness, `**` is just wildcards matching `..` and
    `/` like any other characters — while resolving, once handed to the real `git
    add`, to a location outside the intended scope entirely (githost-workspace-
    policy-2026-08 Phase 3 audit, MEDIUM).
    """
    config = get_config()
    allow = config.write_globs
    deny = config.write_globs_deny
    if not allow and not deny:
        return

    denied: list[str] = []
    for path in paths:
        normalized = os.path.normpath(path.replace(os.sep, "/"))
        if os.path.isabs(normalized) or normalized == ".." or normalized.startswith("../"):
            denied.append(path)
            continue
        passes_allow = not allow or any(fnmatch(normalized, pattern) for pattern in allow)
        hits_deny = bool(deny) and any(fnmatch(normalized, pattern) for pattern in deny)
        if not passes_allow or hits_deny:
            denied.append(path)

    if denied:
        raise WriteGlobDenied(repo_path, denied, config.allowlist_source)


# Remote-URL validation for git_remote. A remote URL is not inert data: git will
# hand it to a remote helper on the next fetch, and `ext::` in particular runs an
# arbitrary shell command (`git remote add x "ext::sh -c '…'"` executes on fetch).
# Restricting to the transports forge actually uses keeps a write tool that exists
# to be the audited path from becoming a command-execution primitive.
_ALLOWED_REMOTE_SCHEMES = ("http://", "https://", "ssh://", "git://")
# Schemes where a bare userinfo is a login name rather than a secret. Over http(s)
# a bare userinfo is how a PAT is normally embedded (`https://<token>@github.com/…`),
# so there it is refused outright; over ssh/git `git@` is just the SSH user.
_LOGIN_USERINFO_SCHEMES = ("ssh://", "git://")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
# scp-style: [user@]host:path. `git@github.com:owner/repo.git` is the form every
# forge remote uses, so it has to keep working — but only without a password.
#
# The path must not begin with '/' or ':'. The ':' exclusion is what separates this
# from git's remote-helper syntax: `ext::sh -c '…'` is otherwise shaped exactly like
# host:path, and would match here and be stored as a remote that runs a shell
# command on the next fetch.
_SCP_RE = re.compile(r"^(?P<userinfo>[^/@]+@)?[^/@:]+:(?![/:]).+$")
_WHITESPACE_RE = re.compile(r"\s")
# A leading '-' makes git parse the value as an option rather than a name/URL.
_LEADING_DASH_ERR = "must not start with '-'"
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_remote_name(name: str) -> None:
    """Raise RemoteNameInvalid unless `name` is a plain remote name."""
    if not name:
        raise RemoteNameInvalid("remote name is required")
    if name.startswith("-"):
        raise RemoteNameInvalid(f"remote name {_LEADING_DASH_ERR}")
    if not _REMOTE_NAME_RE.match(name):
        raise RemoteNameInvalid(
            f"Invalid remote name '{name}': use letters, digits, '.', '_', '-', '/'"
        )


def validate_branch_name(name: str) -> None:
    """Raise BranchNameInvalid unless `name` is safe to interpolate into a push refspec.

    ``git_branch_delete_remote`` builds ``":refs/heads/" + name``. GitPython passes
    that as one argv element, so this is not a shell-injection boundary — but a
    refspec is ``src:dst``, so an embedded colon silently re-points the delete at a
    ref the caller never named, and the leading ``:`` means git parses the whole
    thing rather than rejecting it. Deleting the wrong remote ref is not a failure
    the caller can notice from a success result.

    The checks are git's own refname rules (``git check-ref-format``) restricted to
    the ones that matter for a destructive call, expressed as a denylist rather than
    a character allowlist so legitimate names like ``fix/#383`` or
    ``release/v1.2.0+build`` are not rejected for no reason.
    """
    if not name:
        raise BranchNameInvalid("branch name is required")
    if name.startswith("-"):
        raise BranchNameInvalid(f"branch name {_LEADING_DASH_ERR}")
    if ":" in name:
        raise BranchNameInvalid(
            f"Invalid branch name '{name}': ':' would split the push refspec and "
            f"retarget the deletion at a different ref"
        )
    if _WHITESPACE_RE.search(name):
        raise BranchNameInvalid(f"Invalid branch name '{name}': must not contain whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise BranchNameInvalid(
            f"Invalid branch name '{name!r}': must not contain control characters"
        )
    for bad in ("*", "?", "[", "~", "^", "\\"):
        if bad in name:
            raise BranchNameInvalid(f"Invalid branch name '{name}': must not contain '{bad}'")
    if ".." in name or "@{" in name:
        raise BranchNameInvalid(f"Invalid branch name '{name}': must not contain '..' or '@{{'")
    if name.endswith(".lock") or name.endswith("/") or name.endswith("."):
        raise BranchNameInvalid(
            f"Invalid branch name '{name}': must not end with '.lock', '/' or '.'"
        )
    if name.startswith("/") or "//" in name:
        raise BranchNameInvalid(
            f"Invalid branch name '{name}': must not start with '/' or contain '//'"
        )


def validate_remote_url(url: str) -> None:
    """Raise RemoteUrlRejected unless `url` is a credential-free supported remote URL.

    Credentials are **refused, not redacted**. redact_url_credentials() exists for
    text on its way out to a caller; this is text on its way into `.git/config`,
    where a token would persist on disk for every later fetch and push, outlive the
    call that supplied it, and be readable by anything that can read the repo.
    Redacting it there would silently store a broken remote instead.

    Any userinfo at all is refused for scheme-qualified URLs, not just the
    `user:token@` form — `https://<token>@github.com/owner/repo` is exactly how a
    PAT is normally embedded, and it carries no colon. scp-style remotes keep their
    bare `git@host:path` username, which is a username and not a secret, but are
    refused if they carry a `user:password@`.
    """
    if not url:
        raise RemoteUrlRejected("remote url is required")
    if url.startswith("-"):
        raise RemoteUrlRejected(f"remote url {_LEADING_DASH_ERR}")
    if _WHITESPACE_RE.search(url):
        raise RemoteUrlRejected("Remote URL must not contain whitespace")

    if _SCHEME_RE.match(url):
        if not url.startswith(_ALLOWED_REMOTE_SCHEMES):
            scheme = url.split("://", 1)[0]
            raise RemoteUrlRejected(
                f"Unsupported remote URL scheme '{scheme}://'. "
                f"Allowed: {', '.join(_ALLOWED_REMOTE_SCHEMES)}, or scp-style user@host:path."
            )
        rest = url.split("://", 1)[1]
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            userinfo = authority.rsplit("@", 1)[0]
            if not url.startswith(_LOGIN_USERINFO_SCHEMES):
                raise RemoteUrlRejected(
                    "Remote URL embeds credentials in its userinfo component. Refused rather "
                    "than redacted: it would persist in .git/config. Use an scp-style SSH "
                    "remote, or a credential helper."
                )
            if ":" in userinfo:
                raise RemoteUrlRejected(
                    "Remote URL embeds a password in its user component. Refused rather than "
                    "redacted: it would persist in .git/config."
                )
        return

    if scp_match := _SCP_RE.match(url):
        if ":" in (scp_match.group("userinfo") or ""):
            raise RemoteUrlRejected(
                "Remote URL embeds a password in its user component. Refused rather than "
                "redacted: it would persist in .git/config."
            )
        return

    # Everything else — including bare local paths and `ext::`/`fd::` remote
    # helpers, which run commands on fetch.
    raise RemoteUrlRejected(
        f"Unsupported remote URL form: '{url}'. Use {', '.join(_ALLOWED_REMOTE_SCHEMES)}, "
        "or scp-style user@host:path."
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


# SECURITY[deferred]: a credential this process does not hold in its own config, appearing
# OUTSIDE a URL, survives both passes — `Authorization: Bearer <unknown-tok>` comes through
# intact. mask_credentials() only replaces values it knows; redact_url_credentials() only
# matches `scheme://userinfo@`. Measured 2026-09-19 against forge's real secrets: 7
# adversarial cases in the shapes git/GitPython actually emit, 0 leaks — the gap is narrow
# but real. The exposure shape is pre-existing (this function's output already reaches
# callers at ~40 sites in tools/); what changed is that audit.py now also writes it to a
# durable HMAC-signed log.
#
# Deferred by Ted 2026-09-19 rather than patched here: scrub() is on every tool return
# path, so an over-broad shape-based redactor silently mangles legitimate error text —
# a failure mode harder to notice than the leak it fixes. Needs its own build with a
# fixture corpus asserting both directions.
#
# Target: vikunja#913 (id 996). Audit: 2026-09-19/agent-build-workflow-2026-09-p1-githost-telemetry.
def scrub(text: str) -> str:
    """Full credential scrub for caller-facing strings: known tokens + URL userinfo.

    Note the limit documented in the SECURITY[deferred] comment above: an unknown
    credential outside a URL is not redacted. Callers writing this output to a durable
    sink should be aware of that, and must scrub before any truncation — cutting first
    can split a token and leave an unmatched prefix.
    """
    return redact_url_credentials(mask_credentials(text))

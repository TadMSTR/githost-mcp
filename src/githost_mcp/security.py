"""Path allowlist validation and credential masking."""

from __future__ import annotations

import os
import re
from fnmatch import fnmatch
from pathlib import Path

from .config import get_config, root_as_written
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
    "canonical_path",
    "clean_env",
    "mask_credentials",
    "redact_credential_shapes",
    "redact_url_credentials",
    "scrub",
    "split_authority",
    "url_host",
    "validate_branch_name",
    "validate_read_path",
    "validate_ref",
    "validate_remote_name",
    "validate_remote_url",
    "validate_tag_name",
    "validate_write_globs",
    "validate_write_path",
]

# URL authority parsing: one rule for validate_remote_url, redact_url_credentials,
# url_host and the npm registry target (#1434 F-02).
#
# The userinfo runs to the LAST `@` of the authority. A credential can also sit in front of
# a `/`, `?` or `#`, as in `https://<token>/x@host/o/r.git`: a first-`/` parser saw no `@`
# in the authority, so the validator accepted it, the redactor kept it, and url_host
# returned the token as the host. That shape is read as userinfo when the part before the
# first `/`, `?` or `#` is not host-shaped and the part after the last `@` is. A host-shaped
# head (a dotted name, an IP, `localhost`, or `label:port`) keeps ordinary URLs with an `@`
# in the path intact: npm's `https://registry.npmjs.org/@scope%2fpkg`.
#
# Residual, stated: a dotted credential (a JWT) in front of `/…@host` reads as a host.
#
# scp-style remotes (`git@github.com:owner/repo.git`) have no scheme and are left
# readable: that is the form every forge remote actually uses.
_HOSTLIKE_RE = re.compile(
    r"(?:localhost|\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+"
    r"|[A-Za-z0-9-]{1,63}:\d{1,5})(?::\d{1,5})?"
)
_HOST_AFTER_AT_RE = re.compile(
    r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*)(?::\d{1,5})?"
)
_PATH_START_RE = re.compile(r"[/?#]")


def split_authority(after_scheme: str) -> tuple[str | None, str, str]:
    """Split the text after `scheme://` into (userinfo or None, host[:port], rest)."""
    m = _PATH_START_RE.search(after_scheme)
    first = m.start() if m else len(after_scheme)
    head = after_scheme[:first]
    if "@" in head:
        userinfo, hostport = head.rsplit("@", 1)
        return userinfo, hostport, after_scheme[first:]
    at = after_scheme.rfind("@")
    if head and at > first and not _HOSTLIKE_RE.fullmatch(head):
        tail = after_scheme[at + 1 :]
        m = _PATH_START_RE.search(tail)
        end = m.start() if m else len(tail)
        if _HOST_AFTER_AT_RE.fullmatch(tail[:end]):
            return after_scheme[:at], tail[:end], tail[end:]
    return None, head, after_scheme[first:]


# The start of a scheme-qualified URL in free text. Linear (audit F-02,
# agent-build-workflow-2026-09 p4): the lookbehind lets an attempt start only at the
# beginning of a run of scheme characters, and every quantifier is possessive, so a long
# run is scanned once. `pre` keeps leading digits/`+.-` so `1https://tok@h` is still found.
# Each URL runs to whitespace, a quote, or the next URL's start, whichever comes first, so
# a URL nested in another's query or joined to it by a comma is redacted on its own.
_URL_START_RE = re.compile(
    r"(?<![a-zA-Z0-9+.\-])(?P<pre>[0-9+.\-]*+)(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*+://)"
)
_URL_END_RE = re.compile(r"[\s'\"<>]")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def url_host(url: str) -> str | None:
    """The host of a URL, never the URL: `https://u:p@host/x` and `git@host:x` → host.

    Total: it runs after a side effect (git_remote add, git_clone, npm publish), so it never
    raises. A host that is not host-shaped is None.
    """
    try:
        if _SCHEME_RE.match(url):
            _userinfo, hostport, _rest = split_authority(url.split("://", 1)[1])
            host = hostport
            if host.startswith("["):
                host = host[1 : host.find("]")] if "]" in host else ""
            else:
                host = host.rsplit(":", 1)[0] if ":" in host else host
        else:
            host = url.split("@", 1)[-1].split(":", 1)[0]
    except Exception:
        return None
    return host if host and _HOST_RE.fullmatch(host) else None


_HOST_RE = re.compile(r"[A-Za-z0-9.:-]{1,253}")

# The environment a publish child (python -m build, twine, npm) gets: these names and
# nothing else (vikunja#610). `python -m build` runs the repository's own build backend and
# `npm publish` its lifecycle scripts, so before 0.21.0 repo code received githost's whole
# environment: GITHUB_TOKEN, GITEA_TOKEN, WOODPECKER_TOKEN, AUDIT_SIGNING_KEY and
# GITHOST_MCP_AUTH_TOKEN. Each call site adds the one credential its child needs
# (TWINE_PASSWORD, NPM_TOKEN) explicitly. TMPDIR is kept because build and npm write
# their scratch trees under it. No shadow mode: tests run a real `python -m build` and a
# real `npm pack` under it.
#
# PM2's IPC variables (NODE_CHANNEL_FD and friends) are not on the list, so they stay out
# by construction: a Node child that inherits them gets a stray fd and SIGABRTs at
# teardown (HLOPS-1, GHOST-11).
CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "TZ",
    "TMPDIR",
    # The POSIX locale categories, by name: a prefix match would admit any LC_-named
    # variable a launcher happened to set.
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
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
    """The environment for a publish child: CHILD_ENV_ALLOWLIST's names only."""
    return {k: os.environ[k] for k in CHILD_ENV_ALLOWLIST if k in os.environ}


def canonical_path(repo_path: str) -> Path:
    """The path a caller's `repo_path` names: `~` expanded, then resolved.

    `Path("~/x").resolve()` does not expand `~` — it resolves to `<cwd>/~/x`, so a
    literal `~/repos/...` was refused as outside every root (sysadmin, 2026-10-06).
    Expanding here is only half the fix: the git operation must run on this same
    value, not on the caller's literal string, or validation and execution disagree
    about which directory they mean and git creates a directory named `~` under the
    cwd. The validators below therefore return it, and every tool runs on the return.
    """
    try:
        return Path(repo_path).expanduser().resolve()
    except Exception as e:
        raise InvalidArgument(f"Invalid repo path: {e}") from None


def _validate_path(
    repo_path: str, roots: list[str], *, verb: str, list_name: str, source: str
) -> str:
    if not roots:
        raise PathNotAllowed(
            f"{verb} operations are disabled: no {list_name} resolved (source: {source}). "
            "Set ALLOWED_REPO_ROOTS, or grant this agent via the workspace policy or manifest."
        )
    resolved = canonical_path(repo_path)

    # Each root is matched as written, and only the requested path is resolved
    # (vikunja#996): resolving the root here let a root swapped for a symlink carry the
    # grant to the link's target. A root that is a symlink now (swapped after load, when
    # config refused any that already were) is refused.
    refused: list[str] = []
    for root in roots:
        written = root_as_written(root)
        if os.path.islink(written):
            refused.append(written)
            continue
        try:
            resolved.relative_to(written)
            return str(resolved)
        except ValueError:
            continue

    note = f"; refused because each is a symlink: {refused}" if refused else ""
    raise PathNotAllowed(
        f"Path '{repo_path}' is not under any allowed root ({list_name}, source: {source}). "
        f"Allowed: {roots}{note}"
    )


def validate_write_path(repo_path: str) -> str:
    """Raise PathNotAllowed if repo_path is not under an allowed write root.

    Returns the canonical path that was checked. Run the operation on that value,
    never on the caller's original string (see canonical_path()).

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
    return _validate_path(
        repo_path,
        config.allowed_write_roots,
        verb="Write",
        list_name="allowed_write_roots",
        source=config.allowlist_source,
    )


def validate_read_path(repo_path: str) -> str:
    """Raise PathNotAllowed if repo_path is not under an allowed read root.

    Returns the canonical path that was checked; run the operation on it.
    """
    config = get_config()
    return _validate_path(
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
# scp-style: [user@]host:path. `git@github.com:owner/repo.git` is the form every
# forge remote uses, so it has to keep working — but only without a password.
#
# The path must not begin with '/' or ':'. The ':' exclusion is what separates this
# from git's remote-helper syntax: `ext::sh -c '…'` is otherwise shaped exactly like
# host:path, and would match here and be stored as a remote that runs a shell
# command on the next fetch.
_SCP_RE = re.compile(r"\A(?P<userinfo>[^/@]+@)?[^/@:]+:(?![/:]).+\Z")
_WHITESPACE_RE = re.compile(r"\s")
# A leading '-' makes git parse the value as an option rather than a name/URL.
_LEADING_DASH_ERR = "must not start with '-'"
_REMOTE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


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


def _check_refname(name: str, kind: str, exc: type[ValueError]) -> None:
    """Raise `exc` unless `name` is safe to hand to a push as a refspec.

    A refspec is ``[+]src[:dst]``. A leading ``+`` force-updates the destination, a
    leading ``:`` (empty source) deletes it, and an embedded colon pushes ``src`` to
    a ref the caller never named. GitPython passes the value as one argv element, so
    this is not a shell-injection boundary — it is the boundary between the ungated
    push verbs and the HITL-gated ones (``git_branch_delete_remote``,
    ``git_push_force_with_lease``), which a raw refspec would otherwise reach.

    The checks are git's own refname rules (``git check-ref-format``) restricted to
    the ones that matter for a push, expressed as a denylist rather than a character
    allowlist so legitimate names like ``fix/#383`` or ``release/v1.2.0+build`` are
    not rejected for no reason.
    """
    if not name:
        raise exc(f"{kind} name is required")
    if name.startswith("-"):
        raise exc(f"{kind} name {_LEADING_DASH_ERR}")
    if name.startswith("refs/") or name in ("HEAD", "@"):
        raise exc(
            f"Invalid {kind} name '{name}': give the plain {kind} name, not a full ref or "
            f"HEAD; the push pins the namespace itself"
        )
    if name.startswith("+"):
        raise exc(
            f"Invalid {kind} name '{name}': a leading '+' makes the push refspec a force update"
        )
    if ":" in name:
        raise exc(
            f"Invalid {kind} name '{name}': ':' would split the push refspec and "
            f"retarget it at a different ref (or delete one)"
        )
    if _WHITESPACE_RE.search(name):
        raise exc(f"Invalid {kind} name '{name}': must not contain whitespace")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise exc(f"Invalid {kind} name '{name!r}': must not contain control characters")
    for bad in ("*", "?", "[", "~", "^", "\\"):
        if bad in name:
            raise exc(f"Invalid {kind} name '{name}': must not contain '{bad}'")
    if ".." in name or "@{" in name:
        raise exc(f"Invalid {kind} name '{name}': must not contain '..' or '@{{'")
    if name.endswith(".lock") or name.endswith("/") or name.endswith("."):
        raise exc(f"Invalid {kind} name '{name}': must not end with '.lock', '/' or '.'")
    if name.startswith("/") or "//" in name:
        raise exc(f"Invalid {kind} name '{name}': must not start with '/' or contain '//'")


def validate_branch_name(name: str) -> None:
    """Raise BranchNameInvalid unless `name` is a plain branch name, safe as a refspec.

    Every caller-supplied branch that reaches a push goes through here: ``git_push``,
    ``git_branch_delete_remote`` (which builds ``":refs/heads/" + name``) and
    ``git_push_force_with_lease``. See `_check_refname` for the rules.
    """
    _check_refname(name, "branch", BranchNameInvalid)


def validate_tag_name(name: str) -> None:
    """Raise InvalidArgument unless `name` is a plain tag name, safe as a refspec.

    ``git_tag(push=True)`` and ``release`` push the tag by name, so a ``+`` prefix
    would force-move an existing remote tag (and re-run its tag-triggered publish).
    """
    _check_refname(name, "tag", InvalidArgument)


# A commit ref for the CI status reads: a SHA, or a branch/tag name. It lands in a URL path
# (Gitea, through quote(ref, safe=""); PyGithub's get_commit quotes it the same way), so it
# is one encoded segment either way. The rules follow `git check-ref-format` where it
# matters here (vikunja#1429): a leading `_` and a `#` are valid branch names, so they are
# accepted; `..`, a leading `-`, a component starting with `.` (which covers `.` and `..`
# segments), an empty segment, a trailing `/`, whitespace, control characters and git's
# own forbidden characters are refused.
_REF_RE = re.compile(r"\A[A-Za-z0-9_#][A-Za-z0-9._/+#-]*\Z")
_REF_MAX = 255


def validate_ref(ref: str) -> None:
    """Raise InvalidArgument unless `ref` is a SHA or a plain branch/tag name."""
    if not ref or len(ref) > _REF_MAX:
        raise InvalidArgument(f"ref must be 1-{_REF_MAX} characters")
    if ref.startswith("-"):
        raise InvalidArgument(f"ref {_LEADING_DASH_ERR}")
    if (
        ".." in ref
        or not _REF_RE.match(ref)
        or ref.endswith("/")
        or "//" in ref
        or "/." in ref
        or ref.endswith(".lock")
    ):
        raise InvalidArgument(
            "ref must be a SHA or a branch/tag name (letters, digits, '.', '_', '#', '/', "
            "'+', '-'; no '..', no leading '-', no empty or dot-led path segment)"
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
        userinfo, _hostport, _rest = split_authority(url.split("://", 1)[1])
        if userinfo is not None:
            if not url.startswith(_LOGIN_USERINFO_SCHEMES) or any(c in userinfo for c in "/?#"):
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
    starts = list(_URL_START_RE.finditer(text))
    out: list[str] = []
    pos = 0
    for i, m in enumerate(starts):
        body = m.end()
        limit = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        stop = _URL_END_RE.search(text, body, limit)
        end = stop.start() if stop else limit
        userinfo, hostport, rest = split_authority(text[body:end])
        if userinfo:  # None, or an empty `scheme://@host`: nothing to hide
            out.append(f"{text[pos:body]}***@{hostport}{rest}")
            pos = end
    out.append(text[pos:])
    return "".join(out)


# Credentials this process does not hold in its own config, redacted by shape (vikunja#913).
# mask_credentials() only replaces values it knows and redact_url_credentials() only matches
# `scheme://userinfo@`, so `Authorization: Bearer <unknown>` came through both, and so did
# a hook's stderr printing `GITHUB_TOKEN=ghp_…` (both captured for real in
# tests/fixtures/scrub_corpus.json). scrub() is on every tool return path, so the shapes
# are narrow, and the corpus test asserts both directions: every planted credential is
# redacted and every ordinary message comes back byte-identical.
#
# The value after an Authorization scheme word. Linear: a fixed literal, then one
# character class with no overlap with what precedes it.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token|basic)\s+)[^\s'\"<>,;]+"
)
# Vendor-prefixed tokens, keeping the prefix (it says which credential leaked). Each length
# floor sits below the vendor's real token length and above ordinary identifiers that share
# a prefix (`npm_config_cache`, `pypi-publish`).
_VENDOR_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:"
    r"(?P<gh>gh[pousr]_)[A-Za-z0-9]{30,}"
    r"|(?P<ghpat>github_pat_)[A-Za-z0-9_]{40,}"
    r"|(?P<glpat>glpat-)[A-Za-z0-9_-]{20,}"
    r"|(?P<slack>xox[bp]-)[A-Za-z0-9-]{10,}"
    r"|(?P<npm>npm_)[A-Za-z0-9]{30,}"
    r"|(?P<pypi>pypi-)[A-Za-z0-9_-]{40,}"
    r")"
)


def redact_credential_shapes(text: str) -> str:
    """Redact an Authorization header value and vendor-prefixed tokens by shape."""
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}***", text)
    return _VENDOR_TOKEN_RE.sub(lambda m: f"{m.group(m.lastgroup)}***", text)


def scrub(text: str) -> str:
    """Full credential scrub for caller-facing strings.

    Known tokens (mask_credentials), URL userinfo, then credential shapes (an
    Authorization header value, vendor-prefixed tokens). A credential with none of those
    shapes, held nowhere in this process's config, still passes. Callers writing this
    output to a durable sink must scrub before any truncation: cutting first can split a
    token and leave an unmatched prefix.
    """
    return redact_credential_shapes(redact_url_credentials(mask_credentials(text)))

"""Repo bootstrap guards: names, ownership, clone URL and destination, the clone itself.

Shared by git_clone and the `*_repo_create` / `*_repo_get` tools. Everything here is an
undecorated helper so mutation testing reaches it (mutmut generates no mutants inside an
`@mcp.tool` body).

The shape of git_clone is decided here and is deliberate (Ted, 2026-10-25,
agent-build-workflow-2026-09 part 4b):

- **No caller URL.** The URL is built from `provider/owner/repo`, so the `ext::`/`fd::`
  remote-helper surface vikunja#764 worried about is removed rather than filtered.
  validate_remote_url() still runs on the built URL, as defence in depth.
- **No caller destination.** The path follows forge's checkout convention, measured from
  40 existing checkouts' `origin`, and is still checked against the agent's write roots.
- **Owned repos only.** Only repos whose owner forge controls: GitHub owners in
  `forge_owned_owners`, organizations on our Gitea. A fork of an upstream is one of those
  and is deliberately cloneable (fork, then clone the fork); cloning the upstream itself
  stays an operator step.
"""

from __future__ import annotations

import os
import re
import unicodedata
from fnmatch import fnmatch

import git

from .errors import InvalidArgument, MissingArgument, NotOwned, WriteGlobDenied
from .identity import _parse_remote, _repo_full_name
from .security import canonical_path, redact_url_credentials

CLONE_PROVIDERS = frozenset({"github", "gitea"})
VISIBILITIES = frozenset({"public", "private"})

# Where clones land. `~` is expanded per call, for the broker's own uid: when
# agent-isolation gives agents their own uids, each lands under its own home rather than
# a hardcoded /home/ted. A location, not a grant — validate_write_path() still decides.
GITHUB_CLONE_ROOT = "~/repos/personal"
GITEA_CLONE_ROOT = "~/repos/gitea"

# Measured 2026-10-25: the largest owned repos (TadMSTR/memsearch, 34 MB checked out;
# TadMSTR/claudecodeui, 25 MB; host-forge/scripts on Gitea) clone in 0.7-1.3 s from
# forge. 120 s is ~90x that: room for a slow network, short of an unbounded hang.
CLONE_TIMEOUT_S = 120

# GitHub and Gitea both allow [A-Za-z0-9_.-] in owner and repo names. `.github` is a
# real repo (#764's second case), so a leading dot is allowed; "." and ".." are not.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_MAX_DESCRIPTION_CHARS = 350
_LINE_BREAKING_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_SYMLINK_MODE = "120000"


def validate_segment(argument: str, value: str | None) -> str:
    """Return `value` if it is one safe owner or repo name segment.

    Refuses "." and "..": these are interpolated into Gitea API paths, and httpx resolves
    dot segments, so `owner=".."` would address a different endpoint. A `.git` suffix is
    refused too: the clone URL appends one, and GitHub renames such a repo on create.
    """
    if not value:
        raise MissingArgument(f"{argument} is required")
    if value in (".", "..") or value.startswith("-") or not _SEGMENT_RE.fullmatch(value):
        raise InvalidArgument(
            f"Invalid {argument} {value!r}: use 1-100 of [A-Za-z0-9_.-], not starting "
            "with '-', and not '.' or '..'"
        )
    if value.lower().endswith(".git"):
        raise InvalidArgument(f"Invalid {argument} {value!r}: drop the '.git' suffix")
    return value


def first_bad_segment(
    pairs: tuple[tuple[str, str | None], ...],
) -> tuple[str, str, str | None, str] | None:
    """`(reason_class, argument, value, message)` for the first invalid segment, else None.

    For the pre-flight guards, which record a rejection with audit_rejection() before
    the tool has built its AuditCtx.
    """
    for argument, value in pairs:
        try:
            validate_segment(argument, value)
        except MissingArgument as e:
            return "missing_argument", argument, value, str(e)
        except InvalidArgument as e:
            return "invalid_argument", argument, value, str(e)
    return None


def validate_description(description: str | None) -> str | None:
    """GitHub caps a repo description at 350 characters; refuse longer before the call.

    Control characters are refused too (audit F-05). The HITL notification renders each
    argument on its own line, unescaped, so a newline here could print a second, forged
    `visibility:` line beside the real one, and visibility is the decision being approved.
    """
    if description is None:
        return None
    # Unicode categories, not an ASCII range (round 2, R2-05): Cc covers C0, DEL and C1
    # (U+0085 NEL among them), and Zl/Zp are U+2028/U+2029. All are line breaks to some
    # renderer.
    if any(unicodedata.category(ch) in _LINE_BREAKING_CATEGORIES for ch in description):
        raise InvalidArgument("description must not contain control characters or newlines")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise InvalidArgument(
            f"description is {len(description)} characters; the limit is {_MAX_DESCRIPTION_CHARS}"
        )
    return description


def readback_warning(
    provider: str, requested: str, reported: str | None, readback_error: str | None
) -> str | None:
    """The warning a create result carries, or None when the read-back confirms the request.

    A failed read-back is reported as a warning on a successful result, never as an error
    (CR-03/CR-05): the repo exists by then, so an error would misreport a create that is
    not idempotent, and a retry would only be refused as "already exists".
    """
    if readback_error is not None:
        return (
            f"created, but reading it back failed ({readback_error}); visibility is "
            "unconfirmed. Check the repo before pushing anything to it."
        )
    if reported is None:
        return (
            f"created, but {provider} does not show it yet; visibility is unconfirmed. "
            "Check the repo before pushing anything to it."
        )
    if reported != requested:
        return (
            f"requested {requested}, {provider} reports {reported}. "
            "Check the repo before pushing anything to it."
        )
    return None


def readback_result(reported: str | None, warning: str | None) -> str:
    """The audit `result` for a create: `ok`, or an `ok:*` naming what is unconfirmed.

    The create succeeded either way, so these stay successes (audit.py counts `ok*` as
    such). But "asked for private, the provider says public" must be visible in the audit
    trail, not only in the result the calling agent receives (round 2, R2-03).
    """
    if warning is None:
        return "ok"
    return "ok:visibility_unconfirmed" if reported is None else "ok:visibility_mismatch"


def tree_check_paths(ls_files_stage: str) -> dict[str, str]:
    """Paths to test against the deny globs, mapped to how a refusal should name them.

    Parses `git ls-files -s -z`. A tracked symlink is also tested as `<path>/x`: a symlink
    named `manifests` pointing at `evil/` lands `manifests/a.yml` on disk while no tracked
    path matches `manifests/**` (round 2, R2-01).
    """
    paths: dict[str, str] = {}
    for entry in ls_files_stage.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        paths[path] = path
        if meta.split(" ", 1)[0] == _SYMLINK_MODE:
            paths[f"{path}/x"] = f"{path} (symlink)"
    return paths


def symlinks_above_denied(ls_files_stage: str, write_globs_deny: list[str], root: str) -> list[str]:
    """Tracked symlinks that sit above a denied area, at any depth (round 3, R3-01).

    tree_check_paths() tests a symlink `S` as `S/x`, which catches `S/*` and `S/**` but not
    a deeper glob: with `config -> evil/` and a deny glob `config/secrets/*`, `evil/secrets/a`
    lands at `config/secrets/a` while no tested path matches. So `S` is refused when some
    deny glob has more segments than `S`, and its first segments match `S` segment by
    segment (fnmatch per segment, so `a/*/secrets/*` covers a symlink `a/foo`).

    A symlink that resolves to a regular file inside the checkout cannot hold anything
    beneath it and is skipped. That keeps `AGENT.md -> CLAUDE.md`, a common way to serve
    one instructions file to several agents, cloneable even under a `**/...` deny.
    """
    above: list[str] = []
    for entry in ls_files_stage.split("\0"):
        meta, _, path = entry.partition("\t")
        if not entry or meta.split(" ", 1)[0] != _SYMLINK_MODE:
            continue
        if os.path.isfile(os.path.join(root, path)):
            continue
        parts = os.path.normpath(path).split("/")
        for pattern in write_globs_deny:
            segments = pattern.split("/")
            if len(segments) > len(parts) and all(
                fnmatch(part, seg) for part, seg in zip(parts, segments, strict=False)
            ):
                above.append(f"{path} (symlink)")
                break
    return above


def validate_depth(depth: int | None) -> int | None:
    """A shallow-clone depth must be a positive int. `True` is an int in Python; refuse it."""
    if depth is None:
        return None
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise InvalidArgument(f"depth must be a positive integer, got {depth!r}")
    return depth


def ensure_owned(provider: str, owner: str, forge_owners: list[str], gitea_host: str) -> None:
    """Raise NotOwned unless `owner` is one forge controls on `provider`.

    GitHub: case-insensitive membership of `forge_owned_owners`, the same comparison
    identity.py uses. Gitea: only if an instance is configured — an unset GITEA_URL is not
    a licence to clone from anywhere. That `owner` is an organization there (not a user
    namespace, which could hold a mirror of third-party code) needs an API call, so it is
    checked separately by ensure_gitea_org().
    """
    if provider == "github":
        if owner.lower() not in {o.lower() for o in forge_owners}:
            raise NotOwned(
                f"git_clone only clones repos forge owns; '{owner}' is not in "
                f"forge_owned_owners ({', '.join(forge_owners)}). Cloning a third-party "
                "repo is an operator step. To work on one, github_fork it and clone the fork."
            )
        return
    if not gitea_host:
        raise NotOwned("GITEA_URL is not set, so no Gitea owner can be recognised as ours")


def ensure_gitea_org(owner: str, is_org: bool) -> None:
    """Raise NotOwned unless `owner` is an organization on our Gitea (audit F-04)."""
    if not is_org:
        raise NotOwned(
            f"'{owner}' is not an organization on our Gitea (or is not visible to the token). "
            "git_clone clones Gitea repos in organizations only; a user namespace is an "
            "operator step."
        )


def ensure_no_branch_for_denied_paths(branch: str | None, write_globs_deny: list[str]) -> None:
    """Refuse `branch=` for an agent whose grant denies some paths (audit F-03).

    A clone writes every path of the tree, and `branch` can name any branch on the remote,
    reviewed or not. For an agent that may not write, say, `manifests/**`, that would make
    an unmerged branch's denied files land on disk under its roots. The default branch is
    still cloneable, and its tree is then checked by ensure_no_denied_paths().
    """
    if branch is not None and write_globs_deny:
        raise WriteGlobDenied(
            f"branch={branch!r}",
            ["(non-default branch)"],
            "this agent's grant carries write_globs_deny; clone the default branch",
        )


def ensure_whole_tree_writer(dest: str, write_globs: list[str], source: str) -> None:
    """Refuse a clone for an agent whose grant is scoped by `write_globs`.

    A clone writes a whole tree. An agent allowed only `README*` and `**/*.md` (writer)
    would otherwise get every file of the repo written under its roots in one call. Ted
    denied writer the tool at the manifest (2026-10-25); this is the same rule in code.
    A deny-only scope (developer's `manifests/**`) narrows nothing a clone writes on
    purpose, so only an allow scope refuses.
    """
    if write_globs:
        raise WriteGlobDenied(dest, ["(entire clone)"], source)


def clone_url(provider: str, owner: str, repo: str, gitea_host: str) -> str:
    """The scp-form SSH URL every forge checkout already uses."""
    host = "github.com" if provider == "github" else gitea_host
    return f"git@{host}:{owner}/{repo}.git"


def raw_clone_destination(provider: str, owner: str, repo: str) -> str:
    """Forge's checkout convention, `~` expanded but NOT resolved.

    GitHub `<owner>/<repo>` → `~/repos/personal/<repo>`.
    Gitea `<org>/<repo>` → `~/repos/gitea/<org>-<repo>`, or `~/repos/gitea/<repo>` when the
    name already starts with `<org>-` (`agent-platform/agent-platform-knowledge-base`).

    Kept unresolved for the "is something already here?" check: resolving first follows a
    dangling symlink at this path, and the clone would land wherever it points.
    """
    if provider == "github":
        return os.path.join(os.path.expanduser(GITHUB_CLONE_ROOT), repo)
    name = repo if repo.startswith(f"{owner}-") else f"{owner}-{repo}"
    return os.path.join(os.path.expanduser(GITEA_CLONE_ROOT), name)


def clone_destination(provider: str, owner: str, repo: str) -> str:
    """raw_clone_destination(), canonical. Computed here, never taken from the caller."""
    return str(canonical_path(raw_clone_destination(provider, owner, repo)))


def checkout_origin(path: str) -> str | None:
    """`origin`'s URL if `path` is a real directory holding a git checkout, else None."""
    if os.path.islink(path) or not os.path.isdir(path):
        return None
    try:
        return (
            git.Repo(path, search_parent_directories=False, expand_vars=False).remotes["origin"].url
        )
    except Exception:
        return None


def origin_is(origin: str | None, provider: str, owner: str, repo: str, gitea_host: str) -> bool:
    """True when `origin` names this provider's `<owner>/<repo>`, in any URL form."""
    if not origin:
        return False
    parsed = _parse_remote(origin)
    full_name = _repo_full_name(origin)
    host = "github.com" if provider == "github" else gitea_host
    return (
        parsed is not None
        and full_name is not None
        and parsed[0] == host.lower()
        and full_name.lower() == f"{owner}/{repo}".lower()
    )


def occupied_message(
    dest: str, origin: str | None, provider: str, owner: str, repo: str, gitea_host: str
) -> str:
    """Why an existing destination is refused, and whether it is safe to use.

    The convention is not one-to-one: `a/foo` and `b/foo` on GitHub share
    `~/repos/personal/foo`, and Gitea's `a-b/c` and `a/b-c` share `a-b-c`. So "use the
    checkout that is there" is said only when that checkout's origin IS this repo; an
    agent told otherwise could commit and push to the wrong remote.
    """
    full_name = f"{owner}/{repo}"
    if origin_is(origin, provider, owner, repo, gitea_host):
        return f"{full_name} is already cloned at {dest}; use that checkout."
    if origin:
        return (
            f"{dest} is occupied by a checkout of {redact_url_credentials(origin)}, not "
            f"{full_name}. Nothing was cloned. Where {full_name} should live instead is an "
            "operator decision."
        )
    return f"{dest} already exists and is not a checkout of {full_name}. Nothing was cloned."


def clone_args(depth: int | None, branch: str | None) -> list[str]:
    """Options for `git clone`. Never `-c`/`--config`/`--template`/`--upload-pack`.

    `--no-recurse-submodules` because `.gitmodules` URLs are content the cloned repo
    controls: validating the top-level URL says nothing about them.
    """
    args = ["--no-recurse-submodules"]
    if depth is not None:
        args += ["--depth", str(depth)]
    if branch is not None:
        args += ["--branch", branch]
    return args


def run_clone(
    url: str,
    dest: str,
    args: list[str],
    timeout: float = CLONE_TIMEOUT_S,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Run `git clone <args> -- <url> <dest>`, killed after `timeout` seconds.

    Not `Repo.clone_from()`: it runs git with `as_process=True`, and GitPython applies
    `kill_after_timeout` only when it waits on the process itself, so clone_from has no
    working timeout. GitPython's own unsafe-protocol and unsafe-option checks are called
    here explicitly instead, so going around clone_from loses neither.

    The env passed to execute() MERGES into os.environ, it does not replace it. Only
    GIT_TERMINAL_PROMPT is set: under PM2 there is nobody to answer a prompt.
    """
    git.Git.check_unsafe_protocols(url)
    git.Git.check_unsafe_options(options=args, unsafe_options=git.Repo.unsafe_git_clone_options)
    env = {"GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}
    git.Git().clone(*args, "--", url, dest, kill_after_timeout=timeout, env=env)

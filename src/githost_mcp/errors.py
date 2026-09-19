"""Rejection taxonomy and the closed `reason_class` vocabulary.

Every rejection githost-mcp raises on its own behalf gets a distinct exception type
here, and `classify_reason()` maps that type — never the message text — to one of the
`REASON_CLASSES` below.

Why a separate module: `security` and `tools.git_local` both raise from this taxonomy
and both are imported by `audit`, which needs the classifier. Everything imports from
here and nothing here imports back, so there is no cycle. `security` re-exports the
types it raises, so `from .security import WriteGlobDenied` keeps working.

Why classify by type rather than by message: a regex over exception text rots the
first time someone rewords an error string, and it rots silently — the counter keeps
incrementing, just into the wrong bucket. A type is renamed by the compiler or not at
all.

`reason_class` is a Prometheus label, so the vocabulary is closed *by construction*:
`classify_reason()` can only ever return a member of `REASON_CLASSES`. The unbounded
detail lives in the audit record's `reason` field, which is not a label.
"""

from __future__ import annotations

# The closed vocabulary. Bounded cardinality is the invariant this enforces — adding a
# member is a deliberate edit here, never a side effect of a new error string.
#
# `other` is counted, never dropped: a rising `other` is the signal that agents are
# hitting a limit this vocabulary cannot name yet, which is exactly the finding the
# verb-expansion work is waiting for. That signal only works if everything we *can*
# already name has its own member — shipping known denials into `other` would bury it.
REASON_CLASSES: frozenset[str] = frozenset(
    {
        "not_in_allowed_roots",
        "write_glob_denied",
        "bad_repo_format",
        "no_such_remote",
        "branch_invalid",
        "push_rejected",
        "fetch_rejected",
        "remote_url_rejected",
        "remote_name_invalid",
        "identity_undetermined",
        "repo_not_found",
        "invalid_argument",
        "other",
    }
)


class PathNotAllowed(ValueError):
    """The repo path is outside this agent's allowed read/write roots.

    Also covers the no-roots-resolved case: an agent with no grant at all and an agent
    whose grant excludes this path are both allowlist rejections. The `reason` field
    distinguishes them; the metric does not need to.
    """


class BranchNameInvalid(ValueError):
    """A branch name failed `validate_branch_name()`'s refname rules."""


class RemoteNameInvalid(ValueError):
    """A remote name failed `validate_remote_name()`."""


class NoSuchRemote(ValueError):
    """The named remote does not exist in this repository."""


class RepoNotFound(ValueError):
    """The path is not a git repository, or does not exist."""


class InvalidArgument(ValueError):
    """A caller-supplied argument failed static validation before any remote call.

    Enum-shaped parameters (`merge_style`, `method`) and path-segment validation live
    here. Distinct from `bad_repo_format`, which is common enough to be worth its own
    member.
    """


class RepoFormatInvalid(ValueError):
    """`repo` was not in `owner/repo` form."""


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


class RemoteUrlRejected(ValueError):
    """A remote URL is not a credential-free supported transport."""


# Exception type -> reason class. Ordered most-specific-first: the loop below returns on
# the first isinstance() hit, and several of these share ValueError as a base.
_BY_TYPE: tuple[tuple[type[BaseException], str], ...] = (
    (WriteGlobDenied, "write_glob_denied"),
    (RemoteUrlRejected, "remote_url_rejected"),
    (RemoteNameInvalid, "remote_name_invalid"),
    (BranchNameInvalid, "branch_invalid"),
    (PathNotAllowed, "not_in_allowed_roots"),
    (NoSuchRemote, "no_such_remote"),
    (RepoNotFound, "repo_not_found"),
    (RepoFormatInvalid, "bad_repo_format"),
    (InvalidArgument, "invalid_argument"),
)

# Audit `result` string -> reason class, for the call sites that pass a literal result
# with no exception object in scope (`ac.finish("error:PushRejected")` and friends).
# These are githost-mcp's own constants, not free text.
_BY_RESULT: dict[str, str] = {
    "error:PushRejected": "push_rejected",
    "error:FetchRejected": "fetch_rejected",
    "denied:write_glob": "write_glob_denied",
    "denied:remote_url": "remote_url_rejected",
    "denied:identity_undetermined": "identity_undetermined",
    "denied:bad_repo_format": "bad_repo_format",
    "denied:invalid_argument": "invalid_argument",
    "denied:not_in_allowed_roots": "not_in_allowed_roots",
}


def classify_reason(result: str, exc: BaseException | None = None) -> str:
    """Map a finished call to a `REASON_CLASSES` member.

    The exception type wins when there is one; otherwise the literal result string is
    looked up. Anything unrecognised is `other` — counted, never dropped.
    """
    if exc is not None:
        for exc_type, reason_class in _BY_TYPE:
            if isinstance(exc, exc_type):
                return reason_class
    # `denied:<class>` is the canonical shape written by audit_rejection(), so a new
    # rejection site needs no entry in _BY_RESULT — only a vocabulary member.
    if result.startswith("denied:"):
        suffix = result[len("denied:") :]
        if suffix in REASON_CLASSES:
            return suffix
    mapped = _BY_RESULT.get(result)
    if mapped is not None:
        return mapped
    return "other"

"""Decoding and error-checking for GitPython's PushInfo / FetchInfo bitmasks.

Both classes report the outcome of a remote operation as an integer bitmask.
Stringifying it — the pre-0.9.0 behaviour — turned a rejected push into an opaque
``"1032"`` sitting next to a success-shaped key (vikunja #265, id 276). Decoding it
here, once, keeps every call site honest and diagnosable.

**PushInfo and FetchInfo constants are different integers.** Every shared flag name
has a different value between the two classes, so a mask built for one is silently
wrong against the other — ``PUSH_ERROR_MASK`` (1080) tested against FetchInfo flags
would miss ``FetchInfo.ERROR`` (128) entirely while falsely flagging ``TAG_UPDATE``
(8) and ``FORCED_UPDATE`` (32). Masks are derived from each class's own constants so
a GitPython bump cannot silently rot them.
"""

from __future__ import annotations

from typing import NamedTuple

import git

from .security import scrub

PUSH_FLAG_NAMES: tuple[tuple[int, str], ...] = (
    (git.remote.PushInfo.NEW_TAG, "NEW_TAG"),
    (git.remote.PushInfo.NEW_HEAD, "NEW_HEAD"),
    (git.remote.PushInfo.NO_MATCH, "NO_MATCH"),
    (git.remote.PushInfo.REJECTED, "REJECTED"),
    (git.remote.PushInfo.REMOTE_REJECTED, "REMOTE_REJECTED"),
    (git.remote.PushInfo.REMOTE_FAILURE, "REMOTE_FAILURE"),
    (git.remote.PushInfo.DELETED, "DELETED"),
    (git.remote.PushInfo.FORCED_UPDATE, "FORCED_UPDATE"),
    (git.remote.PushInfo.FAST_FORWARD, "FAST_FORWARD"),
    (git.remote.PushInfo.UP_TO_DATE, "UP_TO_DATE"),
    (git.remote.PushInfo.ERROR, "ERROR"),
)

PUSH_ERROR_MASK = (
    git.remote.PushInfo.ERROR
    | git.remote.PushInfo.REJECTED
    | git.remote.PushInfo.REMOTE_REJECTED
    | git.remote.PushInfo.REMOTE_FAILURE
)

FETCH_FLAG_NAMES: tuple[tuple[int, str], ...] = (
    (git.remote.FetchInfo.NEW_TAG, "NEW_TAG"),
    (git.remote.FetchInfo.NEW_HEAD, "NEW_HEAD"),
    (git.remote.FetchInfo.HEAD_UPTODATE, "HEAD_UPTODATE"),
    (git.remote.FetchInfo.TAG_UPDATE, "TAG_UPDATE"),
    (git.remote.FetchInfo.REJECTED, "REJECTED"),
    (git.remote.FetchInfo.FORCED_UPDATE, "FORCED_UPDATE"),
    (git.remote.FetchInfo.FAST_FORWARD, "FAST_FORWARD"),
    (git.remote.FetchInfo.ERROR, "ERROR"),
)

# FetchInfo has no REMOTE_REJECTED / REMOTE_FAILURE equivalent — do not invent one.
FETCH_ERROR_MASK = git.remote.FetchInfo.ERROR | git.remote.FetchInfo.REJECTED


def decode_push_flags(flags: int) -> list[str]:
    """Decode a PushInfo bitmask to flag names, so a failure is diagnosable from
    the audit log without a bitmask lookup."""
    names = [name for bit, name in PUSH_FLAG_NAMES if flags & bit]
    return names or [str(flags)]


def decode_fetch_flags(flags: int) -> list[str]:
    """Decode a FetchInfo bitmask to flag names. Note FetchInfo.HEAD_UPTODATE is a
    normal, successful outcome — it is not in FETCH_ERROR_MASK."""
    names = [name for bit, name in FETCH_FLAG_NAMES if flags & bit]
    return names or [str(flags)]


class RemoteOutcome(NamedTuple):
    """The decoded result of a push or pull.

    ``summary`` is already scrubbed: it derives from the remote's raw text and can
    carry a credential-bearing remote URL straight to the caller (SC-14).
    """

    flags: list[str]
    summary: str
    failed: bool


def evaluate_push(push_info) -> RemoteOutcome:
    """Decode a push result and decide whether it actually landed.

    An empty result means the remote acknowledged nothing at all — the ref did not
    move, so it is not a success.
    """
    decoded: list[str] = []
    summaries: list[str] = []
    failed = False
    for p in push_info:
        decoded.extend(decode_push_flags(p.flags))
        summary = (p.summary or "").strip()
        if summary:
            summaries.append(summary)
        if p.flags & PUSH_ERROR_MASK:
            failed = True

    if not decoded:
        failed = True
        summaries.append("remote reported no ref updates")

    return RemoteOutcome(flags=decoded, summary=scrub("; ".join(summaries)), failed=failed)


def evaluate_fetch(fetch_info) -> RemoteOutcome:
    """Decode a pull/fetch result and decide whether it succeeded.

    FetchInfo carries the human-readable reason in ``note`` rather than ``summary``.
    An empty result is *not* treated as a failure here: a pull with nothing to fetch
    legitimately returns no FetchInfo entries.
    """
    decoded: list[str] = []
    notes: list[str] = []
    failed = False
    for f in fetch_info:
        decoded.extend(decode_fetch_flags(f.flags))
        note = (getattr(f, "note", "") or "").strip()
        if note:
            notes.append(note)
        if f.flags & FETCH_ERROR_MASK:
            failed = True

    return RemoteOutcome(flags=decoded, summary=scrub("; ".join(notes)), failed=failed)

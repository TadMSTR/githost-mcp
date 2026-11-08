"""Bounded, scrubbed view over a CI log, shared by the Woodpecker, Gitea and GitHub readers.

Before this, `woodpecker_get_logs` kept the *first* 500 lines of a step, so the failure at
the end was cut off (deploy-standard p1's push pipeline 1235: the python step's 832 lines
stopped partway through the passing tests), and `gitea_actions get_job_log` returned the
whole raw log with no bound and no scrub. Neither let an agent find out why CI failed.
(githost-scoped-2026-11 p1, #1425 (id 1508), #1389 (id 1472))

The view never logs what it returns. Callers put only the view parameters and counts in
the audit trail — a log line can carry a pipeline environment value.
"""

from __future__ import annotations

from typing import Any

from .errors import InvalidArgument
from .security import scrub

# Caps, per githost-scoped-2026-11-p1-ci-diagnostics plan, Configuration.
MAX_LINES = 500
MAX_BYTES = 64 * 1024
MAX_CONTEXT = 20
MAX_GREP_LEN = 200
# One line can exceed the whole byte budget (a minified asset, a base64 blob). Without a
# per-line cap that line could never be returned, and paging past it would be impossible.
MAX_LINE_CHARS = 2000
_LINE_CUT_MARK = " …[line cut]"

MODES = ("tail", "head", "range")

# A log body is capped before it is parsed or split (KB IV-14), and the read stops at the
# cap rather than draining to EOF (KB IV-72), so neither memory nor time is unbounded.
DOWNLOAD_MAX = 32 * 1024 * 1024


def read_capped(chunks, cap: int | None = None) -> tuple[bytes, bool]:
    """Join byte chunks up to `cap` bytes. Returns (data, cut); stops at the cap."""
    cap = DOWNLOAD_MAX if cap is None else cap
    out: list[bytes] = []
    size = 0
    for chunk in chunks:
        room = cap - size
        if len(chunk) > room:
            out.append(chunk[:room])
            return b"".join(out), True
        out.append(chunk)
        size += len(chunk)
    return b"".join(out), False


async def aread_capped(chunks, cap: int | None = None) -> tuple[bytes, bool]:
    """read_capped() over an async iterator of byte chunks (httpx `aiter_bytes()`)."""
    cap = DOWNLOAD_MAX if cap is None else cap
    out: list[bytes] = []
    size = 0
    async for chunk in chunks:
        room = cap - size
        if len(chunk) > room:
            out.append(chunk[:room])
            return b"".join(out), True
        out.append(chunk)
        size += len(chunk)
    return b"".join(out), False


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)


def validate_view(
    mode: str | None,
    offset: int,
    limit: int,
    grep: str | None,
    context: int,
) -> None:
    """Raise InvalidArgument for a view request the reader would refuse.

    `mode` may be None: the caller picks a default from the step state afterwards.
    """
    if mode is not None and mode not in MODES:
        raise InvalidArgument(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    if offset < 0:
        raise InvalidArgument("offset must be >= 0")
    if limit < 1:
        raise InvalidArgument("limit must be >= 1")
    if not 0 <= context <= MAX_CONTEXT:
        raise InvalidArgument(f"context must be between 0 and {MAX_CONTEXT}")
    if grep is not None:
        # A literal substring, never a regex: an agent-supplied pattern is a ReDoS vector,
        # and a substring covers what is actually searched for (FAILED, not ok, Error).
        if not grep:
            raise InvalidArgument("grep must not be empty")
        if len(grep) > MAX_GREP_LEN:
            raise InvalidArgument(f"grep must be at most {MAX_GREP_LEN} characters")
        if _has_control(grep):
            raise InvalidArgument("grep must not contain control characters")


def collapse_cr(line: str) -> str:
    """Keep what a terminal would show: the text after the last carriage return.

    apt and pip redraw progress with `\\r`, so one logical line arrives as
    `(Reading database ... 5%\\r(Reading database ... 10%\\r...` — about 1 KB of text for
    one line of meaning. A trailing `\\r` (a CRLF line ending) is dropped first, or every
    CRLF line would collapse to empty.
    """
    line = line.rstrip("\r")
    if "\r" in line:
        line = line.rsplit("\r", 1)[1]
    return line


def split_text(text: str) -> list[str]:
    """Split raw log text into lines on `\\n` only.

    `str.splitlines()` also splits on `\\r`, which would turn every progress redraw into a
    separate line before collapse_cr() could fold it.
    """
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _prepare(line: str) -> str:
    # Scrub before cutting: a cut can split a token and leave an unmatched prefix.
    shown = scrub(collapse_cr(line))
    if len(shown) > MAX_LINE_CHARS:
        shown = shown[:MAX_LINE_CHARS] + _LINE_CUT_MARK
    return shown


def _merge_windows(hits: list[int], context: int, total: int) -> list[int]:
    """Line indices covered by `context` lines either side of each hit, overlaps merged."""
    out: list[int] = []
    last = -1
    for h in hits:
        start = max(h - context, last + 1)
        end = min(h + context, total - 1)
        out.extend(range(start, end + 1))
        last = max(last, end)
    return out


def _take(indices: list[int], lines: list[str], from_end: bool, max_lines: int) -> list[int]:
    """Pick indices from one end, stopping at the line cap or the byte cap."""
    picked: list[int] = []
    used = 0
    seq = reversed(indices) if from_end else iter(indices)
    for i in seq:
        if len(picked) >= max_lines:
            break
        cost = len(lines[i].encode("utf-8")) + 1
        if picked and used + cost > MAX_BYTES:
            break
        picked.append(i)
        used += cost
    if from_end:
        picked.reverse()
    return picked


def _segments(picked: list[int], lines: list[str]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for i in picked:
        if segs and segs[-1]["to"] == i:
            segs[-1]["lines"].append(lines[i])
            segs[-1]["to"] = i + 1
        else:
            segs.append({"from": i, "to": i + 1, "lines": [lines[i]]})
    return segs


def view(
    raw_lines: list[str],
    *,
    mode: str = "tail",
    offset: int = 0,
    limit: int = MAX_LINES,
    grep: str | None = None,
    ignore_case: bool = False,
    context: int = 0,
) -> dict[str, Any]:
    """Return a bounded, scrubbed view of `raw_lines`.

    Line numbers are 0-based. `returned_from` is inclusive and `returned_to` exclusive,
    so the next page of a `range` read starts at `offset=returned_to`.

    - `head` / `tail`: up to `limit` lines from that end.
    - `range`: lines `[offset, offset + limit)`.
    - `grep`: the lines containing the literal substring (plus `context` either side),
      taken from the end for `tail` and from the start for `head`; with `range`, only
      hits inside `[offset, offset + limit)` count. The response then carries `segments`
      (contiguous runs, each with its own `from`/`to`) instead of `lines`.

    At most MAX_LINES lines and about MAX_BYTES of text are returned; `truncated` says
    whether anything selected was left out. Matching runs on the scrubbed text, so a grep
    cannot be used to test whether a redacted credential is present.
    """
    validate_view(mode, offset, limit, grep, context)
    lines = [_prepare(line) for line in raw_lines]
    total = len(lines)
    max_lines = min(limit, MAX_LINES)

    if mode == "range":
        candidates = list(range(min(offset, total), min(offset + limit, total)))
    else:
        candidates = list(range(total))

    result: dict[str, Any] = {"mode": mode, "total_lines": total}

    if grep is None:
        picked = _take(candidates, lines, mode == "tail", max_lines)
        result["lines"] = [lines[i] for i in picked]
    else:
        needle = grep.lower() if ignore_case else grep
        hits = [i for i in candidates if needle in (lines[i].lower() if ignore_case else lines[i])]
        covered = _merge_windows(hits, context, total)
        if mode == "range":
            lo, hi = (candidates[0], candidates[-1] + 1) if candidates else (0, 0)
            covered = [i for i in covered if lo <= i < hi]
        picked = _take(covered, lines, mode == "tail", max_lines)
        hit_set = set(hits)
        result["match_count"] = len(hits)
        result["matches_returned"] = sum(1 for i in picked if i in hit_set)
        result["segments"] = _segments(picked, lines)
        candidates = covered

    result["returned_from"] = picked[0] if picked else None
    result["returned_to"] = picked[-1] + 1 if picked else None
    result["truncated"] = len(picked) < len(candidates)
    return result

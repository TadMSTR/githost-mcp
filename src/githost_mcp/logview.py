"""Bounded, scrubbed view over a CI log, shared by the Woodpecker, Gitea and GitHub readers.

Before this, `woodpecker_get_logs` kept the *first* 500 lines of a step, so the failure at
the end was cut off (deploy-standard p1's push pipeline 1235: the python step's 832 lines
stopped partway through the passing tests), and `gitea_actions get_job_log` returned the
whole raw log with no bound and no scrub. Neither let an agent find out why CI failed.
(githost-scoped-2026-11 p1, vikunja#1425, vikunja#1389)

The view never logs what it returns. Callers put only the view parameters and counts in
the audit trail — a log line can carry a pipeline environment value.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    """Join byte chunks up to `cap` bytes. Returns (data, cut); stops at the cap.

    Reaching the cap exactly counts as cut: asking for one more chunk only to learn whether
    the body ended there would block on a stalled stream (CodeRabbit CR-05, PR #34). A body
    of exactly `cap` bytes is therefore reported cut, which costs at most its last line.
    """
    cap = DOWNLOAD_MAX if cap is None else cap
    out: list[bytes] = []
    size = 0
    for chunk in chunks:
        room = cap - size
        out.append(chunk[:room])
        size += min(len(chunk), room)
        if size >= cap:
            return b"".join(out), True
    return b"".join(out), False


async def aread_capped(chunks, cap: int | None = None) -> tuple[bytes, bool]:
    """read_capped() over an async iterator of byte chunks (httpx `aiter_bytes()`)."""
    cap = DOWNLOAD_MAX if cap is None else cap
    out: list[bytes] = []
    size = 0
    async for chunk in chunks:
        room = cap - size
        out.append(chunk[:room])
        size += min(len(chunk), room)
        if size >= cap:
            return b"".join(out), True
    return b"".join(out), False


def decode_capped(data: bytes, cut: bool) -> str:
    """Decode a capped log body, dropping the partial last line of a cut one.

    The cap can fall inside a line, and so inside a token or a `scheme://user:pass@` URL.
    scrub() can't recognise half a credential, and `tail` returns exactly that line, so
    everything after the last newline of a cut body is dropped before the view sees it
    (audit F-03, githost-scoped-2026-11 p1).
    """
    if cut:
        nl = data.rfind(b"\n")
        data = data[: nl + 1] if nl >= 0 else b""
    return data.decode("utf-8", "replace")


# Rebuilding an httpx.Response from an already-decoded body with the upstream headers makes
# httpx try to decode it again: `content-encoding: gzip` over plain bytes raises
# DecodingError in the constructor (CodeRabbit CR-03, PR #34).
_BODY_FRAMING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def decoded_headers(headers) -> dict[str, str]:
    """Upstream headers minus body framing, for a Response rebuilt from decoded bytes."""
    return {k: v for k, v in headers.items() if k.lower() not in _BODY_FRAMING_HEADERS}


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


class TextLines(Sequence):
    """The lines of a log text, split on `\\n` only and only as far as a read needs.

    A head or tail read takes its lines from one end with a bounded split, so a 32 MiB
    flood of newlines is never turned into a 33.5M-item list for a `tail limit=5`
    (#1431 CR-08/R2-03: 2.1 GB peak before). `len()` counts newlines without splitting.
    Anything else (indexing, a range or grep read) splits the whole text once.
    """

    def __init__(self, text: str):
        self._text = text[:-1] if text.endswith("\n") else text
        self._empty = not text
        self._list: list[str] | None = None

    def __len__(self) -> int:
        if self._list is not None:
            return len(self._list)
        return 0 if self._empty else self._text.count("\n") + 1

    def end(self, count: int, from_end: bool) -> tuple[int, list[str]]:
        """(index of the first line returned, up to `count` lines from one end)."""
        if self._empty or count <= 0:
            return (len(self) if from_end else 0), []
        if from_end:
            parts = self._text.rsplit("\n", count)
            picked = parts[-count:] if len(parts) > count else parts
            return len(self) - len(picked), picked
        return 0, self._text.split("\n", count)[:count]

    def _lines(self) -> list[str]:
        if self._list is None:
            self._list = [] if self._empty else self._text.split("\n")
            self._text = ""
        return self._list

    def __getitem__(self, i):
        return self._lines()[i]

    def __iter__(self):
        return iter(self._lines())


def split_text(text: str) -> TextLines:
    """Split raw log text into lines on `\\n` only, lazily (see TextLines).

    `str.splitlines()` also splits on `\\r`, which would turn every progress redraw into a
    separate line before collapse_cr() could fold it.
    """
    return TextLines(text)


class _Shown:
    """Scrubbed, display-cut lines, computed per index on first use and kept.

    view() used to build two full-length lists (scrubbed and cut) before selecting
    anything; only the lines a read returns are ever needed whole.
    """

    def __init__(self, raw: Sequence[str]):
        self._raw = raw
        self._cache: dict[int, str] = {}

    def __getitem__(self, i: int) -> str:
        line = self._cache.get(i)
        if line is None:
            line = _cut(scrub(collapse_cr(self._raw[i])))
            self._cache[i] = line
        return line


def _cut(shown: str) -> str:
    # Callers scrub first: a cut can split a token and leave an unmatched prefix.
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


def _take(indices: Sequence[int], lines: Any, from_end: bool, max_lines: int) -> list[int]:
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


def _take_hits(
    hits: list[int],
    context: int,
    lo: int,
    hi: int,
    lines: Any,
    from_end: bool,
    max_lines: int,
) -> list[int]:
    """Pick each hit, then its context nearest-first, until a cap is reached.

    Taking the merged windows in line order spent the budget on the context before a hit:
    `limit=1, context=1` returned only the line above the match (CodeRabbit CR-09, PR #34).
    """
    picked: set[int] = set()
    used = 0
    for h in reversed(hits) if from_end else hits:
        window = [h]
        for d in range(1, context + 1):
            window += [i for i in (h - d, h + d) if lo <= i < hi]
        for i in window:
            if i in picked:
                continue
            if len(picked) >= max_lines:
                return sorted(picked)
            cost = len(lines[i].encode("utf-8")) + 1
            if picked and used + cost > MAX_BYTES:
                return sorted(picked)
            picked.add(i)
            used += cost
    return sorted(picked)


def _segments(picked: list[int], lines: Any) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for i in picked:
        if segs and segs[-1]["to"] == i:
            segs[-1]["lines"].append(lines[i])
            segs[-1]["to"] = i + 1
        else:
            segs.append({"from": i, "to": i + 1, "lines": [lines[i]]})
    return segs


def view(
    raw_lines: Sequence[str],
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
    max_lines = min(limit, MAX_LINES)

    if grep is None and mode in ("head", "tail") and isinstance(raw_lines, TextLines):
        # One bounded split from the needed end; nothing else is materialised.
        total = len(raw_lines)
        start, end_lines = raw_lines.end(max_lines, mode == "tail")
        lines: Any = _Shown(end_lines)
        local = _take(range(len(end_lines)), lines, mode == "tail", max_lines)
        result: dict[str, Any] = {"mode": mode, "total_lines": total}
        result["lines"] = [lines[i] for i in local]
        picked = [start + i for i in local]
        candidates: Any = range(total)
    else:
        total = len(raw_lines)
        lines = _Shown(raw_lines)
        if mode == "range":
            lo, hi = min(offset, total), min(offset + limit, total)
        else:
            lo, hi = 0, total
        candidates = range(lo, hi)
        result = {"mode": mode, "total_lines": total}

        if grep is None:
            picked = _take(candidates, lines, mode == "tail", max_lines)
            result["lines"] = [lines[i] for i in picked]
        else:
            # Match on the whole scrubbed line, not the display cut: a hit past character
            # MAX_LINE_CHARS was reported as `match_count: 0` (audit F-04). Each line is
            # scrubbed for the test and dropped; only the hit indices are kept.
            needle = grep.lower() if ignore_case else grep
            # scrub() only replaces text with something containing `*`, and collapse_cr()
            # keeps a substring, so an ASCII needle with no `*` that is absent from the raw
            # line cannot be in the scrubbed one: skip the scrub. (ASCII only: Python's
            # lower() is contextual for a final sigma.)
            prefilter = "*" not in needle and needle.isascii()
            hits = []
            for i in candidates:
                raw = raw_lines[i]
                if prefilter and needle not in (raw.lower() if ignore_case else raw):
                    continue
                text = scrub(collapse_cr(raw))
                if needle in (text.lower() if ignore_case else text):
                    hits.append(i)
            covered = [i for i in _merge_windows(hits, context, total) if lo <= i < hi]
            picked = _take_hits(hits, context, lo, hi, lines, mode == "tail", max_lines)
            hit_set = set(hits)
            result["match_count"] = len(hits)
            result["matches_returned"] = sum(1 for i in picked if i in hit_set)
            result["segments"] = _segments(picked, lines)
            candidates = covered

    if picked:
        result["returned_from"] = picked[0]
        result["returned_to"] = picked[-1] + 1
    elif mode == "range":
        # An empty page still advances: the next one starts after the scanned range
        # (CodeRabbit CR-10, PR #34). Otherwise a range grep with no hit returned
        # `returned_to: null` and the documented paging stopped.
        result["returned_from"] = result["returned_to"] = hi
    else:
        result["returned_from"] = result["returned_to"] = None
    result["truncated"] = len(picked) < len(candidates)
    return result

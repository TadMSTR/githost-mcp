"""Tests for the shared bounded log reader (githost-scoped-2026-11 p1, Phase 1)."""

import time

import pytest

from githost_mcp import logview
from githost_mcp.config import reset_config
from githost_mcp.errors import InvalidArgument
from githost_mcp.logview import (
    MAX_BYTES,
    MAX_CONTEXT,
    MAX_GREP_LEN,
    MAX_LINE_CHARS,
    MAX_LINES,
    collapse_cr,
    split_text,
    validate_view,
    view,
)


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("WOODPECKER_TOKEN", "wp_known_token_1234567890abcdef")
    reset_config()


def _lines(n: int) -> list[str]:
    return [f"line {i}" for i in range(n)]


# --- modes -------------------------------------------------------------------------------


def test_tail_returns_last_lines():
    r = view(_lines(1000), mode="tail", limit=3)
    assert r["lines"] == ["line 997", "line 998", "line 999"]
    assert (r["returned_from"], r["returned_to"]) == (997, 1000)
    assert r["total_lines"] == 1000
    assert r["truncated"] is True


def test_head_returns_first_lines():
    r = view(_lines(10), mode="head", limit=2)
    assert r["lines"] == ["line 0", "line 1"]
    assert (r["returned_from"], r["returned_to"]) == (0, 2)
    assert r["truncated"] is True


def test_range_pages_with_returned_to():
    first = view(_lines(10), mode="range", offset=2, limit=3)
    assert first["lines"] == ["line 2", "line 3", "line 4"]
    nxt = view(_lines(10), mode="range", offset=first["returned_to"], limit=3)
    assert nxt["lines"] == ["line 5", "line 6", "line 7"]
    # Range truncation is about the requested window, not the whole log.
    assert first["truncated"] is False


def test_range_past_end_is_empty_not_an_error():
    r = view(_lines(5), mode="range", offset=50, limit=10)
    assert r["lines"] == []
    assert r["returned_from"] is None and r["returned_to"] is None
    assert r["truncated"] is False


def test_whole_short_log_not_truncated():
    r = view(_lines(5), mode="tail")
    assert len(r["lines"]) == 5
    assert r["truncated"] is False


def test_empty_log():
    r = view([], mode="tail")
    assert r["lines"] == [] and r["total_lines"] == 0 and r["truncated"] is False


# --- caps --------------------------------------------------------------------------------


def test_line_cap_holds_even_when_limit_is_larger():
    r = view(_lines(5000), mode="tail", limit=10_000)
    assert len(r["lines"]) == MAX_LINES
    assert r["lines"][-1] == "line 4999"
    assert r["truncated"] is True


def test_byte_cap_holds():
    big = ["x" * 1000 for _ in range(400)]  # ~400 KB, under the line cap
    r = view(big, mode="head")
    returned = sum(len(s) + 1 for s in r["lines"])
    assert returned <= MAX_BYTES
    assert len(r["lines"]) < 400
    assert r["truncated"] is True


def test_overlong_single_line_is_cut_not_dropped():
    r = view(["a" * (MAX_BYTES * 2)], mode="tail")
    assert len(r["lines"]) == 1
    assert len(r["lines"][0]) < MAX_LINE_CHARS + 50
    assert r["lines"][0].endswith("[line cut]")


# --- carriage returns --------------------------------------------------------------------


def test_cr_progress_collapses_to_last_redraw():
    raw = "(Reading database ... \r(Reading database ... 5%\r(Reading database ... 100%"
    assert collapse_cr(raw) == "(Reading database ... 100%"


def test_crlf_line_ending_is_not_collapsed_to_empty():
    assert collapse_cr("hello\r") == "hello"


def test_split_text_does_not_split_on_cr():
    assert split_text("a\rb\nc\n") == ["a\rb", "c"]
    assert split_text("") == []
    r = view(split_text("a\rb\nc\n"), mode="head")
    assert r["lines"] == ["b", "c"]


# --- grep --------------------------------------------------------------------------------


def test_grep_no_context():
    lines = ["ok 1", "not ok 2 boom", "ok 3", "not ok 4 bang"]
    r = view(lines, mode="tail", grep="not ok")
    assert "lines" not in r
    assert r["match_count"] == 2
    assert r["segments"] == [
        {"from": 1, "to": 2, "lines": ["not ok 2 boom"]},
        {"from": 3, "to": 4, "lines": ["not ok 4 bang"]},
    ]


def test_grep_context_windows_merge_when_overlapping():
    lines = _lines(20)
    lines[5] = "FAIL a"
    lines[7] = "FAIL b"
    r = view(lines, mode="head", grep="FAIL", context=2)
    assert len(r["segments"]) == 1
    seg = r["segments"][0]
    assert (seg["from"], seg["to"]) == (3, 10)
    assert r["matches_returned"] == 2


def test_grep_separate_windows_stay_separate():
    lines = _lines(40)
    lines[5] = "FAIL a"
    lines[30] = "FAIL b"
    r = view(lines, mode="head", grep="FAIL", context=1)
    assert [(s["from"], s["to"]) for s in r["segments"]] == [(4, 7), (29, 32)]


def test_grep_no_match():
    r = view(_lines(10), mode="tail", grep="nothing here")
    assert r["match_count"] == 0
    assert r["segments"] == []
    assert r["truncated"] is False


def test_grep_tail_keeps_last_hits_under_cap():
    lines = [f"FAIL {i}" for i in range(MAX_LINES + 100)]
    r = view(lines, mode="tail", grep="FAIL")
    assert r["matches_returned"] == MAX_LINES
    assert r["segments"][-1]["lines"][-1] == f"FAIL {MAX_LINES + 99}"
    assert r["truncated"] is True


def test_grep_ignore_case():
    r = view(["Error: x", "fine"], mode="tail", grep="error", ignore_case=True)
    assert r["match_count"] == 1
    assert view(["Error: x"], mode="tail", grep="error")["match_count"] == 0


def test_grep_regex_metacharacters_are_literal():
    lines = ["a.b", "axb", "(.*)+$", "zzz"]
    r = view(lines, mode="head", grep=".")
    assert r["match_count"] == 2  # "a.b" and "(.*)+$", never "axb"
    r = view(lines, mode="head", grep="(.*)+$")
    assert r["match_count"] == 1


def test_grep_in_range_only_counts_hits_inside_the_window():
    lines = ["FAIL"] * 10
    r = view(lines, mode="range", offset=2, limit=3, grep="FAIL", context=5)
    assert r["match_count"] == 3
    assert [(s["from"], s["to"]) for s in r["segments"]] == [(2, 5)]


def test_grep_adversarial_input_is_linear():
    # A literal substring search has no backtracking to blow up; bound it anyway.
    lines = ["a" * 1999 + "!"] * 2000
    start = time.monotonic()
    view(lines, mode="tail", grep="a" * MAX_GREP_LEN)
    assert time.monotonic() - start < 5


# --- scrub -------------------------------------------------------------------------------


def test_url_userinfo_is_scrubbed():
    r = view(["cloning https://user:s3cr3tpass@git.example.com/o/r.git"], mode="tail")
    assert "s3cr3tpass" not in r["lines"][0]
    assert "https://***@git.example.com" in r["lines"][0]


def test_known_token_is_scrubbed():
    r = view(["header: wp_known_token_1234567890abcdef"], mode="tail")
    assert "wp_known_token" not in r["lines"][0]


def test_grep_cannot_probe_for_a_scrubbed_credential():
    lines = ["auth wp_known_token_1234567890abcdef ok"]
    r = view(lines, mode="tail", grep="wp_known_token")
    assert r["match_count"] == 0


def test_scrub_runs_before_the_line_cut():
    # A token straddling the cut must not leave an unredacted prefix behind.
    # Cut first, the kept text would end "https://u:longp" — no "@", so the URL rule
    # no longer matches and the password prefix leaks.
    line = "x" * (MAX_LINE_CHARS - 15) + "https://u:longpassword@h.example/"
    r = view([line], mode="tail")
    assert "u:longp" not in r["lines"][0]
    assert "https://***@" in r["lines"][0]


def test_cr_collapse_runs_before_scrub_and_hides_overdrawn_text():
    # Only the final redraw survives; earlier redraws are not shown at all.
    r = view(["https://u:pw1234567@h.example/\rclean"], mode="tail")
    assert r["lines"] == ["clean"]


# --- validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "middle"},
        {"offset": -1},
        {"limit": 0},
        {"context": -1},
        {"context": MAX_CONTEXT + 1},
        {"grep": ""},
        {"grep": "x" * (MAX_GREP_LEN + 1)},
        {"grep": "bad\nnewline"},
        {"grep": "bell\x07"},
        {"grep": "del\x7f"},
    ],
)
def test_validate_view_rejects(kwargs):
    args = {"mode": "tail", "offset": 0, "limit": 10, "grep": None, "context": 0} | kwargs
    with pytest.raises(InvalidArgument):
        validate_view(**args)


def test_validate_view_accepts_none_mode_and_max_values():
    validate_view(None, 0, 1, "x" * MAX_GREP_LEN, MAX_CONTEXT)


def test_view_never_logs_content(monkeypatch):
    # The reader has no logger at all: content can only leave through its return value.
    assert not hasattr(logview, "log")


# --- capped readers (KB IV-14, IV-72) ------------------------------------------------------


def _chunks(n, size, seen):
    for i in range(n):
        seen.append(i)
        yield b"x" * size


def test_read_capped_stops_at_cap_without_draining():
    seen = []
    data, cut = logview.read_capped(_chunks(1000, 10, seen), cap=25)
    assert (len(data), cut) == (25, True)
    assert len(seen) == 3  # the iterator was not drained to its end


def test_read_capped_under_cap_is_whole():
    data, cut = logview.read_capped(iter([b"ab", b"cd"]), cap=10)
    assert (data, cut) == (b"abcd", False)


@pytest.mark.asyncio
async def test_aread_capped_stops_at_cap_without_draining():
    seen = []

    async def gen():
        for i in range(1000):
            seen.append(i)
            yield b"y" * 10

    data, cut = await logview.aread_capped(gen(), cap=25)
    assert (len(data), cut) == (25, True)
    assert len(seen) == 3

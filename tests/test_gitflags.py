"""Tests for PushInfo/FetchInfo bitmask decoding.

The masks are the whole point of this module, and getting them wrong is silent:
green tests would coexist with a pull that never reports failure. These assert the
constants directly against the installed GitPython rather than against literals.
"""

import git
import pytest

from githost_mcp.gitflags import (
    FETCH_ERROR_MASK,
    PUSH_ERROR_MASK,
    decode_fetch_flags,
    decode_push_flags,
    evaluate_fetch,
    evaluate_push,
)


class FakeInfo:
    """Stands in for PushInfo/FetchInfo — only flags plus one text field is read."""

    def __init__(self, flags: int, summary: str = "", note: str = ""):
        self.flags = flags
        self.summary = summary
        self.note = note


# --- mask correctness --------------------------------------------------------


def test_push_and_fetch_masks_are_different_values():
    """Every shared flag name has a different integer between the two classes, so a
    mask built for one is silently wrong against the other."""
    assert FETCH_ERROR_MASK != PUSH_ERROR_MASK


def test_push_mask_would_miss_fetch_error_bit():
    """The specific trap: PUSH_ERROR_MASK tested against FetchInfo flags misses
    FetchInfo.ERROR entirely."""
    assert not (PUSH_ERROR_MASK & git.remote.FetchInfo.ERROR)
    assert FETCH_ERROR_MASK & git.remote.FetchInfo.ERROR


def test_push_mask_would_falsely_flag_benign_fetch_outcomes():
    """...while falsely flagging TAG_UPDATE and FORCED_UPDATE as failures."""
    for benign in (git.remote.FetchInfo.TAG_UPDATE, git.remote.FetchInfo.FORCED_UPDATE):
        assert PUSH_ERROR_MASK & benign, "precondition: the push mask does collide here"
        assert not (FETCH_ERROR_MASK & benign), "fetch mask must not treat this as an error"


def test_fetch_mask_derives_from_fetch_constants():
    """Re-derived, not hardcoded — a GitPython bump must not silently rot it."""
    assert FETCH_ERROR_MASK == git.remote.FetchInfo.ERROR | git.remote.FetchInfo.REJECTED


def test_fetch_info_has_no_remote_rejected_equivalent():
    """Guards against someone mirroring the push mask's extra bits onto FetchInfo."""
    assert not hasattr(git.remote.FetchInfo, "REMOTE_REJECTED")
    assert not hasattr(git.remote.FetchInfo, "REMOTE_FAILURE")


# --- decoding ----------------------------------------------------------------


def test_decode_fetch_flags_names_the_bits():
    flags = git.remote.FetchInfo.FAST_FORWARD | git.remote.FetchInfo.NEW_HEAD
    assert set(decode_fetch_flags(flags)) == {"FAST_FORWARD", "NEW_HEAD"}


def test_decode_fetch_flags_falls_back_to_the_raw_value():
    assert decode_fetch_flags(0) == ["0"]


def test_decode_push_flags_names_the_bits():
    assert decode_push_flags(git.remote.PushInfo.FAST_FORWARD) == ["FAST_FORWARD"]


# --- evaluate_fetch ----------------------------------------------------------


def test_evaluate_fetch_clean_result_is_not_a_failure():
    outcome = evaluate_fetch([FakeInfo(git.remote.FetchInfo.FAST_FORWARD, note="")])
    assert outcome.failed is False
    assert outcome.flags == ["FAST_FORWARD"]


def test_evaluate_fetch_head_uptodate_is_not_a_failure():
    outcome = evaluate_fetch([FakeInfo(git.remote.FetchInfo.HEAD_UPTODATE)])
    assert outcome.failed is False


@pytest.mark.parametrize("bit", [git.remote.FetchInfo.ERROR, git.remote.FetchInfo.REJECTED])
def test_evaluate_fetch_flags_error_bits(bit):
    outcome = evaluate_fetch([FakeInfo(bit, note="would clobber existing tag")])
    assert outcome.failed is True
    assert outcome.summary == "would clobber existing tag"


def test_evaluate_fetch_surfaces_note_scrubbed():
    """FetchInfo.note carries the human-readable reason — and can carry a
    credential-bearing remote URL with it (SC-14)."""
    leaked = "https://ted:ghp_LEAKED_TOKEN@github.com/o/r.git"
    outcome = evaluate_fetch([FakeInfo(git.remote.FetchInfo.ERROR, note=f"rejected from {leaked}")])
    assert "ghp_LEAKED_TOKEN" not in outcome.summary
    assert "***@github.com" in outcome.summary


def test_evaluate_fetch_empty_is_not_a_failure():
    """A pull with nothing to fetch legitimately returns no entries — unlike a push,
    where an empty result means the ref did not move."""
    assert evaluate_fetch([]).failed is False


# --- evaluate_push -----------------------------------------------------------


def test_evaluate_push_empty_is_a_failure():
    assert evaluate_push([]).failed is True


def test_evaluate_push_error_bit_is_a_failure():
    outcome = evaluate_push([FakeInfo(git.remote.PushInfo.REJECTED, summary="[rejected] main")])
    assert outcome.failed is True
    assert "REJECTED" in outcome.flags


def test_evaluate_push_scrubs_the_summary():
    leaked = "https://ted:ghp_LEAKED_TOKEN@github.com/o/r.git"
    outcome = evaluate_push([FakeInfo(git.remote.PushInfo.ERROR, summary=f"failed to {leaked}")])
    assert "ghp_LEAKED_TOKEN" not in outcome.summary


# --- evaluate_push honours PushInfoList.error --------------------------------
#
# GitPython raises only when it parsed no porcelain lines at all
# (remote.py:_get_push_info — `if not output: raise`). When *some* lines parsed
# and git still exited non-zero it swallows the exception onto the list's `error`
# attribute and returns normally. Every surviving PushInfo can then be error-free
# while the push as a whole failed, so flags alone are not enough.


class FakePushInfoList(list):
    """A list that carries `error`, the way git.remote.PushInfoList does."""

    def __init__(self, items, error=None):
        super().__init__(items)
        self.error = error


def test_evaluate_push_list_error_is_a_failure_despite_clean_flags():
    """The regression that matters: a partially-rejected push whose rejected ref
    is a line GitPython could not parse leaves only the successful entries behind.
    Reading flags alone calls that a success — vikunja #265 in a third shape."""
    clean = FakeInfo(git.remote.PushInfo.FAST_FORWARD, summary="abc..def")
    outcome = evaluate_push(FakePushInfoList([clean], error=Exception("push rejected some refs")))
    assert outcome.failed is True, "a list-level error must not be overridden by clean flags"
    assert "push rejected some refs" in outcome.summary


def test_evaluate_push_no_list_error_still_succeeds():
    """Guards against 'fail whenever the attribute exists' — an unset error is the
    normal case for every successful push and must stay a success."""
    clean = FakeInfo(git.remote.PushInfo.FAST_FORWARD, summary="abc..def")
    assert evaluate_push(FakePushInfoList([clean], error=None)).failed is False


def test_evaluate_push_plain_list_without_error_attribute_still_works():
    """evaluate_push is handed plain lists by callers and tests; the lookup must
    not require the attribute to exist."""
    clean = FakeInfo(git.remote.PushInfo.FAST_FORWARD, summary="abc..def")
    assert evaluate_push([clean]).failed is False


def test_evaluate_push_list_error_is_scrubbed():
    """The swallowed exception is a GitCommandError whose cmdline can carry a
    credential-bearing remote URL."""
    leaked = "https://ted:ghp_LISTERROR_TOKEN@github.com/o/r.git"
    clean = FakeInfo(git.remote.PushInfo.FAST_FORWARD, summary="abc..def")
    outcome = evaluate_push(FakePushInfoList([clean], error=Exception(f"failed pushing {leaked}")))
    assert outcome.failed is True
    assert "ghp_LISTERROR_TOKEN" not in outcome.summary

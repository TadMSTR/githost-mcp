"""Tests for the PyGithub wrapper (get_github caching/init, github_call masking)."""

from unittest.mock import patch

import pytest

import githost_mcp._providers.github_client as gc
from githost_mcp.config import reset_config

TOKEN = "ghp_fakefakefake123456789012345678901"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    reset_config()
    gc._client = None
    yield
    gc._client = None


def test_get_github_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reset_config()
    gc._client = None
    with pytest.raises(ValueError, match="GITHUB_TOKEN is not set"):
        gc.get_github()


def test_get_github_returns_cached_client():
    first = gc.get_github()
    second = gc.get_github()
    assert first is second


def test_get_github_init_failure_masks_detail(monkeypatch):
    with (
        patch("github.Github", side_effect=RuntimeError("bad")),
        pytest.raises(ValueError, match="GitHub client init failed"),
    ):
        gc.get_github()


def test_github_call_passthrough():
    assert gc.github_call(lambda x: x + 1, 41) == 42


def test_github_call_masks_token_in_exception():
    def boom():
        raise RuntimeError(f"request failed with {TOKEN}")

    with pytest.raises(ValueError) as exc:
        gc.github_call(boom)
    assert TOKEN not in str(exc.value)
    assert "***" in str(exc.value)


def test_github_call_generic_error_preserved():
    def boom():
        raise RuntimeError("plain failure")

    with pytest.raises(ValueError, match="plain failure"):
        gc.github_call(boom)

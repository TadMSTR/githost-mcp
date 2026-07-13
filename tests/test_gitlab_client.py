"""Tests for the python-gitlab wrapper (get_gitlab caching/init, gitlab_call masking)."""

from unittest.mock import patch

import pytest

import githost_mcp._providers.gitlab_client as glc
from githost_mcp.config import reset_config

TOKEN = "glpat-fakefakefake1234567890abcdef"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("GITLAB_URL", "https://gitlab.com")
    reset_config()
    glc._client = None
    yield
    glc._client = None


def test_get_gitlab_missing_token(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    reset_config()
    glc._client = None
    with pytest.raises(ValueError, match="GITLAB_TOKEN is not set"):
        glc.get_gitlab()


def test_get_gitlab_returns_cached_client():
    first = glc.get_gitlab()
    second = glc.get_gitlab()
    assert first is second


def test_get_gitlab_init_failure_masks_detail():
    with (
        patch("gitlab.Gitlab", side_effect=RuntimeError("bad")),
        pytest.raises(ValueError, match="GitLab client init failed"),
    ):
        glc.get_gitlab()


def test_gitlab_call_passthrough():
    assert glc.gitlab_call(lambda x: x * 2, 21) == 42


def test_gitlab_call_401_maps_to_auth_error():
    def boom():
        raise RuntimeError("401 Unauthorized")

    with pytest.raises(ValueError, match="authentication failed"):
        glc.gitlab_call(boom)


def test_gitlab_call_403_maps_to_authz_error():
    def boom():
        raise RuntimeError("403 Forbidden")

    with pytest.raises(ValueError, match="authorization denied"):
        glc.gitlab_call(boom)


def test_gitlab_call_masks_token_in_generic_error():
    def boom():
        raise RuntimeError(f"request failed with {TOKEN}")

    with pytest.raises(ValueError) as exc:
        glc.gitlab_call(boom)
    assert TOKEN not in str(exc.value)
    assert "***" in str(exc.value)


def test_gitlab_call_generic_error_preserved():
    def boom():
        raise RuntimeError("plain failure")

    with pytest.raises(ValueError, match="plain failure"):
        glc.gitlab_call(boom)

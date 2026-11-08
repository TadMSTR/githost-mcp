"""python-gitlab wrapper with credential masking on auth errors."""

from __future__ import annotations

from typing import Any

import structlog

from ..audit import current_child
from ..config import get_config
from .github_client import api_call_name

log = structlog.get_logger(__name__)

_client: Any = None


def get_gitlab():
    """Return a cached Gitlab instance. Raises ValueError on missing token."""
    global _client
    if _client is not None:
        return _client
    config = get_config()
    if not config.gitlab_token:
        raise ValueError("GITLAB_TOKEN is not set")
    try:
        import gitlab  # type: ignore

        _client = gitlab.Gitlab(config.gitlab_url, private_token=config.gitlab_token)
        return _client
    except Exception as e:
        raise ValueError(f"GitLab client init failed: {type(e).__name__}") from None


def gitlab_call(fn, *args, **kwargs):
    """Execute a python-gitlab call, masking credentials from any exception.

    A child span `http.gitlab` of the current tool span, as github_call() does for GitHub.
    """
    try:
        with current_child("http.gitlab", **{"githost.api.call": api_call_name(fn)}):
            return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        config = get_config()
        if config.gitlab_token and config.gitlab_token in msg:
            msg = msg.replace(config.gitlab_token, "***")
        if "401" in msg or "Unauthorized" in msg:
            raise ValueError("GitLab authentication failed (check GITLAB_TOKEN)") from None
        if "403" in msg or "Forbidden" in msg:
            raise ValueError("GitLab authorization denied (insufficient token scope)") from None
        raise ValueError(msg) from None

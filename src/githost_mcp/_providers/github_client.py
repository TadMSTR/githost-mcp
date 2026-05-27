"""PyGithub wrapper with credential masking on auth errors."""

from __future__ import annotations

from typing import Any

import structlog

from ..config import get_config

log = structlog.get_logger(__name__)

_client: Any = None


def get_github():
    """Return a cached Github instance. Raises ValueError on missing token."""
    global _client
    if _client is not None:
        return _client
    config = get_config()
    if not config.github_token:
        raise ValueError("GITHUB_TOKEN is not set")
    try:
        from github import Github  # type: ignore

        _client = Github(config.github_token)
        return _client
    except Exception as e:
        raise ValueError(f"GitHub client init failed: {type(e).__name__}") from None


def github_call(fn, *args, **kwargs):
    """Execute a PyGithub call, masking credentials from any exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        config = get_config()
        if config.github_token and config.github_token in msg:
            msg = msg.replace(config.github_token, "***")
        raise ValueError(msg) from None

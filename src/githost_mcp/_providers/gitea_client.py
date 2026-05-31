"""httpx client for Gitea API with auth header injection and credential masking."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from ..config import get_config

log = structlog.get_logger(__name__)


def _get_gitea_headers() -> dict[str, str]:
    config = get_config()
    if not config.gitea_token:
        raise ValueError("GITEA_TOKEN is not set")
    return {"Authorization": f"token {config.gitea_token}", "Content-Type": "application/json"}


def _gitea_base() -> str:
    config = get_config()
    if not config.gitea_url:
        raise ValueError("GITEA_URL is not set")
    return config.gitea_url.rstrip("/") + "/api/v1"


async def gitea_get(path: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_gitea_base()}{path}", headers=_get_gitea_headers())
            _check_gitea_response(resp)
            return resp.json()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Gitea request failed: {type(e).__name__}") from None


async def gitea_post(path: str, data: dict) -> Any:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_gitea_base()}{path}",
                headers=_get_gitea_headers(),
                json=data,
            )
            _check_gitea_response(resp)
            return resp.json()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Gitea request failed: {type(e).__name__}") from None


async def gitea_post_void(path: str, data: dict) -> None:
    """POST to Gitea API and discard response body (for 204 No Content endpoints)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_gitea_base()}{path}",
                headers=_get_gitea_headers(),
                json=data,
            )
            _check_gitea_response(resp)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Gitea request failed: {type(e).__name__}") from None


def _check_gitea_response(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise ValueError("Gitea authentication failed (check GITEA_TOKEN)")
    if resp.status_code == 403:
        raise ValueError("Gitea authorization denied (insufficient token scope)")
    if resp.status_code >= 400:
        raise ValueError(f"Gitea API error {resp.status_code}: {resp.text[:200]}")

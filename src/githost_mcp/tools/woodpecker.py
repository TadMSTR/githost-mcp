"""Woodpecker CI tools via httpx (2 tools)."""

from __future__ import annotations

from typing import Optional

import httpx
import structlog

from ..audit import AuditCtx
from ..config import get_config

log = structlog.get_logger(__name__)


def _woodpecker_headers() -> dict[str, str]:
    config = get_config()
    if not config.woodpecker_token:
        raise ValueError("WOODPECKER_TOKEN is not set")
    return {"Authorization": f"Bearer {config.woodpecker_token}", "Content-Type": "application/json"}


def _woodpecker_base() -> str:
    config = get_config()
    if not config.woodpecker_url:
        raise ValueError("WOODPECKER_URL is not set")
    return config.woodpecker_url.rstrip("/") + "/api"


def _check_response(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise ValueError("Woodpecker authentication failed (check WOODPECKER_TOKEN)")
    if resp.status_code == 403:
        raise ValueError("Woodpecker authorization denied")
    if resp.status_code >= 400:
        raise ValueError(f"Woodpecker API error {resp.status_code}")


def register(mcp) -> None:
    @mcp.tool
    async def woodpecker_trigger(repo: str, branch: Optional[str] = None) -> dict:
        """Trigger a Woodpecker CI pipeline for a repository.

        Args:
            repo: Repository in 'owner/repo' format.
            branch: Branch to trigger (default: repo default branch).
        """
        ac = AuditCtx("woodpecker_trigger", "woodpecker", repo, {"repo": repo, "branch": branch})
        try:
            params = {}
            if branch:
                params["branch"] = branch
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{_woodpecker_base()}/repos/{repo}/pipelines",
                    headers=_woodpecker_headers(),
                    params=params,
                )
                _check_response(resp)
                data = resp.json()
            ac.finish("ok")
            return {
                "pipeline_id": data.get("id") or data.get("number"),
                "status": data.get("status"),
                "branch": data.get("branch"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def woodpecker_status(repo: str, pipeline_id: int) -> dict:
        """Get status of a Woodpecker CI pipeline run.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline ID or number from woodpecker_trigger.
        """
        ac = AuditCtx("woodpecker_status", "woodpecker", repo, {"repo": repo, "pipeline_id": pipeline_id})
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{_woodpecker_base()}/repos/{repo}/pipelines/{pipeline_id}",
                    headers=_woodpecker_headers(),
                )
                _check_response(resp)
                data = resp.json()
            ac.finish("ok")
            return {
                "id": data.get("id") or data.get("number"),
                "status": data.get("status"),
                "branch": data.get("branch"),
                "started_at": data.get("started"),
                "finished_at": data.get("finished"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

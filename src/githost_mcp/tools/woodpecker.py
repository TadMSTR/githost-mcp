"""Woodpecker CI tools via httpx (5 tools)."""

from __future__ import annotations

import re

import httpx
import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..security import scrub

_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
_REPO_FMT_ERR = "repo must be in 'owner/repo' format (alphanumeric, hyphens, underscores, dots)"

log = structlog.get_logger(__name__)


def _woodpecker_headers() -> dict[str, str]:
    config = get_config()
    if not config.woodpecker_token:
        raise ValueError("WOODPECKER_TOKEN is not set")
    return {
        "Authorization": f"Bearer {config.woodpecker_token}",
        "Content-Type": "application/json",
    }


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
        # The response body is the only thing that says *why*. Dropping it is what
        # made #269 expensive: woodpecker_trigger returned "API error 400" for its
        # entire life with no hint that the request form was wrong. Bounded and
        # scrubbed — the body can echo back a credential-bearing URL (SC-14).
        detail = scrub(resp.text[:300]).strip()
        raise ValueError(
            f"Woodpecker API error {resp.status_code}" + (f": {detail}" if detail else "")
        )


async def _woodpecker_repo_id(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    owner: str,
    name: str,
) -> int:
    """Resolve owner/name to a numeric repo ID via the Woodpecker 3.x lookup endpoint."""
    resp = await client.get(
        f"{base}/repos/lookup/{owner}/{name}",
        headers=headers,
    )
    if resp.status_code == 404:
        raise ValueError(f"Repository '{owner}/{name}' not found in Woodpecker (not registered?)")
    _check_response(resp)
    return int(resp.json()["id"])


def register(mcp) -> None:
    @mcp.tool
    async def woodpecker_trigger(repo: str, branch: str | None = None) -> dict:
        """Trigger a Woodpecker CI pipeline for a repository.

        Args:
            repo: Repository in 'owner/repo' format.
            branch: Branch to trigger (default: repo default branch).
        """
        if not _REPO_RE.match(repo):
            return {"error": _REPO_FMT_ERR}
        ac = AuditCtx("woodpecker_trigger", "woodpecker", repo, {"repo": repo, "branch": branch})
        try:
            owner, name = repo.split("/", 1)
            # Woodpecker 3.x expects a JSON body here, not query params — a
            # query-param POST returns HTTP 400 with an empty body (vikunja #269,
            # id 280). Omit `branch` entirely when unset so the repo default applies
            # server-side; sending null is not the same thing.
            payload: dict[str, str] = {}
            if branch:
                payload["branch"] = branch
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with httpx.AsyncClient(timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.post(
                    f"{base}/repos/{repo_id}/pipelines",
                    headers=headers,
                    json=payload,
                )
                _check_response(resp)
                data = resp.json()
            ac.finish("ok")
            return {
                # Woodpecker resolves the {pipeline} path segment in status/logs/cancel
                # as the per-repo pipeline *number*, not the global id — GET
                # /repos/<id>/pipelines/<global-id> 404s. Return the number as the
                # chainable handle so trigger -> status actually works; the global id
                # is kept separately for reference. (vikunja #269, id 280)
                "pipeline_id": data.get("number") or data.get("id"),
                "internal_id": data.get("id"),
                "status": data.get("status"),
                "branch": data.get("branch"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_list_pipelines(
        repo: str,
        limit: int = 10,
        status: str | None = None,
    ) -> dict:
        """List recent pipeline runs for a Woodpecker repository.

        Args:
            repo: Repository in 'owner/repo' format.
            limit: Max pipelines to return (default 10, max 100).
            status: Optional filter by status (e.g. pending/running/success/failure/error).
        """
        if not _REPO_RE.match(repo):
            return {"error": _REPO_FMT_ERR}
        limit = min(limit, 100)
        ac = AuditCtx(
            "woodpecker_list_pipelines",
            "woodpecker",
            repo,
            {"repo": repo, "limit": limit, "status": status},
        )
        try:
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with httpx.AsyncClient(timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines",
                    headers=headers,
                    params={"page": 1, "limit": limit},
                )
                _check_response(resp)
                data = resp.json()
            pipelines = [
                {
                    "id": p.get("id"),
                    "number": p.get("number"),
                    "status": p.get("status"),
                    "branch": p.get("branch"),
                    "event": p.get("event"),
                    "created": p.get("created"),
                    "started": p.get("started"),
                    "finished": p.get("finished"),
                }
                for p in (data if isinstance(data, list) else [])
            ]
            if status:
                pipelines = [p for p in pipelines if p.get("status") == status]
            ac.finish("ok")
            return {"repo": repo, "pipelines": pipelines}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_get_logs(
        repo: str,
        pipeline_id: int,
        step_name: str | None = None,
    ) -> dict:
        """Fetch step output from a Woodpecker pipeline run.

        Returns up to 500 lines of log output. Pipeline output may contain
        sensitive environment variable values — log content is NOT written to
        the audit trail, only call metadata is logged.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger).
            step_name: Step name to fetch (default: first step).
        """
        if not _REPO_RE.match(repo):
            return {"error": _REPO_FMT_ERR}
        # NOTE: Only metadata is logged here — step output may contain secrets from
        # pipeline environment variables and must not appear in the audit trail.
        ac = AuditCtx(
            "woodpecker_get_logs",
            "woodpecker",
            repo,
            {"repo": repo, "pipeline_id": pipeline_id, "step_name": step_name},
        )
        try:
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with httpx.AsyncClient(timeout=30.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                steps_resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}/steps",
                    headers=headers,
                )
                _check_response(steps_resp)
                steps = steps_resp.json()
                if not isinstance(steps, list) or not steps:
                    ac.finish("error:no_steps")
                    return {"error": "No steps found for pipeline"}

                if step_name:
                    step = next((s for s in steps if s.get("name") == step_name), None)
                    if step is None:
                        ac.finish("error:step_not_found")
                        return {"error": f"Step '{step_name}' not found"}
                else:
                    step = steps[0]

                step_id = int(step.get("id"))
                step_label = step.get("name", str(step_id))

                log_resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}/{step_id}/logs",
                    headers=headers,
                )
                _check_response(log_resp)
                log_data = log_resp.json()

            if isinstance(log_data, list):
                lines = [
                    entry.get("out", str(entry)) if isinstance(entry, dict) else str(entry)
                    for entry in log_data
                ]
            else:
                lines = [str(log_data)]

            truncated = len(lines) > 500
            if truncated:
                lines = lines[:500]

            ac.finish("ok")
            result: dict = {"step": step_label, "lines": lines}
            if truncated:
                result["truncated"] = True
                result["notice"] = "Output truncated at 500 lines"
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_pipeline_cancel(repo: str, pipeline_id: int) -> dict:
        """Cancel a running Woodpecker pipeline.

        DESTRUCTIVE: Terminates a running CI pipeline. HITL gated in scoped-mcp
        manifests — sysadmin only.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger).
        """
        if not _REPO_RE.match(repo):
            return {"error": _REPO_FMT_ERR}
        ac = AuditCtx(
            "woodpecker_pipeline_cancel",
            "woodpecker",
            repo,
            {"repo": repo, "pipeline_id": pipeline_id},
        )
        try:
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with httpx.AsyncClient(timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.delete(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
                )
                if resp.status_code == 409:
                    ac.finish("error:already_finished")
                    return {"error": "Pipeline is already finished and cannot be cancelled"}
                _check_response(resp)
            ac.finish("ok")
            return {"cancelled": True, "id": pipeline_id}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_status(repo: str, pipeline_id: int) -> dict:
        """Get status of a Woodpecker CI pipeline run.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger).
        """
        if not _REPO_RE.match(repo):
            return {"error": _REPO_FMT_ERR}
        ac = AuditCtx(
            "woodpecker_status", "woodpecker", repo, {"repo": repo, "pipeline_id": pipeline_id}
        )
        try:
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with httpx.AsyncClient(timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
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
            return {"error": scrub(str(e))}

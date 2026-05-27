"""GitLab tools via python-gitlab (4 tools)."""

from __future__ import annotations

from typing import Optional

import structlog

from .._providers.gitlab_client import get_gitlab, gitlab_call
from ..audit import AuditCtx
from ..security import mask_credentials

log = structlog.get_logger(__name__)


def _err(e: Exception) -> dict:
    return {"error": mask_credentials(str(e))}


def register(mcp) -> None:
    @mcp.tool
    def gitlab_create_release(
        project: str,
        tag: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Create a GitLab release for a tag.

        Args:
            project: Project in 'namespace/project' format.
            tag: Tag name for the release.
            name: Release name (defaults to tag name).
            description: Release notes markdown.
        """
        ac = AuditCtx("gitlab_create_release", "gitlab", project, {"project": project, "tag": tag})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            release = gitlab_call(
                proj.releases.create,
                {
                    "name": name or tag,
                    "tag_name": tag,
                    "description": description or "",
                },
            )
            ac.finish("ok")
            return {"tag": tag, "name": release.name, "url": getattr(release, "_links", {}).get("self", "")}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_get_release(project: str, tag: str) -> dict:
        """Get a GitLab release by tag.

        Args:
            project: Project in 'namespace/project' format.
            tag: Tag name.
        """
        ac = AuditCtx("gitlab_get_release", "gitlab", project, {"project": project, "tag": tag})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            release = gitlab_call(proj.releases.get, tag)
            ac.finish("ok")
            return {
                "tag": release.tag_name,
                "name": release.name,
                "description": release.description,
                "released_at": release.released_at,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_list_releases(project: str, limit: int = 10) -> dict:
        """List recent releases for a GitLab project.

        Args:
            project: Project in 'namespace/project' format.
            limit: Max releases to return (default 10).
        """
        ac = AuditCtx("gitlab_list_releases", "gitlab", project, {"project": project, "limit": limit})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            releases = []
            for r in gitlab_call(proj.releases.list, get_all=False)[:limit]:
                releases.append({
                    "tag": r.tag_name,
                    "name": r.name,
                    "released_at": r.released_at,
                })
            ac.finish("ok")
            return {"project": project, "releases": releases}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_mr_list(project: str, state: str = "opened", limit: int = 20) -> dict:
        """List merge requests by state.

        Args:
            project: Project in 'namespace/project' format.
            state: 'opened', 'closed', 'locked', or 'merged' (default: opened).
            limit: Max MRs to return (default 20).
        """
        ac = AuditCtx("gitlab_mr_list", "gitlab", project, {"project": project, "state": state})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            mrs = []
            for mr in gitlab_call(proj.mergerequests.list, state=state, get_all=False)[:limit]:
                mrs.append({
                    "iid": mr.iid,
                    "title": mr.title,
                    "state": mr.state,
                    "author": mr.author.get("username") if mr.author else None,
                    "source_branch": mr.source_branch,
                    "target_branch": mr.target_branch,
                    "created_at": mr.created_at,
                    "web_url": mr.web_url,
                })
            ac.finish("ok")
            return {"project": project, "mrs": mrs}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

"""GitLab tools via python-gitlab (7 tools)."""

from __future__ import annotations

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
        name: str | None = None,
        description: str | None = None,
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
            return {
                "tag": tag,
                "name": release.name,
                "url": getattr(release, "_links", {}).get("self", ""),
            }
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
        ac = AuditCtx(
            "gitlab_list_releases", "gitlab", project, {"project": project, "limit": limit}
        )
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            releases = []
            for r in gitlab_call(proj.releases.list, get_all=False)[:limit]:
                releases.append(
                    {
                        "tag": r.tag_name,
                        "name": r.name,
                        "released_at": r.released_at,
                    }
                )
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
                mrs.append(
                    {
                        "iid": mr.iid,
                        "title": mr.title,
                        "state": mr.state,
                        "author": mr.author.get("username") if mr.author else None,
                        "source_branch": mr.source_branch,
                        "target_branch": mr.target_branch,
                        "created_at": mr.created_at,
                        "web_url": mr.web_url,
                    }
                )
            ac.finish("ok")
            return {"project": project, "mrs": mrs}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_mr_create(
        project: str,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str | None = None,
    ) -> dict:
        """Open a merge request on a GitLab project.

        Args:
            project: Project in 'namespace/project' format.
            title: MR title.
            source_branch: Source branch name.
            target_branch: Target branch name.
            description: MR description (optional).
        """
        ac = AuditCtx(
            "gitlab_mr_create",
            "gitlab",
            project,
            {"project": project, "source_branch": source_branch, "target_branch": target_branch},
        )
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            mr = gitlab_call(
                proj.mergerequests.create,
                {
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "title": title,
                    "description": description or "",
                },
            )
            ac.finish("ok")
            return {
                "iid": mr.iid,
                "title": mr.title,
                "state": mr.state,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "web_url": mr.web_url,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_mr_get(project: str, mr_iid: int) -> dict:
        """Get details of a GitLab merge request.

        Args:
            project: Project in 'namespace/project' format.
            mr_iid: Merge request internal ID (iid), not the global id.
        """
        ac = AuditCtx("gitlab_mr_get", "gitlab", project, {"project": project, "mr_iid": mr_iid})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            mr = gitlab_call(proj.mergerequests.get, mr_iid)
            ac.finish("ok")
            return {
                "iid": mr.iid,
                "title": mr.title,
                "state": mr.state,
                "merge_status": mr.merge_status,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "author": mr.author.get("username") if mr.author else None,
                "web_url": mr.web_url,
                "created_at": mr.created_at,
                "updated_at": mr.updated_at,
                "labels": mr.labels,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_mr_merge(
        project: str,
        mr_iid: int,
        merge_commit_message: str | None = None,
        squash: bool = False,
    ) -> dict:
        """Merge a GitLab merge request.

        DESTRUCTIVE: Permanently merges the MR source branch into the target branch. This
        tool should be HITL gated in scoped-mcp manifests for all agents (same treatment as
        gitea_pr_merge) — that gating is a scoped-mcp manifest change outside this repo.

        Args:
            project: Project in 'namespace/project' format.
            mr_iid: Merge request internal ID (iid) to merge.
            merge_commit_message: Optional custom merge commit message.
            squash: Squash commits into a single commit on merge (default False).
        """
        ac = AuditCtx(
            "gitlab_mr_merge",
            "gitlab",
            project,
            {"project": project, "mr_iid": mr_iid, "squash": squash},
        )
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            mr = gitlab_call(proj.mergerequests.get, mr_iid)
            kwargs: dict = {"squash": squash}
            if merge_commit_message:
                kwargs["merge_commit_message"] = merge_commit_message
            gitlab_call(mr.merge, **kwargs)
            ac.finish("ok")
            return {"iid": mr.iid, "state": mr.state, "merged": True, "web_url": mr.web_url}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

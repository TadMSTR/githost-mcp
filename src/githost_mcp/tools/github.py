"""GitHub tools via PyGithub (7 tools)."""

from __future__ import annotations

from typing import Optional

import structlog

from .._providers.github_client import get_github, github_call
from ..audit import AuditCtx
from ..security import mask_credentials

log = structlog.get_logger(__name__)


def _err(e: Exception) -> dict:
    return {"error": mask_credentials(str(e))}


def register(mcp) -> None:
    @mcp.tool
    def github_create_release(
        repo: str,
        tag: str,
        name: Optional[str] = None,
        body: Optional[str] = None,
        draft: bool = False,
        prerelease: bool = False,
        generate_release_notes: bool = False,
    ) -> dict:
        """Create a GitHub release for a tag.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name for the release.
            name: Release title (defaults to tag name).
            body: Release notes markdown.
            draft: Create as draft (default False).
            prerelease: Mark as pre-release (default False).
            generate_release_notes: Auto-generate release notes from commits (default False).
        """
        ac = AuditCtx("github_create_release", "github", repo, {"repo": repo, "tag": tag})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            release = github_call(
                gh_repo.create_git_release,
                tag=tag,
                name=name or tag,
                message=body or "",
                draft=draft,
                prerelease=prerelease,
                generate_release_notes=generate_release_notes,
            )
            ac.finish("ok")
            return {"id": release.id, "tag": tag, "url": release.html_url, "draft": draft}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_get_release(repo: str, tag: str) -> dict:
        """Get GitHub release metadata by tag.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name.
        """
        ac = AuditCtx("github_get_release", "github", repo, {"repo": repo, "tag": tag})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            release = github_call(gh_repo.get_release, tag)
            ac.finish("ok")
            return {
                "id": release.id,
                "tag": release.tag_name,
                "name": release.title,
                "url": release.html_url,
                "draft": release.draft,
                "prerelease": release.prerelease,
                "published_at": release.published_at.isoformat() if release.published_at else None,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_list_releases(repo: str, limit: int = 10) -> dict:
        """List recent releases for a GitHub repository.

        Args:
            repo: Repository in 'owner/repo' format.
            limit: Max releases to return (default 10).
        """
        ac = AuditCtx("github_list_releases", "github", repo, {"repo": repo, "limit": limit})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            releases = []
            for r in github_call(gh_repo.get_releases).get_page(0)[:limit]:
                releases.append({
                    "tag": r.tag_name,
                    "name": r.title,
                    "url": r.html_url,
                    "draft": r.draft,
                    "prerelease": r.prerelease,
                    "published_at": r.published_at.isoformat() if r.published_at else None,
                })
            ac.finish("ok")
            return {"repo": repo, "releases": releases}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_workflow_list(repo: str, ref: Optional[str] = None, limit: int = 10) -> dict:
        """List workflow runs for a repo, optionally filtered by ref.

        Args:
            repo: Repository in 'owner/repo' format.
            ref: Branch, tag, or SHA to filter by (optional).
            limit: Max runs to return (default 10).
        """
        ac = AuditCtx("github_workflow_list", "github", repo, {"repo": repo, "ref": ref})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            kwargs = {}
            if ref:
                kwargs["branch"] = ref
            runs = []
            for run in github_call(gh_repo.get_workflow_runs, **kwargs)[:limit]:
                runs.append({
                    "id": run.id,
                    "name": run.name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "workflow": run.workflow_id,
                    "created_at": run.created_at.isoformat(),
                    "url": run.html_url,
                })
            ac.finish("ok")
            return {"repo": repo, "runs": runs}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_workflow_status(repo: str, run_id: int) -> dict:
        """Get status and conclusion for a specific workflow run.

        Args:
            repo: Repository in 'owner/repo' format.
            run_id: Workflow run ID from github_workflow_list.
        """
        ac = AuditCtx("github_workflow_status", "github", repo, {"repo": repo, "run_id": run_id})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            run = github_call(gh_repo.get_workflow_run, run_id)
            ac.finish("ok")
            return {
                "id": run.id,
                "name": run.name,
                "status": run.status,
                "conclusion": run.conclusion,
                "created_at": run.created_at.isoformat(),
                "updated_at": run.updated_at.isoformat(),
                "url": run.html_url,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_pr_list(repo: str, state: str = "open", limit: int = 20) -> dict:
        """List pull requests by state.

        Args:
            repo: Repository in 'owner/repo' format.
            state: 'open', 'closed', or 'all' (default: open).
            limit: Max PRs to return (default 20).
        """
        ac = AuditCtx("github_pr_list", "github", repo, {"repo": repo, "state": state})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            prs = []
            for pr in github_call(gh_repo.get_pulls, state=state)[:limit]:
                prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "state": pr.state,
                    "author": pr.user.login if pr.user else None,
                    "base": pr.base.ref,
                    "head": pr.head.ref,
                    "created_at": pr.created_at.isoformat(),
                    "url": pr.html_url,
                })
            ac.finish("ok")
            return {"repo": repo, "prs": prs}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_pr_comments(repo: str, pr_number: int) -> dict:
        """List comments on a pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: PR number.
        """
        ac = AuditCtx("github_pr_comments", "github", repo, {"repo": repo, "pr_number": pr_number})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(gh_repo.get_pull, pr_number)
            comments = []
            for c in github_call(pr.get_issue_comments):
                comments.append({
                    "id": c.id,
                    "author": c.user.login if c.user else None,
                    "body": c.body,
                    "created_at": c.created_at.isoformat(),
                    "updated_at": c.updated_at.isoformat(),
                })
            ac.finish("ok")
            return {"repo": repo, "pr": pr_number, "comments": comments}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

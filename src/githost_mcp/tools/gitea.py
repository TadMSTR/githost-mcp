"""Gitea tools via httpx (8 tools)."""

from __future__ import annotations

from typing import Optional

import structlog

from .._providers.gitea_client import gitea_get, gitea_post, gitea_post_void
from ..audit import AuditCtx
from ..config import get_config

log = structlog.get_logger(__name__)


def register(mcp) -> None:
    @mcp.tool
    async def gitea_create_release(
        repo: str,
        tag: str,
        name: Optional[str] = None,
        body: Optional[str] = None,
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict:
        """Create a release on the configured Gitea instance (GITEA_URL).

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name for the release.
            name: Release title (defaults to tag name).
            body: Release notes markdown.
            draft: Create as draft (default False).
            prerelease: Mark as pre-release (default False).
        """
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_create_release", "gitea", repo, {"repo": repo, "tag": tag})
        try:
            data = {
                "tag_name": tag,
                "name": name or tag,
                "body": body or "",
                "draft": draft,
                "prerelease": prerelease,
            }
            result = await gitea_post(f"/repos/{owner}/{repo_name}/releases", data)
            ac.finish("ok")
            return {"id": result.get("id"), "tag": tag, "url": result.get("html_url")}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_get_release(repo: str, tag: str) -> dict:
        """Get a Gitea release by tag.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name.
        """
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_get_release", "gitea", repo, {"repo": repo, "tag": tag})
        try:
            releases = await gitea_get(f"/repos/{owner}/{repo_name}/releases")
            for r in releases:
                if r.get("tag_name") == tag:
                    ac.finish("ok")
                    return {
                        "id": r.get("id"),
                        "tag": r.get("tag_name"),
                        "name": r.get("name"),
                        "url": r.get("html_url"),
                        "draft": r.get("draft"),
                        "prerelease": r.get("prerelease"),
                        "published_at": r.get("published_at"),
                    }
            raise ValueError(f"Release for tag '{tag}' not found in {repo}")
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_list_releases(repo: str, limit: int = 10) -> dict:
        """List recent releases for a Gitea repository.

        Args:
            repo: Repository in 'owner/repo' format.
            limit: Max releases to return (default 10).
        """
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_list_releases", "gitea", repo, {"repo": repo, "limit": limit})
        try:
            data = await gitea_get(f"/repos/{owner}/{repo_name}/releases?limit={limit}")
            releases = [
                {
                    "tag": r.get("tag_name"),
                    "name": r.get("name"),
                    "url": r.get("html_url"),
                    "draft": r.get("draft"),
                    "prerelease": r.get("prerelease"),
                    "published_at": r.get("published_at"),
                }
                for r in (data if isinstance(data, list) else [])
            ]
            ac.finish("ok")
            return {"repo": repo, "releases": releases}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_pr_list(repo: str, state: str = "open", limit: int = 20) -> dict:

        """List pull requests on a Gitea repository.

        Args:
            repo: Repository in 'owner/repo' format.
            state: 'open', 'closed', or 'all' (default: open).
            limit: Max PRs to return (default 20).
        """
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_pr_list", "gitea", repo, {"repo": repo, "state": state})
        try:
            data = await gitea_get(f"/repos/{owner}/{repo_name}/pulls?state={state}&limit={limit}")
            prs = [
                {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "author": pr.get("user", {}).get("login") if pr.get("user") else None,
                    "base": pr.get("base", {}).get("label"),
                    "head": pr.get("head", {}).get("label"),
                    "created_at": pr.get("created_at"),
                    "url": pr.get("html_url"),
                }
                for pr in (data if isinstance(data, list) else [])
            ]
            ac.finish("ok")
            return {"repo": repo, "prs": prs}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_pr_create(
        repo: str,
        title: str,
        head: str,
        base: str,
        body: Optional[str] = None,
        draft: bool = False,
    ) -> dict:
        """Open a pull request on a Gitea repository.

        Args:
            repo: Repository in 'owner/repo' format.
            title: PR title.
            head: Source branch name.
            base: Target branch name.
            body: PR description (optional).
            draft: Create as draft PR (default False).
        """
        if "/" not in repo:
            return {"error": "repo must be in 'owner/repo' format"}
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_pr_create", "gitea", repo, {"repo": repo, "head": head, "base": base})
        try:
            data = {"title": title, "head": head, "base": base, "body": body or "", "draft": draft}
            result = await gitea_post(f"/repos/{owner}/{repo_name}/pulls", data)
            ac.finish("ok")
            return {
                "number": result.get("number"),
                "title": result.get("title"),
                "url": result.get("html_url"),
                "state": result.get("state"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_pr_get(repo: str, pr_number: int) -> dict:
        """Get details of a Gitea pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
        """
        if "/" not in repo:
            return {"error": "repo must be in 'owner/repo' format"}
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_pr_get", "gitea", repo, {"repo": repo, "pr_number": pr_number})
        try:
            result = await gitea_get(f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
            ac.finish("ok")
            return {
                "number": result.get("number"),
                "title": result.get("title"),
                "state": result.get("state"),
                "mergeable": result.get("mergeable"),
                "head": result.get("head", {}).get("label"),
                "base": result.get("base", {}).get("label"),
                "url": result.get("html_url"),
                "created_at": result.get("created_at"),
                "updated_at": result.get("updated_at"),
                "labels": [lb.get("name") for lb in (result.get("labels") or [])],
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_pr_comment(repo: str, pr_number: int, body: str) -> dict:
        """Post a comment on a Gitea pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
            body: Comment text (markdown supported).
        """
        if "/" not in repo:
            return {"error": "repo must be in 'owner/repo' format"}
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_pr_comment", "gitea", repo, {"repo": repo, "pr_number": pr_number})
        try:
            # Gitea uses the issues endpoint for PR comments
            result = await gitea_post(
                f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments", {"body": body}
            )
            ac.finish("ok")
            return {
                "id": result.get("id"),
                "url": result.get("html_url"),
                "created_at": result.get("created_at"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    async def gitea_pr_merge(
        repo: str,
        pr_number: int,
        merge_style: str = "merge",
        message: Optional[str] = None,
    ) -> dict:
        """Merge a Gitea pull request.

        DESTRUCTIVE: Permanently merges the PR branch into the base branch.
        HITL gated in scoped-mcp manifests for all agents — operator confirmation
        required before this tool executes.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number to merge.
            merge_style: One of 'merge', 'squash', or 'rebase' (default: merge).
            message: Optional merge commit message title.
        """
        if "/" not in repo:
            return {"error": "repo must be in 'owner/repo' format"}
        valid_styles = {"merge", "squash", "rebase"}
        if merge_style not in valid_styles:
            return {"error": f"merge_style must be one of: {', '.join(sorted(valid_styles))}"}
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx(
            "gitea_pr_merge", "gitea", repo,
            {"repo": repo, "pr_number": pr_number, "merge_style": merge_style},
        )
        try:
            data: dict = {"Do": merge_style}
            if message:
                data["merge_message_title"] = message
            await gitea_post_void(f"/repos/{owner}/{repo_name}/pulls/{pr_number}/merge", data)
            ac.finish("ok")
            return {"merged": True, "pr_number": pr_number}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

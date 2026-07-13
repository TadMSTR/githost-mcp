"""GitHub tools via PyGithub (10 tools)."""

from __future__ import annotations

import re

import structlog

from .._providers.github_client import get_github, github_call
from ..audit import AuditCtx
from ..security import mask_credentials

log = structlog.get_logger(__name__)

# GitHub full names are always exactly 'owner/repo' (one slash, no numeric-ID form).
_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
_REPO_FMT_ERR = "repo must be in 'owner/repo' format (alphanumeric, hyphens, underscores, dots)"


def _err(e: Exception) -> dict:
    return {"error": mask_credentials(str(e))}


def _bad_repo(repo: str) -> dict | None:
    """Return an error dict if `repo` is not a valid 'owner/repo' string, else None.

    Defense-in-depth: `repo` reaches PyGithub's get_repo() and is used to build API
    paths. PyGithub URL-encodes segments so traversal isn't observed, but validating
    here matches the guard gitea.py/woodpecker.py already apply and rejects malformed
    input before it reaches the client library.
    """
    return None if _REPO_RE.match(repo) else {"error": _REPO_FMT_ERR}


def register(mcp) -> None:
    @mcp.tool
    def github_create_release(
        repo: str,
        tag: str,
        name: str | None = None,
        body: str | None = None,
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
        if err := _bad_repo(repo):
            return err
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
        if err := _bad_repo(repo):
            return err
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
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_list_releases", "github", repo, {"repo": repo, "limit": limit})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            releases = []
            for r in github_call(gh_repo.get_releases).get_page(0)[:limit]:
                releases.append(
                    {
                        "tag": r.tag_name,
                        "name": r.title,
                        "url": r.html_url,
                        "draft": r.draft,
                        "prerelease": r.prerelease,
                        "published_at": r.published_at.isoformat() if r.published_at else None,
                    }
                )
            ac.finish("ok")
            return {"repo": repo, "releases": releases}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_workflow_list(repo: str, ref: str | None = None, limit: int = 10) -> dict:
        """List workflow runs for a repo, optionally filtered by ref.

        Args:
            repo: Repository in 'owner/repo' format.
            ref: Branch, tag, or SHA to filter by (optional).
            limit: Max runs to return (default 10).
        """
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_workflow_list", "github", repo, {"repo": repo, "ref": ref})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            kwargs = {}
            if ref:
                kwargs["branch"] = ref
            runs = []
            for run in github_call(gh_repo.get_workflow_runs, **kwargs)[:limit]:
                runs.append(
                    {
                        "id": run.id,
                        "name": run.name,
                        "status": run.status,
                        "conclusion": run.conclusion,
                        "workflow": run.workflow_id,
                        "created_at": run.created_at.isoformat(),
                        "url": run.html_url,
                    }
                )
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
        if err := _bad_repo(repo):
            return err
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
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_pr_list", "github", repo, {"repo": repo, "state": state})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            prs = []
            for pr in github_call(gh_repo.get_pulls, state=state)[:limit]:
                prs.append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "state": pr.state,
                        "author": pr.user.login if pr.user else None,
                        "base": pr.base.ref,
                        "head": pr.head.ref,
                        "created_at": pr.created_at.isoformat(),
                        "url": pr.html_url,
                    }
                )
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
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_pr_comments", "github", repo, {"repo": repo, "pr_number": pr_number})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(gh_repo.get_pull, pr_number)
            comments = []
            for c in github_call(pr.get_issue_comments):
                comments.append(
                    {
                        "id": c.id,
                        "author": c.user.login if c.user else None,
                        "body": c.body,
                        "created_at": c.created_at.isoformat(),
                        "updated_at": c.updated_at.isoformat(),
                    }
                )
            ac.finish("ok")
            return {"repo": repo, "pr": pr_number, "comments": comments}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_pr_create(
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
        draft: bool = False,
    ) -> dict:
        """Open a pull request on a GitHub repository.

        Args:
            repo: Repository in 'owner/repo' format.
            title: PR title.
            head: Source branch name.
            base: Target branch name.
            body: PR description (optional).
            draft: Create as draft PR (default False).
        """
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx(
            "github_pr_create", "github", repo, {"repo": repo, "head": head, "base": base}
        )
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(
                gh_repo.create_pull,
                base=base,
                head=head,
                title=title,
                body=body or "",
                draft=draft,
            )
            ac.finish("ok")
            return {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "draft": pr.draft,
                "url": pr.html_url,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_pr_get(repo: str, pr_number: int) -> dict:
        """Get details of a GitHub pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
        """
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_pr_get", "github", repo, {"repo": repo, "pr_number": pr_number})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(gh_repo.get_pull, pr_number)
            ac.finish("ok")
            return {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "mergeable": pr.mergeable,
                "merged": pr.merged,
                "draft": pr.draft,
                "head": pr.head.ref,
                "base": pr.base.ref,
                "url": pr.html_url,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                "labels": [lb.name for lb in pr.labels],
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_pr_merge(
        repo: str,
        pr_number: int,
        merge_method: str = "merge",
        commit_title: str | None = None,
    ) -> dict:
        """Merge a GitHub pull request.

        DESTRUCTIVE: Permanently merges the PR branch into the base branch. This tool
        should be HITL gated in scoped-mcp manifests for all agents (same treatment as
        gitea_pr_merge) — that gating is a scoped-mcp manifest change outside this repo.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number to merge.
            merge_method: One of 'merge', 'squash', or 'rebase' (default: merge).
            commit_title: Optional merge commit title.
        """
        if err := _bad_repo(repo):
            return err
        valid_methods = {"merge", "squash", "rebase"}
        if merge_method not in valid_methods:
            return {"error": f"merge_method must be one of: {', '.join(sorted(valid_methods))}"}
        ac = AuditCtx(
            "github_pr_merge",
            "github",
            repo,
            {"repo": repo, "pr_number": pr_number, "merge_method": merge_method},
        )
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(gh_repo.get_pull, pr_number)
            kwargs: dict = {"merge_method": merge_method}
            if commit_title:
                kwargs["commit_title"] = commit_title
            status = github_call(pr.merge, **kwargs)
            ac.finish("ok")
            return {"merged": status.merged, "sha": status.sha, "message": status.message}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

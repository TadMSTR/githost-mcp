"""Gitea tools via httpx."""

from __future__ import annotations

import re

import structlog

from .._providers.gitea_client import (
    gitea_delete,
    gitea_get,
    gitea_get_optional,
    gitea_get_text,
    gitea_patch,
    gitea_post,
    gitea_post_void,
)
from ..audit import AuditCtx, audit_rejection, reject_enum
from ..bootstrap import (
    VISIBILITIES,
    first_bad_segment,
    readback_result,
    readback_warning,
    validate_description,
)
from ..config import get_config
from ..errors import AlreadyExists, InvalidArgument, MissingArgument
from ..security import scrub


def _err(e: Exception) -> dict:
    """Scrub credentials from an exception before returning it to the caller.

    Mirrors github.py / gitlab.py so the credential-isolation invariant holds for Gitea
    tools too (SC-14 / GHOST-8): the audit log is scrubbed independently, but the direct
    tool-return value must not carry a credential-shaped substring either.
    """
    return {"error": scrub(str(e))}


_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
_REPO_FMT_ERR = "repo must be in 'owner/repo' format (alphanumeric, hyphens, underscores, dots)"

# String path segments (tag names, workflow file names) are interpolated into Gitea API
# URLs and sent raw by httpx — unlike GitHub/GitLab which route through PyGithub/
# python-gitlab (those URL-encode segments). Validate before use so an unvalidated value
# can't traverse the path or inject a query string (IV-01). Tags may contain slashes;
# workflow file names may not. `..` is rejected outright.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _bad_tag(tag: str) -> dict | None:
    if ".." in tag or not _TAG_RE.match(tag):
        return {"error": "tag contains characters not allowed in a Gitea API path segment"}
    return None


log = structlog.get_logger(__name__)


# Where a read-shaped request lands on a write-dispatch tool, the rejection names the
# sibling that serves it. Agents reached for read methods here 34 times in a month.
_HINT_GITEA_ISSUE_WRITE = "To read an issue, list issues or read comments, use gitea_issue_read"


def gitea_visibility(data: dict) -> str:
    """Gitea carries two booleans where GitHub carries one string."""
    if data.get("private"):
        return "private"
    if data.get("internal"):
        return "internal"
    return "public"


def gitea_repo_metadata(data: dict) -> dict:
    """The same shape github_repo_get returns, from Gitea's repo representation.

    Gitea's `default_delete_branch_after_merge` is the merge-UI default, the nearest
    thing it has to GitHub's `delete_branch_on_merge`, so it is reported under that name.
    """
    parent = data.get("parent")
    return {
        "exists": True,
        "full_name": data.get("full_name"),
        "visibility": gitea_visibility(data),
        "default_branch": data.get("default_branch"),
        "empty": data.get("empty"),
        "archived": data.get("archived"),
        "fork": data.get("fork"),
        "parent": parent.get("full_name") if isinstance(parent, dict) else None,
        "delete_branch_on_merge": data.get("default_delete_branch_after_merge"),
        "ssh_url": data.get("ssh_url"),
        "html_url": data.get("html_url"),
    }


def register(mcp) -> None:
    @mcp.tool
    async def gitea_create_release(
        repo: str,
        tag: str,
        name: str | None = None,
        body: str | None = None,
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
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_create_release", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_get_release(repo: str, tag: str) -> dict:
        """Get a Gitea release by tag.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name.
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_get_release", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_list_releases(repo: str, limit: int = 10) -> dict:
        """List recent releases for a Gitea repository.

        Args:
            repo: Repository in 'owner/repo' format.
            limit: Max releases to return (default 10, max 100).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_list_releases", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        limit = min(limit, 100)
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_list(repo: str, state: str = "open", limit: int = 20) -> dict:
        """List pull requests on a Gitea repository.

        Args:
            repo: Repository in 'owner/repo' format.
            state: 'open', 'closed', or 'all' (default: open).
            limit: Max PRs to return (default 20, max 100).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection("gitea_pr_list", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR)
        limit = min(limit, 100)
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_create(
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
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
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_pr_create", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_get(repo: str, pr_number: int) -> dict:
        """Get details of a Gitea pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
        """
        if not _REPO_RE.match(repo):
            return audit_rejection("gitea_pr_get", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR)
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_comment(repo: str, pr_number: int, body: str) -> dict:
        """Post a comment on a Gitea pull request.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
            body: Comment text (markdown supported).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_pr_comment", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_merge(
        repo: str,
        pr_number: int,
        merge_style: str = "merge",
        message: str | None = None,
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
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_pr_merge", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        valid_styles = {"merge", "squash", "rebase"}
        if merge_style not in valid_styles:
            return reject_enum(
                "gitea_pr_merge",
                "gitea",
                repo,
                "merge_style",
                merge_style,
                valid_styles,
            )
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx(
            "gitea_pr_merge",
            "gitea",
            repo,
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
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_pr_review(
        repo: str,
        pr_number: int,
        method: str,
        body: str | None = None,
        event: str | None = None,
        review_id: int | None = None,
        message: str | None = None,
    ) -> dict:
        """Read or submit reviews on a Gitea pull request (method-dispatch).

        Methods:
          - get_diff: return the unified diff for the PR (raw `.diff` text).
          - get_files: list changed files (filename, status, additions, deletions).
          - submit_review: post a review. `event` in {APPROVE, REQUEST_CHANGES, COMMENT}
            (APPROVE maps to Gitea's APPROVED); `body` required for REQUEST_CHANGES/COMMENT.
          - dismiss_review: dismiss a submitted review. Requires `review_id` and `message`.

        DESTRUCTIVE methods: submit_review (event=APPROVE or REQUEST_CHANGES) and
        dismiss_review change PR state and must be HITL gated at the (tool, method) level
        in scoped-mcp manifests.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
            method: get_diff | get_files | submit_review | dismiss_review.
            body: Review body markdown (submit_review).
            event: APPROVE, REQUEST_CHANGES, or COMMENT (submit_review).
            review_id: Review ID to dismiss (dismiss_review).
            message: Dismissal reason (dismiss_review).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_pr_review", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        valid = {"get_diff", "get_files", "submit_review", "dismiss_review"}
        if method not in valid:
            return reject_enum(
                "gitea_pr_review",
                "gitea",
                repo,
                "method",
                method,
                valid,
            )
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        base = f"/repos/{owner}/{repo_name}/pulls/{pr_number}"
        ac = AuditCtx(
            "gitea_pr_review",
            "gitea",
            repo,
            {"repo": repo, "pr_number": pr_number, "method": method},
        )
        try:
            if method == "get_diff":
                diff = await gitea_get_text(f"{base}.diff")
                ac.finish("ok")
                return {"repo": repo, "pr": pr_number, "diff": diff}

            if method == "get_files":
                data = await gitea_get(f"{base}/files")
                files = [
                    {
                        "filename": f.get("filename"),
                        "status": f.get("status"),
                        "additions": f.get("additions"),
                        "deletions": f.get("deletions"),
                        "changes": f.get("changes"),
                        "previous_filename": f.get("previous_filename"),
                    }
                    for f in (data if isinstance(data, list) else [])
                ]
                ac.finish("ok")
                return {"repo": repo, "pr": pr_number, "files": files}

            if method == "submit_review":
                event_map = {
                    "APPROVE": "APPROVED",
                    "REQUEST_CHANGES": "REQUEST_CHANGES",
                    "COMMENT": "COMMENT",
                }
                if event is None or event == "":
                    raise MissingArgument("event is required for submit_review")
                if event not in event_map:
                    raise InvalidArgument(f"event must be one of: {', '.join(sorted(event_map))}")
                if event in {"REQUEST_CHANGES", "COMMENT"} and not body:
                    raise MissingArgument(f"body is required when event is {event}")
                result = await gitea_post(
                    f"{base}/reviews", {"event": event_map[event], "body": body or ""}
                )
                ac.finish("ok")
                return {
                    "repo": repo,
                    "pr": pr_number,
                    "review_id": result.get("id"),
                    "state": result.get("state"),
                }

            # dismiss_review
            if review_id is None:
                raise MissingArgument("review_id is required for dismiss_review")
            if not message:
                raise MissingArgument("message is required for dismiss_review")
            await gitea_post(
                f"{base}/reviews/{review_id}/dismissals",
                {"message": message, "priors": False},
            )
            ac.finish("ok")
            return {"repo": repo, "pr": pr_number, "review_id": review_id, "dismissed": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_actions(
        repo: str,
        method: str,
        run_id: int | None = None,
        workflow: str | None = None,
        ref: str | None = None,
        inputs: dict | None = None,
        job_id: int | None = None,
        limit: int = 20,
    ) -> dict:
        """Control Gitea Actions workflow runs (method-dispatch).

        Methods:
          - list_runs: list recent action runs (id, status, conclusion, event, branch).
          - get_run: get a single run by id. Requires `run_id`.
          - list_jobs: list jobs for a run (id, name, status, conclusion). Requires `run_id`.
          - get_job_log: return the raw text log for a job. Requires `job_id`.
          - dispatch_workflow: trigger a workflow_dispatch. Requires `workflow` (file name,
            e.g. 'ci.yml') and `ref`; optional `inputs` dict.
          - rerun_run: re-run all jobs of a run. Requires `run_id`.
          - rerun_failed_jobs: re-run only the failed jobs of a run. Requires `run_id`.

        Gitea 1.26 has no API to cancel a run (only rerun / delete), so cancel is not
        exposed here. DESTRUCTIVE methods (dispatch_workflow, rerun_run, rerun_failed_jobs)
        change CI state and must be HITL gated at the (tool, method) level in scoped-mcp
        manifests.

        Args:
            repo: Repository in 'owner/repo' format.
            method: list_runs | get_run | list_jobs | get_job_log | dispatch_workflow |
                rerun_run | rerun_failed_jobs.
            run_id: Action run ID (get_run, list_jobs, rerun_run, rerun_failed_jobs).
            workflow: Workflow file name (dispatch_workflow).
            ref: Branch or tag to run against (dispatch_workflow).
            inputs: Optional workflow_dispatch inputs mapping (dispatch_workflow).
            job_id: Action job ID (get_job_log).
            limit: Max runs to return for list_runs (default 20, max 100).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection("gitea_actions", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR)
        valid = {
            "list_runs",
            "get_run",
            "list_jobs",
            "get_job_log",
            "dispatch_workflow",
            "rerun_run",
            "rerun_failed_jobs",
        }
        if method not in valid:
            return reject_enum(
                "gitea_actions",
                "gitea",
                repo,
                "method",
                method,
                valid,
            )
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        base = f"/repos/{owner}/{repo_name}/actions"
        ac = AuditCtx("gitea_actions", "gitea", repo, {"repo": repo, "method": method})
        try:
            if method == "list_runs":
                limit = min(limit, 100)
                data = await gitea_get(f"{base}/runs?limit={limit}")
                runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
                out = [
                    {
                        "id": r.get("id"),
                        "status": r.get("status"),
                        "conclusion": r.get("conclusion"),
                        "event": r.get("event"),
                        "head_branch": r.get("head_branch"),
                        "run_number": r.get("run_number"),
                        "title": r.get("display_title"),
                        "url": r.get("html_url"),
                    }
                    for r in runs
                ]
                ac.finish("ok")
                return {"repo": repo, "runs": out}

            if method == "get_run":
                if run_id is None:
                    raise MissingArgument("run_id is required for get_run")
                r = await gitea_get(f"{base}/runs/{run_id}")
                ac.finish("ok")
                return {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "conclusion": r.get("conclusion"),
                    "event": r.get("event"),
                    "head_branch": r.get("head_branch"),
                    "head_sha": r.get("head_sha"),
                    "run_number": r.get("run_number"),
                    "title": r.get("display_title"),
                    "url": r.get("html_url"),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                }

            if method == "list_jobs":
                if run_id is None:
                    raise MissingArgument("run_id is required for list_jobs")
                data = await gitea_get(f"{base}/runs/{run_id}/jobs")
                jobs = data.get("jobs", []) if isinstance(data, dict) else []
                out = [
                    {
                        "id": j.get("id"),
                        "name": j.get("name"),
                        "status": j.get("status"),
                        "conclusion": j.get("conclusion"),
                        "url": j.get("html_url"),
                    }
                    for j in jobs
                ]
                ac.finish("ok")
                return {"repo": repo, "run_id": run_id, "jobs": out}

            if method == "get_job_log":
                if job_id is None:
                    raise MissingArgument("job_id is required for get_job_log")
                text = await gitea_get_text(f"{base}/jobs/{job_id}/logs")
                ac.finish("ok")
                return {"repo": repo, "job_id": job_id, "log": text}

            if method == "dispatch_workflow":
                if not workflow or not ref:
                    raise MissingArgument("workflow and ref are required for dispatch_workflow")
                if not _WORKFLOW_RE.match(workflow):
                    raise ValueError("workflow contains characters not allowed in a path segment")
                await gitea_post_void(
                    f"{base}/workflows/{workflow}/dispatches",
                    {"ref": ref, "inputs": inputs or {}},
                )
                ac.finish("ok")
                return {"repo": repo, "workflow": workflow, "ref": ref, "dispatched": True}

            # rerun_run / rerun_failed_jobs
            if run_id is None:
                raise MissingArgument(f"run_id is required for {method}")
            suffix = "rerun" if method == "rerun_run" else "rerun-failed-jobs"
            await gitea_post_void(f"{base}/runs/{run_id}/{suffix}", {})
            ac.finish("ok")
            return {"repo": repo, "run_id": run_id, method: True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_release_update(
        repo: str,
        tag: str,
        name: str | None = None,
        body: str | None = None,
        draft: bool | None = None,
        prerelease: bool | None = None,
    ) -> dict:
        """Update an existing Gitea release identified by tag.

        Only the fields you pass are changed; omitted fields keep their current values.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name of the release to update.
            name: New release title (unchanged if omitted).
            body: New release notes markdown (unchanged if omitted).
            draft: New draft flag (unchanged if omitted).
            prerelease: New prerelease flag (unchanged if omitted).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_release_update", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        if err := _bad_tag(tag):
            return audit_rejection("gitea_release_update", "gitea", repo, "invalid_argument", err)
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_release_update", "gitea", repo, {"repo": repo, "tag": tag})
        try:
            # Gitea can only PATCH a release by numeric id; resolve the tag first.
            existing = await gitea_get(f"/repos/{owner}/{repo_name}/releases/tags/{tag}")
            rid = existing.get("id")
            data: dict = {}
            if name is not None:
                data["name"] = name
            if body is not None:
                data["body"] = body
            if draft is not None:
                data["draft"] = draft
            if prerelease is not None:
                data["prerelease"] = prerelease
            result = await gitea_patch(f"/repos/{owner}/{repo_name}/releases/{rid}", data)
            ac.finish("ok")
            return {
                "id": result.get("id"),
                "tag": result.get("tag_name"),
                "name": result.get("name"),
                "url": result.get("html_url"),
                "draft": result.get("draft"),
                "prerelease": result.get("prerelease"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_release_delete(repo: str, tag: str) -> dict:
        """Delete a Gitea release by tag.

        DESTRUCTIVE: permanently removes the release (the git tag itself is not deleted).
        Must be HITL gated in scoped-mcp manifests.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name of the release to delete.
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_release_delete", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        if err := _bad_tag(tag):
            return audit_rejection("gitea_release_delete", "gitea", repo, "invalid_argument", err)
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        ac = AuditCtx("gitea_release_delete", "gitea", repo, {"repo": repo, "tag": tag})
        try:
            await gitea_delete(f"/repos/{owner}/{repo_name}/releases/tags/{tag}")
            ac.finish("ok")
            return {"repo": repo, "tag": tag, "deleted": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_issue_read(
        repo: str,
        method: str,
        issue_number: int | None = None,
        state: str = "open",
        limit: int = 20,
    ) -> dict:
        """Read Gitea issues (method-dispatch).

        Methods:
          - get: fetch a single issue by index. Requires `issue_number`.
          - list: list issues by state (pull requests are excluded via type=issues).
          - comments: list comments on an issue. Requires `issue_number`.

        Args:
            repo: Repository in 'owner/repo' format.
            method: get | list | comments.
            issue_number: Issue index (get, comments).
            state: 'open', 'closed', or 'all' for list (default: open).
            limit: Max issues to return for list (default 20, max 100).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_issue_read", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        valid = {"get", "list", "comments"}
        if method not in valid:
            return reject_enum(
                "gitea_issue_read",
                "gitea",
                repo,
                "method",
                method,
                valid,
            )
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        base = f"/repos/{owner}/{repo_name}/issues"
        ac = AuditCtx("gitea_issue_read", "gitea", repo, {"repo": repo, "method": method})
        try:
            if method == "list":
                limit = min(limit, 100)
                data = await gitea_get(f"{base}?state={state}&type=issues&limit={limit}")
                issues = [
                    {
                        "number": i.get("number"),
                        "title": i.get("title"),
                        "state": i.get("state"),
                        "author": i.get("user", {}).get("login") if i.get("user") else None,
                        "labels": [lb.get("name") for lb in (i.get("labels") or [])],
                        "created_at": i.get("created_at"),
                        "url": i.get("html_url"),
                    }
                    for i in (data if isinstance(data, list) else [])
                ]
                ac.finish("ok")
                return {"repo": repo, "issues": issues}

            if issue_number is None:
                raise MissingArgument(f"issue_number is required for {method}")

            if method == "get":
                i = await gitea_get(f"{base}/{issue_number}")
                ac.finish("ok")
                return {
                    "number": i.get("number"),
                    "title": i.get("title"),
                    "state": i.get("state"),
                    "body": i.get("body"),
                    "author": i.get("user", {}).get("login") if i.get("user") else None,
                    "labels": [lb.get("name") for lb in (i.get("labels") or [])],
                    "assignees": [a.get("login") for a in (i.get("assignees") or []) if a],
                    "created_at": i.get("created_at"),
                    "updated_at": i.get("updated_at"),
                    "url": i.get("html_url"),
                }

            # comments
            data = await gitea_get(f"{base}/{issue_number}/comments")
            comments = [
                {
                    "id": c.get("id"),
                    "author": c.get("user", {}).get("login") if c.get("user") else None,
                    "body": c.get("body"),
                    "created_at": c.get("created_at"),
                }
                for c in (data if isinstance(data, list) else [])
            ]
            ac.finish("ok")
            return {"repo": repo, "issue": issue_number, "comments": comments}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_issue_write(
        repo: str,
        method: str,
        issue_number: int | None = None,
        title: str | None = None,
        body: str | None = None,
        labels: list[int] | None = None,
        assignees: list[str] | None = None,
        comment: str | None = None,
    ) -> dict:
        """Create or modify Gitea issues (method-dispatch).

        Methods:
          - create: open a new issue. Requires `title`; optional body/labels/assignees.
          - update: change title/body of an issue. Requires `issue_number`.
          - add_comment: post a comment. Requires `issue_number` and `comment`.
          - close: close an issue. Requires `issue_number`.
          - reopen: reopen a closed issue. Requires `issue_number`.

        `close` is state-changing and should be HITL gated at the (tool, method) level in
        scoped-mcp manifests. Gitea labels are numeric IDs (not names); assignees are
        usernames.

        Args:
            repo: Repository in 'owner/repo' format.
            method: create | update | add_comment | close | reopen.
            issue_number: Issue index (update, add_comment, close, reopen).
            title: Issue title (create; optional for update).
            body: Issue body markdown (create; optional for update).
            labels: Label IDs to set at create time (optional).
            assignees: Assignee usernames to set at create time (optional).
            comment: Comment body (add_comment).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_issue_write", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        valid = {"create", "update", "add_comment", "close", "reopen"}
        if method not in valid:
            return reject_enum(
                "gitea_issue_write",
                "gitea",
                repo,
                "method",
                method,
                valid,
                hint=_HINT_GITEA_ISSUE_WRITE,
            )
        config = get_config()
        owner = repo.split("/")[0] if "/" in repo else config.gitea_owner
        repo_name = repo.split("/")[-1]
        base = f"/repos/{owner}/{repo_name}/issues"
        ac = AuditCtx("gitea_issue_write", "gitea", repo, {"repo": repo, "method": method})
        try:
            if method == "create":
                if not title:
                    raise MissingArgument("title is required for create")
                data: dict = {"title": title, "body": body or ""}
                if labels:
                    data["labels"] = labels
                if assignees:
                    data["assignees"] = assignees
                result = await gitea_post(base, data)
                ac.finish("ok")
                return {
                    "number": result.get("number"),
                    "title": result.get("title"),
                    "state": result.get("state"),
                    "url": result.get("html_url"),
                }

            if issue_number is None:
                raise MissingArgument(f"issue_number is required for {method}")

            if method == "update":
                data = {}
                if title is not None:
                    data["title"] = title
                if body is not None:
                    data["body"] = body
                if not data:
                    raise MissingArgument("update requires at least one of title or body")
                await gitea_patch(f"{base}/{issue_number}", data)
                ac.finish("ok")
                return {"repo": repo, "number": issue_number, "updated": True}

            if method == "add_comment":
                if not comment:
                    raise MissingArgument("comment is required for add_comment")
                result = await gitea_post(f"{base}/{issue_number}/comments", {"body": comment})
                ac.finish("ok")
                return {
                    "repo": repo,
                    "issue": issue_number,
                    "comment_id": result.get("id"),
                    "url": result.get("html_url"),
                }

            # close / reopen
            new_state = "closed" if method == "close" else "open"
            await gitea_patch(f"{base}/{issue_number}", {"state": new_state})
            ac.finish("ok")
            return {"repo": repo, "number": issue_number, "state": new_state}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_repo_get(repo: str) -> dict:
        """Read a repo's metadata on the configured Gitea (GITEA_URL).

        Read-only. A repo that does not exist, or that this token cannot see, returns
        {"exists": false} rather than an error.

        Returns full_name, visibility (public/private/internal), default_branch, empty,
        archived, fork, parent, delete_branch_on_merge (Gitea's merge-UI default),
        ssh_url, html_url.

        Args:
            repo: Repository in 'owner/repo' format.
        """
        owner, _, name = repo.partition("/")
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "gitea_repo_get", "gitea", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        if bad := first_bad_segment((("owner", owner), ("repo", name))):
            reason_class, argument, value, message = bad
            return audit_rejection(
                "gitea_repo_get",
                "gitea",
                repo,
                reason_class,
                message,
                argument=argument,
                value=value,
            )
        ac = AuditCtx("gitea_repo_get", "gitea", repo, {"repo": repo})
        try:
            data = await gitea_get_optional(f"/repos/{owner}/{name}")
            result = (
                {"exists": False, "full_name": repo} if data is None else gitea_repo_metadata(data)
            )
            ac.finish("ok")
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

    @mcp.tool
    async def gitea_repo_create(
        owner: str, name: str, visibility: str, description: str | None = None
    ) -> dict:
        """Create an EMPTY repo in an organization on our Gitea. HITL gated.

        `owner` must be an organization: the Gitea token cannot create repos under a
        user account. `visibility` is required and has no default: public or private.
        There is no tool to change it later or to delete the repo; both are operator
        steps. Note that a public repo in a private org is still visible only to the
        org's members.

        Created with no initial commit, ready for an existing local repo to be pushed.
        An existing name is refused before anything is created. The returned
        visibility is read back from Gitea, not echoed from the argument.

        Args:
            owner: Gitea organization, e.g. host-forge or agent-platform.
            name: Repo name, [A-Za-z0-9_.-], without '.git'.
            visibility: public | private.
            description: Optional, up to 350 characters.
        """
        full_name = f"{owner}/{name}"
        if visibility not in VISIBILITIES:
            return reject_enum(
                "gitea_repo_create", "gitea", full_name, "visibility", visibility, VISIBILITIES
            )
        if bad := first_bad_segment((("owner", owner), ("name", name))):
            reason_class, argument, value, message = bad
            return audit_rejection(
                "gitea_repo_create",
                "gitea",
                full_name,
                reason_class,
                message,
                argument=argument,
                value=value,
            )
        try:
            validate_description(description)
        except InvalidArgument as e:
            return audit_rejection(
                "gitea_repo_create", "gitea", full_name, "invalid_argument", str(e)
            )
        params = {"repo": full_name, "visibility": visibility, "description": description}
        ac = AuditCtx("gitea_repo_create", "gitea", full_name, params)
        try:
            # An explicit org check, so a user name gets a clear refusal rather than a
            # bare 404 from the create endpoint.
            if await gitea_get_optional(f"/orgs/{owner}") is None:
                raise InvalidArgument(
                    f"'{owner}' is not an organization on this Gitea (or not visible to the "
                    "token). gitea_repo_create creates repos in organizations only."
                )
            if await gitea_get_optional(f"/repos/{owner}/{name}") is not None:
                raise AlreadyExists(f"{full_name} already exists; nothing was created")
            data = {"name": name, "private": visibility == "private", "auto_init": False}
            if description:
                data["description"] = description
            created = await gitea_post(f"/orgs/{owner}/repos", data)
            reported = empty = readback_error = None
            try:
                readback = await gitea_get_optional(f"/repos/{owner}/{name}")
                if readback is not None:
                    reported = gitea_visibility(readback)
                    empty = readback.get("empty")
            except Exception as e:
                readback_error = scrub(str(e))
            result = {
                "created": True,
                "full_name": created.get("full_name", full_name),
                "requested_visibility": visibility,
                "visibility": reported,
                "empty": empty,
                "ssh_url": created.get("ssh_url"),
                "html_url": created.get("html_url"),
            }
            warning = readback_warning("Gitea", visibility, reported, readback_error)
            if warning:
                result["warning"] = warning
            ac.finish(readback_result(reported, warning), warning)
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return _err(e)

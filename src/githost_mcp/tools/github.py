"""GitHub tools via PyGithub (10 tools)."""

from __future__ import annotations

import re

import structlog

from .._providers.github_client import get_github, github_call
from ..audit import AuditCtx
from ..security import scrub

log = structlog.get_logger(__name__)

# GitHub full names are always exactly 'owner/repo' (one slash, no numeric-ID form).
_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")
_REPO_FMT_ERR = "repo must be in 'owner/repo' format (alphanumeric, hyphens, underscores, dots)"


def _err(e: Exception) -> dict:
    return {"error": scrub(str(e))}


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

    @mcp.tool
    def github_pr_review(
        repo: str,
        pr_number: int,
        method: str,
        body: str | None = None,
        event: str | None = None,
        review_id: int | None = None,
        message: str | None = None,
    ) -> dict:
        """Read or submit reviews on a GitHub pull request (method-dispatch).

        Methods:
          - get_diff: return the unified diff for the PR (raw `git diff` text).
          - get_files: list changed files (filename, status, additions, deletions, patch).
          - get_reviews: list submitted reviews (id, author, state, body).
          - submit_review: post a review. `event` in {APPROVE, REQUEST_CHANGES, COMMENT};
            `body` required for REQUEST_CHANGES/COMMENT.
          - dismiss_review: dismiss a submitted review. Requires `review_id` and `message`.

        DESTRUCTIVE methods: submit_review (event=APPROVE or REQUEST_CHANGES) and
        dismiss_review change PR state and must be HITL gated at the (tool, method) level
        in scoped-mcp manifests. get_diff/get_files/get_reviews and a COMMENT-only
        submit_review are read/comment-only.

        Args:
            repo: Repository in 'owner/repo' format.
            pr_number: Pull request number.
            method: get_diff | get_files | get_reviews | submit_review | dismiss_review.
            body: Review body markdown (submit_review).
            event: APPROVE, REQUEST_CHANGES, or COMMENT (submit_review).
            review_id: Review ID to dismiss (dismiss_review).
            message: Dismissal reason (dismiss_review).
        """
        if err := _bad_repo(repo):
            return err
        valid = {"get_diff", "get_files", "get_reviews", "submit_review", "dismiss_review"}
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx(
            "github_pr_review",
            "github",
            repo,
            {"repo": repo, "pr_number": pr_number, "method": method},
        )
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            pr = github_call(gh_repo.get_pull, pr_number)

            if method == "get_diff":
                # PyGithub has no typed diff accessor; the requester fetches the raw
                # unified diff via the diff media type. Returns (status, headers, body).
                _status, _headers, data = pr._requester.requestBlob(
                    "GET", pr.url, headers={"Accept": "application/vnd.github.v3.diff"}
                )
                ac.finish("ok")
                return {"repo": repo, "pr": pr_number, "diff": data}

            if method == "get_files":
                files = [
                    {
                        "filename": f.filename,
                        "status": f.status,
                        "additions": f.additions,
                        "deletions": f.deletions,
                        "changes": f.changes,
                        "patch": f.patch,
                        "previous_filename": getattr(f, "previous_filename", None),
                    }
                    for f in github_call(pr.get_files)
                ]
                ac.finish("ok")
                return {"repo": repo, "pr": pr_number, "files": files}

            if method == "get_reviews":
                reviews = [
                    {
                        "id": r.id,
                        "user": r.user.login if r.user else None,
                        "state": r.state,
                        "body": r.body,
                        "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                    }
                    for r in github_call(pr.get_reviews)
                ]
                ac.finish("ok")
                return {"repo": repo, "pr": pr_number, "reviews": reviews}

            if method == "submit_review":
                valid_events = {"APPROVE", "REQUEST_CHANGES", "COMMENT"}
                if event not in valid_events:
                    raise ValueError(f"event must be one of: {', '.join(sorted(valid_events))}")
                if event in {"REQUEST_CHANGES", "COMMENT"} and not body:
                    raise ValueError(f"body is required when event is {event}")
                review = github_call(pr.create_review, body=body or "", event=event)
                ac.finish("ok")
                return {
                    "repo": repo,
                    "pr": pr_number,
                    "review_id": review.id,
                    "state": review.state,
                    "event": event,
                }

            # dismiss_review
            if review_id is None:
                raise ValueError("review_id is required for dismiss_review")
            if not message:
                raise ValueError("message is required for dismiss_review")
            review = github_call(pr.get_review, review_id)
            github_call(review.dismiss, message)
            ac.finish("ok")
            return {"repo": repo, "pr": pr_number, "review_id": review_id, "dismissed": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_actions(
        repo: str,
        method: str,
        workflow: str | None = None,
        ref: str | None = None,
        run_id: int | None = None,
        inputs: dict | None = None,
    ) -> dict:
        """Control GitHub Actions workflow runs (method-dispatch).

        Methods:
          - run_workflow: trigger a workflow_dispatch. Requires `workflow` (id or file
            name, e.g. 'ci.yml') and `ref` (branch/tag); optional `inputs` dict.
          - rerun_workflow: re-run all jobs of a run. Requires `run_id`.
          - rerun_failed_jobs: re-run only the failed jobs of a run. Requires `run_id`.
          - cancel_run: cancel an in-progress run. Requires `run_id`.
          - get_run_logs: return the job breakdown for a run (name/status/conclusion/url).
            GitHub's raw logs are a downloadable zip archive, not inline text — this
            returns the per-job status view instead. Requires `run_id`.

        Read-only workflow listing/status stays in github_workflow_list /
        github_workflow_status. DESTRUCTIVE methods (run_workflow, rerun_workflow,
        rerun_failed_jobs, cancel_run) change CI state and must be HITL gated at the
        (tool, method) level in scoped-mcp manifests.

        Args:
            repo: Repository in 'owner/repo' format.
            method: run_workflow | rerun_workflow | rerun_failed_jobs | cancel_run |
                get_run_logs.
            workflow: Workflow id or file name (run_workflow).
            ref: Branch or tag to run against (run_workflow).
            run_id: Workflow run ID (rerun_workflow, rerun_failed_jobs, cancel_run,
                get_run_logs).
            inputs: Optional workflow_dispatch inputs mapping (run_workflow).
        """
        if err := _bad_repo(repo):
            return err
        valid = {
            "run_workflow",
            "rerun_workflow",
            "rerun_failed_jobs",
            "cancel_run",
            "get_run_logs",
        }
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx("github_actions", "github", repo, {"repo": repo, "method": method})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)

            if method == "run_workflow":
                if not workflow or not ref:
                    raise ValueError("workflow and ref are required for run_workflow")
                wf = github_call(gh_repo.get_workflow, workflow)
                if inputs:
                    created = github_call(wf.create_dispatch, ref, inputs)
                else:
                    created = github_call(wf.create_dispatch, ref)
                ac.finish("ok")
                return {"repo": repo, "workflow": workflow, "ref": ref, "dispatched": bool(created)}

            if run_id is None:
                raise ValueError(f"run_id is required for {method}")
            run = github_call(gh_repo.get_workflow_run, run_id)

            if method == "rerun_workflow":
                github_call(run.rerun)
                ac.finish("ok")
                return {"repo": repo, "run_id": run_id, "rerun": True}
            if method == "rerun_failed_jobs":
                github_call(run.rerun_failed_jobs)
                ac.finish("ok")
                return {"repo": repo, "run_id": run_id, "rerun_failed_jobs": True}
            if method == "cancel_run":
                github_call(run.cancel)
                ac.finish("ok")
                return {"repo": repo, "run_id": run_id, "cancelled": True}

            # get_run_logs
            jobs = [
                {
                    "id": j.id,
                    "name": j.name,
                    "status": j.status,
                    "conclusion": j.conclusion,
                    "url": j.html_url,
                }
                for j in github_call(run.jobs)
            ]
            ac.finish("ok")
            return {
                "repo": repo,
                "run_id": run_id,
                "jobs": jobs,
                "note": "GitHub Actions raw logs are a zip archive; use each job's url to view.",
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_release_update(
        repo: str,
        tag: str,
        name: str | None = None,
        body: str | None = None,
        draft: bool | None = None,
        prerelease: bool | None = None,
    ) -> dict:
        """Update an existing GitHub release identified by tag.

        Only the fields you pass are changed; omitted fields keep their current values.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name of the release to update.
            name: New release title (unchanged if omitted).
            body: New release notes markdown (unchanged if omitted).
            draft: New draft flag (unchanged if omitted).
            prerelease: New prerelease flag (unchanged if omitted).
        """
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_release_update", "github", repo, {"repo": repo, "tag": tag})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            release = github_call(gh_repo.get_release, tag)
            updated = github_call(
                release.update_release,
                name if name is not None else release.title,
                body if body is not None else (release.body or ""),
                draft if draft is not None else release.draft,
                prerelease if prerelease is not None else release.prerelease,
            )
            ac.finish("ok")
            return {
                "id": updated.id,
                "tag": updated.tag_name,
                "name": updated.title,
                "url": updated.html_url,
                "draft": updated.draft,
                "prerelease": updated.prerelease,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_release_delete(repo: str, tag: str) -> dict:
        """Delete a GitHub release by tag.

        DESTRUCTIVE: permanently removes the release (the git tag itself is not deleted).
        Must be HITL gated in scoped-mcp manifests.

        Args:
            repo: Repository in 'owner/repo' format.
            tag: Tag name of the release to delete.
        """
        if err := _bad_repo(repo):
            return err
        ac = AuditCtx("github_release_delete", "github", repo, {"repo": repo, "tag": tag})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)
            release = github_call(gh_repo.get_release, tag)
            github_call(release.delete_release)
            ac.finish("ok")
            return {"repo": repo, "tag": tag, "deleted": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_issue_read(
        repo: str,
        method: str,
        issue_number: int | None = None,
        state: str = "open",
        limit: int = 20,
    ) -> dict:
        """Read GitHub issues (method-dispatch).

        Methods:
          - get: fetch a single issue by number. Requires `issue_number`.
          - list: list issues by state (pull requests are excluded).
          - comments: list comments on an issue. Requires `issue_number`.

        Args:
            repo: Repository in 'owner/repo' format.
            method: get | list | comments.
            issue_number: Issue number (get, comments).
            state: 'open', 'closed', or 'all' for list (default: open).
            limit: Max issues to return for list (default 20).
        """
        if err := _bad_repo(repo):
            return err
        valid = {"get", "list", "comments"}
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx("github_issue_read", "github", repo, {"repo": repo, "method": method})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)

            if method == "list":
                issues = []
                for i in github_call(gh_repo.get_issues, state=state)[:limit]:
                    if i.pull_request:  # get_issues includes PRs; skip them
                        continue
                    issues.append(
                        {
                            "number": i.number,
                            "title": i.title,
                            "state": i.state,
                            "author": i.user.login if i.user else None,
                            "labels": [lb.name for lb in i.labels],
                            "created_at": i.created_at.isoformat() if i.created_at else None,
                            "url": i.html_url,
                        }
                    )
                ac.finish("ok")
                return {"repo": repo, "issues": issues}

            if issue_number is None:
                raise ValueError(f"issue_number is required for {method}")
            issue = github_call(gh_repo.get_issue, issue_number)

            if method == "get":
                ac.finish("ok")
                return {
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "body": issue.body,
                    "author": issue.user.login if issue.user else None,
                    "labels": [lb.name for lb in issue.labels],
                    "assignees": [a.login for a in issue.assignees],
                    "created_at": issue.created_at.isoformat() if issue.created_at else None,
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                    "url": issue.html_url,
                }

            # comments
            comments = [
                {
                    "id": c.id,
                    "author": c.user.login if c.user else None,
                    "body": c.body,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in github_call(issue.get_comments)
            ]
            ac.finish("ok")
            return {"repo": repo, "issue": issue_number, "comments": comments}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def github_issue_write(
        repo: str,
        method: str,
        issue_number: int | None = None,
        title: str | None = None,
        body: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
        comment: str | None = None,
    ) -> dict:
        """Create or modify GitHub issues (method-dispatch).

        Methods:
          - create: open a new issue. Requires `title`; optional body/labels/assignees.
          - update: change title/body of an issue. Requires `issue_number`.
          - add_comment: post a comment. Requires `issue_number` and `comment`.
          - close: close an issue. Requires `issue_number`.
          - reopen: reopen a closed issue. Requires `issue_number`.

        `close` is state-changing and should be HITL gated at the (tool, method) level in
        scoped-mcp manifests. labels/assignees use GitHub names/logins.

        Args:
            repo: Repository in 'owner/repo' format.
            method: create | update | add_comment | close | reopen.
            issue_number: Issue number (update, add_comment, close, reopen).
            title: Issue title (create; optional for update).
            body: Issue body markdown (create; optional for update).
            labels: Label names to set at create time (optional).
            assignees: Assignee logins to set at create time (optional).
            comment: Comment body (add_comment).
        """
        if err := _bad_repo(repo):
            return err
        valid = {"create", "update", "add_comment", "close", "reopen"}
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx("github_issue_write", "github", repo, {"repo": repo, "method": method})
        try:
            gh = get_github()
            gh_repo = github_call(gh.get_repo, repo)

            if method == "create":
                if not title:
                    raise ValueError("title is required for create")
                kwargs: dict = {"title": title}
                if body is not None:
                    kwargs["body"] = body
                if labels:
                    kwargs["labels"] = labels
                if assignees:
                    kwargs["assignees"] = assignees
                issue = github_call(gh_repo.create_issue, **kwargs)
                ac.finish("ok")
                return {
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "url": issue.html_url,
                }

            if issue_number is None:
                raise ValueError(f"issue_number is required for {method}")
            issue = github_call(gh_repo.get_issue, issue_number)

            if method == "update":
                kwargs = {}
                if title is not None:
                    kwargs["title"] = title
                if body is not None:
                    kwargs["body"] = body
                if not kwargs:
                    raise ValueError("update requires at least one of title or body")
                github_call(issue.edit, **kwargs)
                ac.finish("ok")
                return {"repo": repo, "number": issue_number, "updated": True}

            if method == "add_comment":
                if not comment:
                    raise ValueError("comment is required for add_comment")
                c = github_call(issue.create_comment, comment)
                ac.finish("ok")
                return {"repo": repo, "issue": issue_number, "comment_id": c.id, "url": c.html_url}

            if method == "close":
                github_call(issue.edit, state="closed")
                ac.finish("ok")
                return {"repo": repo, "number": issue_number, "state": "closed"}

            # reopen
            github_call(issue.edit, state="open")
            ac.finish("ok")
            return {"repo": repo, "number": issue_number, "state": "open"}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

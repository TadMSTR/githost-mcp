"""GitLab tools via python-gitlab (7 tools)."""

from __future__ import annotations

import re

import structlog

from .._providers.gitlab_client import get_gitlab, gitlab_call
from ..audit import AuditCtx
from ..security import mask_credentials

log = structlog.get_logger(__name__)

# GitLab projects can be nested ('group/subgroup/project', one or more slashes) or a bare
# numeric project ID — a single-slash 'owner/repo' regex would reject both valid forms.
_PROJECT_RE = re.compile(r"^([a-zA-Z0-9_.-]+/)+[a-zA-Z0-9_.-]+$|^\d+$")
_PROJECT_FMT_ERR = (
    "project must be 'namespace/project' (nested groups allowed) or a numeric project ID"
)


def _err(e: Exception) -> dict:
    return {"error": mask_credentials(str(e))}


def _bad_project(project: str) -> dict | None:
    """Return an error dict if `project` is not a valid GitLab project identifier, else None.

    Defense-in-depth: `project` reaches python-gitlab's projects.get() and is used to build
    API paths. python-gitlab URL-encodes segments so traversal isn't observed, but validating
    here rejects malformed input before it reaches the client library. Accepts nested group
    paths and numeric project IDs (both valid GitLab forms).
    """
    return None if _PROJECT_RE.match(project) else {"error": _PROJECT_FMT_ERR}


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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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
        if err := _bad_project(project):
            return err
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

    @mcp.tool
    def gitlab_mr_review(project: str, mr_iid: int, method: str) -> dict:
        """Review operations on a GitLab merge request (method-dispatch).

        Methods:
          - get_diffs: return per-file diffs for the MR.
          - get_changed_files: list changed file paths with new/renamed/deleted flags.
          - approve: approve the MR.
          - unapprove: remove your approval from the MR.
          - get_approval_state: return approvals required/left and approver usernames.

        DESTRUCTIVE methods: approve and unapprove change MR approval state and must be
        HITL gated at the (tool, method) level in scoped-mcp manifests. get_diffs,
        get_changed_files, and get_approval_state are read-only.

        Args:
            project: Project in 'namespace/project' format (or numeric ID).
            mr_iid: Merge request internal ID (iid), not the global id.
            method: get_diffs | get_changed_files | approve | unapprove | get_approval_state.
        """
        if err := _bad_project(project):
            return err
        valid = {"get_diffs", "get_changed_files", "approve", "unapprove", "get_approval_state"}
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx(
            "gitlab_mr_review",
            "gitlab",
            project,
            {"project": project, "mr_iid": mr_iid, "method": method},
        )
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            mr = gitlab_call(proj.mergerequests.get, mr_iid)

            if method in {"get_diffs", "get_changed_files"}:
                changes = gitlab_call(mr.changes)
                raw = changes.get("changes", []) if isinstance(changes, dict) else []
                if method == "get_diffs":
                    diffs = [
                        {
                            "old_path": c.get("old_path"),
                            "new_path": c.get("new_path"),
                            "diff": c.get("diff"),
                            "new_file": c.get("new_file"),
                            "renamed_file": c.get("renamed_file"),
                            "deleted_file": c.get("deleted_file"),
                        }
                        for c in raw
                    ]
                    ac.finish("ok")
                    return {"project": project, "mr_iid": mr_iid, "diffs": diffs}
                files = [
                    {
                        "path": c.get("new_path"),
                        "new_file": c.get("new_file"),
                        "renamed_file": c.get("renamed_file"),
                        "deleted_file": c.get("deleted_file"),
                    }
                    for c in raw
                ]
                ac.finish("ok")
                return {"project": project, "mr_iid": mr_iid, "files": files}

            if method == "approve":
                gitlab_call(mr.approve)
                ac.finish("ok")
                return {"project": project, "mr_iid": mr_iid, "approved": True}

            if method == "unapprove":
                gitlab_call(mr.unapprove)
                ac.finish("ok")
                return {"project": project, "mr_iid": mr_iid, "approved": False}

            # get_approval_state
            approvals = gitlab_call(mr.approvals.get)
            approved_by = [
                a.get("user", {}).get("username")
                for a in (getattr(approvals, "approved_by", None) or [])
            ]
            ac.finish("ok")
            return {
                "project": project,
                "mr_iid": mr_iid,
                "approvals_required": getattr(approvals, "approvals_required", None),
                "approvals_left": getattr(approvals, "approvals_left", None),
                "approved_by": approved_by,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_pipeline(
        project: str,
        method: str,
        pipeline_id: int | None = None,
        ref: str | None = None,
        job_id: int | None = None,
        limit: int = 20,
    ) -> dict:
        """Control GitLab CI pipelines (method-dispatch).

        Methods:
          - list: list recent pipelines (id, status, ref, sha, web_url).
          - get: get a single pipeline by id. Requires `pipeline_id`.
          - create: trigger a new pipeline on `ref`. Requires `ref`.
          - retry: retry failed/cancelled jobs of a pipeline. Requires `pipeline_id`.
          - cancel: cancel a running pipeline. Requires `pipeline_id`.
          - get_job_log: return the raw trace log for a job. Requires `job_id`.

        DESTRUCTIVE methods (create, retry, cancel) change CI state and must be HITL gated
        at the (tool, method) level in scoped-mcp manifests. list/get/get_job_log are
        read-only.

        Args:
            project: Project in 'namespace/project' format (or numeric ID).
            method: list | get | create | retry | cancel | get_job_log.
            pipeline_id: Pipeline ID (get, retry, cancel).
            ref: Branch or tag to run against (create).
            job_id: Job ID (get_job_log).
            limit: Max pipelines to return for list (default 20).
        """
        if err := _bad_project(project):
            return err
        valid = {"list", "get", "create", "retry", "cancel", "get_job_log"}
        if method not in valid:
            return {"error": f"method must be one of: {', '.join(sorted(valid))}"}
        ac = AuditCtx("gitlab_pipeline", "gitlab", project, {"project": project, "method": method})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)

            if method == "list":
                pipelines = [
                    {
                        "id": p.id,
                        "status": p.status,
                        "ref": p.ref,
                        "sha": p.sha,
                        "web_url": p.web_url,
                    }
                    for p in gitlab_call(proj.pipelines.list, get_all=False)[:limit]
                ]
                ac.finish("ok")
                return {"project": project, "pipelines": pipelines}

            if method == "create":
                if not ref:
                    raise ValueError("ref is required for create")
                pipe = gitlab_call(proj.pipelines.create, {"ref": ref})
                ac.finish("ok")
                return {
                    "project": project,
                    "id": pipe.id,
                    "status": pipe.status,
                    "ref": pipe.ref,
                    "web_url": pipe.web_url,
                }

            if method == "get_job_log":
                if job_id is None:
                    raise ValueError("job_id is required for get_job_log")
                job = gitlab_call(proj.jobs.get, job_id)
                trace = gitlab_call(job.trace)
                if isinstance(trace, bytes):
                    trace = trace.decode("utf-8", errors="replace")
                ac.finish("ok")
                return {"project": project, "job_id": job_id, "log": trace}

            # get / retry / cancel all need a pipeline_id
            if pipeline_id is None:
                raise ValueError(f"pipeline_id is required for {method}")
            pipe = gitlab_call(proj.pipelines.get, pipeline_id)

            if method == "get":
                ac.finish("ok")
                return {
                    "project": project,
                    "id": pipe.id,
                    "status": pipe.status,
                    "ref": pipe.ref,
                    "sha": pipe.sha,
                    "web_url": pipe.web_url,
                }
            if method == "retry":
                gitlab_call(pipe.retry)
                ac.finish("ok")
                return {"project": project, "id": pipeline_id, "retried": True}
            # cancel
            gitlab_call(pipe.cancel)
            ac.finish("ok")
            return {"project": project, "id": pipeline_id, "cancelled": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_release_update(
        project: str,
        tag: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Update an existing GitLab release identified by tag.

        Only the fields you pass are changed; omitted fields keep their current values.

        Args:
            project: Project in 'namespace/project' format (or numeric ID).
            tag: Tag name of the release to update.
            name: New release name (unchanged if omitted).
            description: New release notes markdown (unchanged if omitted).
        """
        if err := _bad_project(project):
            return err
        ac = AuditCtx("gitlab_release_update", "gitlab", project, {"project": project, "tag": tag})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            release = gitlab_call(proj.releases.get, tag)
            if name is not None:
                release.name = name
            if description is not None:
                release.description = description
            gitlab_call(release.save)
            ac.finish("ok")
            return {
                "tag": release.tag_name,
                "name": release.name,
                "description": release.description,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

    @mcp.tool
    def gitlab_release_delete(project: str, tag: str) -> dict:
        """Delete a GitLab release by tag.

        DESTRUCTIVE: permanently removes the release (the git tag itself is not deleted).
        Must be HITL gated in scoped-mcp manifests.

        Args:
            project: Project in 'namespace/project' format (or numeric ID).
            tag: Tag name of the release to delete.
        """
        if err := _bad_project(project):
            return err
        ac = AuditCtx("gitlab_release_delete", "gitlab", project, {"project": project, "tag": tag})
        try:
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, project)
            gitlab_call(proj.releases.delete, tag)
            ac.finish("ok")
            return {"project": project, "tag": tag, "deleted": True}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return _err(e)

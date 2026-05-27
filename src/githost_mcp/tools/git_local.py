"""Local git operations via gitpython (no subprocess)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import git
import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..security import validate_write_path

log = structlog.get_logger(__name__)


def _open_repo(repo_path: str) -> git.Repo:
    try:
        return git.Repo(repo_path, search_parent_directories=False)
    except git.InvalidGitRepositoryError:
        raise ValueError(f"Not a git repository: {repo_path}")
    except git.NoSuchPathError:
        raise ValueError(f"Path does not exist: {repo_path}")


def register(mcp) -> None:
    @mcp.tool
    def git_status(repo_path: str) -> dict:
        """Working tree status: staged, unstaged, and untracked files.

        Args:
            repo_path: Absolute path to the local git repository.
        """
        ac = AuditCtx("git_status", "local", repo_path, {"repo_path": repo_path})
        try:
            repo = _open_repo(repo_path)
            staged = [item.a_path for item in repo.index.diff("HEAD")] if repo.head.is_valid() else []
            unstaged = [item.a_path for item in repo.index.diff(None)]
            untracked = repo.untracked_files
            result = {
                "repo": repo_path,
                "branch": repo.active_branch.name if not repo.head.is_detached else "HEAD (detached)",
                "staged": staged,
                "unstaged": unstaged,
                "untracked": list(untracked),
                "is_dirty": repo.is_dirty(untracked_files=True),
            }
            ac.finish("ok")
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_diff(repo_path: str, staged: bool = False, file_path: Optional[str] = None) -> dict:
        """Show diff — staged or unstaged, optionally for a specific file.

        Args:
            repo_path: Absolute path to the local git repository.
            staged: If True, show staged diff (vs HEAD). Default False (unstaged).
            file_path: Optional file path to diff (relative to repo root).
        """
        ac = AuditCtx("git_diff", "local", repo_path, {"repo_path": repo_path, "staged": staged, "file_path": file_path})
        try:
            repo = _open_repo(repo_path)
            kwargs = {}
            if file_path:
                kwargs["paths"] = [file_path]
            if staged:
                diffs = repo.index.diff("HEAD", **kwargs) if repo.head.is_valid() else []
            else:
                diffs = repo.index.diff(None, **kwargs)

            patches = []
            for d in diffs:
                try:
                    patches.append({
                        "file": d.a_path,
                        "change_type": d.change_type,
                        "diff": d.diff.decode("utf-8", errors="replace") if d.diff else "",
                    })
                except Exception:
                    patches.append({"file": d.a_path, "change_type": d.change_type, "diff": ""})

            ac.finish("ok")
            return {"repo": repo_path, "staged": staged, "patches": patches}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_log(repo_path: str, limit: int = 20, branch: Optional[str] = None) -> dict:
        """Recent commit log with author, date, and message.

        Args:
            repo_path: Absolute path to the local git repository.
            limit: Max commits to return (default 20).
            branch: Branch or ref to log (default: active branch).
        """
        ac = AuditCtx("git_log", "local", repo_path, {"repo_path": repo_path, "limit": limit})
        try:
            repo = _open_repo(repo_path)
            ref = branch or repo.active_branch.name
            commits = []
            for c in repo.iter_commits(ref, max_count=limit):
                commits.append({
                    "sha": c.hexsha[:12],
                    "author": f"{c.author.name} <{c.author.email}>",
                    "date": c.authored_datetime.isoformat(),
                    "message": c.message.strip(),
                })
            ac.finish("ok")
            return {"repo": repo_path, "branch": ref, "commits": commits}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_show(repo_path: str, ref: str) -> dict:
        """Inspect a specific commit or object.

        Args:
            repo_path: Absolute path to the local git repository.
            ref: Commit SHA, tag, or branch name to inspect.
        """
        ac = AuditCtx("git_show", "local", repo_path, {"repo_path": repo_path, "ref": ref})
        try:
            repo = _open_repo(repo_path)
            commit = repo.commit(ref)
            ac.finish("ok")
            return {
                "sha": commit.hexsha,
                "author": f"{commit.author.name} <{commit.author.email}>",
                "date": commit.authored_datetime.isoformat(),
                "message": commit.message.strip(),
                "stats": {
                    "files_changed": len(commit.stats.files),
                    "insertions": commit.stats.total["insertions"],
                    "deletions": commit.stats.total["deletions"],
                },
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_branch(
        repo_path: str,
        action: str = "list",
        branch_name: Optional[str] = None,
    ) -> dict:
        """List, create, or delete branches.

        Args:
            repo_path: Absolute path to the local git repository.
            action: 'list', 'create', or 'delete'.
            branch_name: Branch name for create/delete actions.
        """
        params = {"repo_path": repo_path, "action": action, "branch_name": branch_name}
        ac = AuditCtx("git_branch", "local", repo_path, params)
        try:
            if action in ("create", "delete"):
                validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            if action == "list":
                branches = [b.name for b in repo.branches]
                ac.finish("ok")
                return {"branches": branches, "active": repo.active_branch.name}
            elif action == "create":
                if not branch_name:
                    raise ValueError("branch_name required for create")
                repo.create_head(branch_name)
                ac.finish("ok")
                return {"created": branch_name}
            elif action == "delete":
                if not branch_name:
                    raise ValueError("branch_name required for delete")
                repo.delete_head(branch_name)
                ac.finish("ok")
                return {"deleted": branch_name}
            else:
                raise ValueError(f"Unknown action '{action}'; use list, create, or delete")
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_checkout(repo_path: str, ref: str) -> dict:
        """Switch branch or detach to a commit/tag.

        Args:
            repo_path: Absolute path to the local git repository.
            ref: Branch name, tag, or commit SHA to check out.
        """
        ac = AuditCtx("git_checkout", "local", repo_path, {"repo_path": repo_path, "ref": ref})
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            repo.git.checkout(ref)
            ac.finish("ok")
            return {"checked_out": ref, "detached": repo.head.is_detached}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_add(repo_path: str, paths: list[str]) -> dict:
        """Stage files or paths.

        Args:
            repo_path: Absolute path to the local git repository.
            paths: List of file or directory paths to stage (relative to repo root).
        """
        ac = AuditCtx("git_add", "local", repo_path, {"repo_path": repo_path, "paths": paths})
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            repo.index.add(paths)
            staged = [item.a_path for item in repo.index.diff("HEAD")] if repo.head.is_valid() else paths
            ac.finish("ok")
            return {"staged": staged}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_commit(repo_path: str, message: str) -> dict:
        """Create a commit. Agent ID is appended to commit metadata.

        Args:
            repo_path: Absolute path to the local git repository.
            message: Commit message.
        """
        ac = AuditCtx("git_commit", "local", repo_path, {"repo_path": repo_path})
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            config = get_config()
            agent_tag = f"\n\nagent-id: {config.agent_id}" if config.agent_id != "unknown" else ""
            full_message = message + agent_tag

            signing_key = config.git_signing_key
            if signing_key:
                # gitpython uses -S with GPG key ID (not the key value)
                repo.git.commit("-S", f"--gpg-sign={signing_key}", "-m", full_message)
                commit = repo.head.commit
            else:
                commit = repo.index.commit(full_message)

            ac.finish("ok")
            return {"sha": commit.hexsha[:12], "message": message}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_push(
        repo_path: str,
        remote: str = "origin",
        branch: Optional[str] = None,
    ) -> dict:
        """Push branch to remote.

        Args:
            repo_path: Absolute path to the local git repository.
            remote: Remote name (default: origin).
            branch: Branch to push (default: current branch).
        """
        params = {"repo_path": repo_path, "remote": remote, "branch": branch}
        ac = AuditCtx("git_push", "local", repo_path, params)
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            branch_name = branch or repo.active_branch.name
            push_info = repo.remotes[remote].push(branch_name)
            flags = [str(p.flags) for p in push_info]
            ac.finish("ok")
            return {"pushed": branch_name, "remote": remote, "flags": flags}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_pull(repo_path: str, remote: str = "origin") -> dict:
        """Pull from remote.

        Args:
            repo_path: Absolute path to the local git repository.
            remote: Remote name (default: origin).
        """
        ac = AuditCtx("git_pull", "local", repo_path, {"repo_path": repo_path, "remote": remote})
        try:
            repo = _open_repo(repo_path)
            pull_info = repo.remotes[remote].pull()
            ac.finish("ok")
            return {"remote": remote, "flags": [str(p.flags) for p in pull_info]}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

    @mcp.tool
    def git_tag(
        repo_path: str,
        tag_name: str,
        message: Optional[str] = None,
        push: bool = False,
        remote: str = "origin",
    ) -> dict:
        """Create an annotated tag and optionally push it.

        Args:
            repo_path: Absolute path to the local git repository.
            tag_name: Tag name (e.g. 'v1.2.3').
            message: Tag annotation message (defaults to tag name).
            push: If True, push the tag to the remote.
            remote: Remote to push to (default: origin).
        """
        params = {"repo_path": repo_path, "tag_name": tag_name, "push": push, "remote": remote}
        ac = AuditCtx("git_tag", "local", repo_path, params)
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            tag_msg = message or tag_name
            tag = repo.create_tag(tag_name, message=tag_msg)
            result = {"tag": tag_name, "sha": tag.commit.hexsha[:12]}
            if push:
                repo.remotes[remote].push(tag_name)
                result["pushed"] = True
            ac.finish("ok")
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": str(e)}

"""Local git operations via gitpython (no subprocess)."""

from __future__ import annotations

import git
import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..gitflags import evaluate_fetch, evaluate_push
from ..security import (
    RemoteUrlRejected,
    WriteGlobDenied,
    redact_url_credentials,
    scrub,
    validate_read_path,
    validate_remote_name,
    validate_remote_url,
    validate_write_globs,
    validate_write_path,
)

log = structlog.get_logger(__name__)


def _open_repo(repo_path: str) -> git.Repo:
    try:
        return git.Repo(repo_path, search_parent_directories=False)
    except git.InvalidGitRepositoryError:
        raise ValueError(f"Not a git repository: {repo_path}") from None
    except git.NoSuchPathError:
        raise ValueError(f"Path does not exist: {repo_path}") from None


def _staged_paths(repo: git.Repo) -> list[str]:
    """Every path currently staged for the next commit.

    Mirrors git_add's own HEAD-validity fallback: before the first commit there is no
    HEAD to diff against, so every index entry is staged by definition. Used to
    enforce write_globs at commit time against what is actually staged, not just what
    the most recent git_add call named — git_commit commits whatever is staged
    regardless of what staged it.
    """
    if repo.head.is_valid():
        return [item.a_path for item in repo.index.diff("HEAD")]
    return [path for path, _stage in repo.index.entries]


def register(mcp) -> None:
    @mcp.tool
    def git_status(repo_path: str) -> dict:
        """Working tree status: staged, unstaged, and untracked files.

        Args:
            repo_path: Absolute path to the local git repository.
        """
        ac = AuditCtx("git_status", "local", repo_path, {"repo_path": repo_path})
        try:
            validate_read_path(repo_path)
            repo = _open_repo(repo_path)
            staged = (
                [item.a_path for item in repo.index.diff("HEAD")] if repo.head.is_valid() else []
            )
            unstaged = [item.a_path for item in repo.index.diff(None)]
            untracked = repo.untracked_files
            result = {
                "repo": repo_path,
                "branch": repo.active_branch.name
                if not repo.head.is_detached
                else "HEAD (detached)",
                "staged": staged,
                "unstaged": unstaged,
                "untracked": list(untracked),
                "is_dirty": repo.is_dirty(untracked_files=True),
            }
            ac.finish("ok")
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_diff(repo_path: str, staged: bool = False, file_path: str | None = None) -> dict:
        """Show diff — staged or unstaged, optionally for a specific file.

        Args:
            repo_path: Absolute path to the local git repository.
            staged: If True, show staged diff (vs HEAD). Default False (unstaged).
            file_path: Optional file path to diff (relative to repo root).
        """
        ac = AuditCtx(
            "git_diff",
            "local",
            repo_path,
            {"repo_path": repo_path, "staged": staged, "file_path": file_path},
        )
        try:
            validate_read_path(repo_path)
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
                    patches.append(
                        {
                            "file": d.a_path,
                            "change_type": d.change_type,
                            "diff": d.diff.decode("utf-8", errors="replace") if d.diff else "",
                        }
                    )
                except Exception:
                    patches.append({"file": d.a_path, "change_type": d.change_type, "diff": ""})

            ac.finish("ok")
            return {"repo": repo_path, "staged": staged, "patches": patches}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_log(repo_path: str, limit: int = 20, branch: str | None = None) -> dict:
        """Recent commit log with author, date, and message.

        Args:
            repo_path: Absolute path to the local git repository.
            limit: Max commits to return (default 20).
            branch: Branch or ref to log (default: active branch).
        """
        ac = AuditCtx("git_log", "local", repo_path, {"repo_path": repo_path, "limit": limit})
        try:
            validate_read_path(repo_path)
            limit = min(limit, 200)
            repo = _open_repo(repo_path)
            ref = branch or repo.active_branch.name
            commits = []
            for c in repo.iter_commits(ref, max_count=limit):
                commits.append(
                    {
                        "sha": c.hexsha[:12],
                        "author": f"{c.author.name} <{c.author.email}>",
                        "date": c.authored_datetime.isoformat(),
                        "message": c.message.strip(),
                    }
                )
            ac.finish("ok")
            return {"repo": repo_path, "branch": ref, "commits": commits}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_show(repo_path: str, ref: str) -> dict:
        """Inspect a specific commit or object.

        Args:
            repo_path: Absolute path to the local git repository.
            ref: Commit SHA, tag, or branch name to inspect.
        """
        ac = AuditCtx("git_show", "local", repo_path, {"repo_path": repo_path, "ref": ref})
        try:
            validate_read_path(repo_path)
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
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_branch(
        repo_path: str,
        action: str = "list",
        branch_name: str | None = None,
    ) -> dict:
        """List, create, or delete branches.

        'create' does not check the new branch out — it is `git branch`, not
        `git checkout -b`. The result carries `active_branch` so the caller can see
        which branch a following commit would land on; pair with git_checkout to
        switch.

        Args:
            repo_path: Absolute path to the local git repository.
            action: 'list', 'create', or 'delete'.
            branch_name: Branch name for create/delete actions.
        """
        params = {"repo_path": repo_path, "action": action, "branch_name": branch_name}
        ac = AuditCtx("git_branch", "local", repo_path, params)
        try:
            if action == "list":
                validate_read_path(repo_path)
            else:
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
                # `create` deliberately does NOT check out — `git branch` and
                # `git checkout -b` are different operations, and switching would
                # break callers that already pair this with git_checkout. Report
                # the branch the repo is actually on so the caller can see that a
                # following git_commit would land there, not on the new branch.
                return {
                    "created": branch_name,
                    "active_branch": repo.active_branch.name
                    if not repo.head.is_detached
                    else "HEAD (detached)",
                }
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
            return {"error": scrub(str(e))}

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
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_remote(
        repo_path: str,
        action: str = "list",
        name: str | None = None,
        url: str | None = None,
    ) -> dict:
        """List, add, or remove git remotes.

        Adding a remote is what makes the fork-and-contribute workflow possible
        without leaving this server: pair with github_fork, which returns the
        clone URL to pass here.

        Remote URLs must not embed credentials. A credential-bearing URL is
        refused outright rather than redacted — it would otherwise persist in
        .git/config and be used by every later fetch and push. Returned URLs have
        any pre-existing userinfo redacted, so a remote added out-of-band cannot
        leak a token through this tool.

        Args:
            repo_path: Absolute path to the local git repository.
            action: 'list', 'add', or 'remove'.
            name: Remote name (required for add/remove).
            url: Remote URL (required for add). http(s)/ssh/git or scp-style user@host:path.
        """
        params = {
            "repo_path": repo_path,
            "action": action,
            "name": name,
            # The URL reaches the audit log, so redact before it is recorded — an
            # add that is about to be refused for embedded credentials must not
            # write those credentials to the audit trail on its way out.
            "url": redact_url_credentials(url) if url else None,
        }
        ac = AuditCtx("git_remote", "local", repo_path, params)
        try:
            if action == "list":
                validate_read_path(repo_path)
            else:
                validate_write_path(repo_path)
            repo = _open_repo(repo_path)

            if action == "list":
                remotes = [
                    {"name": r.name, "url": redact_url_credentials(r.url)} for r in repo.remotes
                ]
                ac.finish("ok")
                return {"repo": repo_path, "remotes": remotes}

            if action == "add":
                validate_remote_name(name)
                validate_remote_url(url)
                if name in [r.name for r in repo.remotes]:
                    raise ValueError(f"Remote '{name}' already exists")
                remote = repo.create_remote(name, url)
                ac.finish("ok")
                return {"added": remote.name, "url": redact_url_credentials(url)}

            if action == "remove":
                validate_remote_name(name)
                if name not in [r.name for r in repo.remotes]:
                    raise ValueError(f"No such remote: '{name}'")
                repo.delete_remote(name)
                ac.finish("ok")
                return {"removed": name}

            raise ValueError(f"Unknown action '{action}'; use list, add, or remove")
        except RemoteUrlRejected as e:
            ac.finish("denied:remote_url")
            return {"error": scrub(str(e))}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

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
            validate_write_globs(repo_path, paths)
            repo = _open_repo(repo_path)
            # Use git binary to stage files; repo.index.add(["."])  would recurse
            # into .git/ internals. The git binary skips .git/ by design. (GHOST-1)
            repo.git.add("--", *paths)
            staged = (
                [item.a_path for item in repo.index.diff("HEAD")] if repo.head.is_valid() else paths
            )
            ac.finish("ok")
            return {"staged": staged}
        except WriteGlobDenied as e:
            ac.finish("denied:write_glob")
            return {"error": scrub(str(e))}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

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
            # Enforced against the staged set, not the paths a prior git_add call named —
            # git_commit commits whatever is staged regardless of what staged it, so
            # git_add-only enforcement would be bypassable via any other staging path.
            validate_write_globs(repo_path, _staged_paths(repo))
            config = get_config()
            agent_tag = f"\n\nagent-id: {config.agent_id}" if config.agent_id != "unknown" else ""
            full_message = message + agent_tag

            actor = (
                git.Actor(config.git_agent_name, config.git_agent_email)
                if config.git_agent_name
                else None
            )
            signing_key = config.git_signing_key
            if signing_key:
                # gitpython uses -S with GPG key ID (not the key value)
                cmd = []
                if actor:
                    cmd += ["-c", f"user.name={actor.name}", "-c", f"user.email={actor.email}"]
                cmd += ["-S", f"--gpg-sign={signing_key}", "-m", full_message]
                repo.git.commit(*cmd)
                commit = repo.head.commit
            else:
                kwargs = {"author": actor, "committer": actor} if actor else {}
                commit = repo.index.commit(full_message, **kwargs)

            ac.finish("ok")
            return {"sha": commit.hexsha[:12], "message": message}
        except WriteGlobDenied as e:
            ac.finish("denied:write_glob")
            return {"error": scrub(str(e))}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_push(
        repo_path: str,
        remote: str = "origin",
        branch: str | None = None,
    ) -> dict:
        """Push branch to remote.

        Returns `pushed_sha`, the full sha of the ref that was pushed. A push can
        succeed while carrying a branch that does not contain your latest commit —
        compare `pushed_sha` against local HEAD to confirm your work landed.

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
            # Captured before the push so the reported sha is the one that was
            # actually sent, not whatever the ref moved to afterwards.
            try:
                pushed_sha = repo.heads[branch_name].commit.hexsha
            except (IndexError, KeyError):
                pushed_sha = None
            push_info = repo.remotes[remote].push(branch_name)

            outcome = evaluate_push(push_info)
            decoded = outcome.flags

            if outcome.failed:
                # PushInfo.summary is the remote's raw text and can carry a
                # credential-bearing remote URL straight to the caller (SC-14).
                reason = outcome.summary or "push rejected by remote"
                log.warning(
                    "push_failed", remote=remote, branch=branch_name, flags=decoded, summary=reason
                )
                ac.finish("error:PushRejected")
                # No "pushed" key on failure — a result carrying both would be the
                # same bug in a new shape.
                return {
                    "error": f"push to {remote}/{branch_name} failed: {reason}",
                    "remote": remote,
                    "branch": branch_name,
                    "flags": decoded,
                    "summary": reason,
                }

            # Set upstream when it is missing: without it, the caller's natural
            # verification (`git rev-list @{u}..HEAD`) errors instead of confirming.
            upstream_set = False
            try:
                head = repo.heads[branch_name]
                if head.tracking_branch() is None:
                    head.set_tracking_branch(repo.remotes[remote].refs[branch_name])
                upstream_set = head.tracking_branch() is not None
            except Exception as e:  # never fail a good push over upstream bookkeeping
                log.warning("push_upstream_not_set", branch=branch_name, error=str(e))

            ac.finish("ok")
            # pushed_sha is the full sha of the ref that was pushed. A push can
            # genuinely succeed while carrying a branch that does not contain the
            # caller's latest commit (vikunja #289, id 300) — comparing this against
            # local HEAD is the only way to tell. Full sha, to match `git ls-remote`.
            return {
                "pushed": branch_name,
                "pushed_sha": pushed_sha,
                "remote": remote,
                "flags": decoded,
                "upstream_set": upstream_set,
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_pull(repo_path: str, remote: str = "origin") -> dict:
        """Pull from remote.

        Args:
            repo_path: Absolute path to the local git repository.
            remote: Remote name (default: origin).
        """
        ac = AuditCtx("git_pull", "local", repo_path, {"repo_path": repo_path, "remote": remote})
        try:
            validate_write_path(repo_path)
            repo = _open_repo(repo_path)
            outcome = evaluate_fetch(repo.remotes[remote].pull())

            if outcome.failed:
                # FetchInfo.note is the remote's text and can carry a
                # credential-bearing URL straight to the caller (SC-14) — it is
                # scrubbed by evaluate_fetch.
                reason = outcome.summary or "pull rejected by remote"
                log.warning("pull_failed", remote=remote, flags=outcome.flags, note=reason)
                ac.finish("error:FetchRejected")
                # No success-shaped key alongside the error.
                return {
                    "error": f"pull from {remote} failed: {reason}",
                    "remote": remote,
                    "flags": outcome.flags,
                    "note": reason,
                }

            ac.finish("ok")
            result = {"remote": remote, "flags": outcome.flags}
            if outcome.summary:
                result["note"] = outcome.summary
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

    @mcp.tool
    def git_tag(
        repo_path: str,
        tag_name: str,
        message: str | None = None,
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
                outcome = evaluate_push(repo.remotes[remote].push(tag_name))
                if outcome.failed:
                    reason = outcome.summary or "tag push rejected by remote"
                    log.warning(
                        "tag_push_failed",
                        remote=remote,
                        tag=tag_name,
                        flags=outcome.flags,
                        summary=reason,
                    )
                    ac.finish("error:PushRejected")
                    # No "pushed" key on failure. The local tag exists by now, so
                    # the caller is left holding state the remote does not have —
                    # say so rather than making it infer that.
                    return {
                        "error": (
                            f"tag {tag_name} push to {remote} failed: {reason}. "
                            "The local tag was created and needs cleanup."
                        ),
                        "tag": tag_name,
                        "sha": tag.commit.hexsha[:12],
                        "remote": remote,
                        "flags": outcome.flags,
                        "summary": reason,
                        "local_tag_created": True,
                    }
                result["pushed"] = True
                result["flags"] = outcome.flags
            ac.finish("ok")
            return result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}")
            return {"error": scrub(str(e))}

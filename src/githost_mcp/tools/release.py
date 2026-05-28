"""Release orchestration tool with rollback (1 tool)."""

from __future__ import annotations

import structlog

from ..audit import AuditCtx
from ..config import get_config
from ..observability import emit_release_target
from ..security import validate_write_path

log = structlog.get_logger(__name__)


def register(mcp) -> None:
    @mcp.tool
    async def release(
        repo_path: str,
        version: str,
        targets: list[str] | None = None,
        github_repo: str | None = None,
        gitea_repo: str | None = None,
        gitlab_project: str | None = None,
        notes: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Coordinated multi-target release: tag → GitHub/Gitea/GitLab → PyPI → npm.

        Rolls back git/release steps on failure. PyPI and npm are immutable — failures
        are logged but do not trigger rollback. Returns immediately with release URLs.

        Args:
            repo_path: Local git repo path (must be in ALLOWED_REPO_ROOTS).
            version: Version string without leading 'v' (e.g. '1.2.3').
            targets: List of targets: github, gitea, gitlab, pypi, npm (default: all configured).
            github_repo: GitHub repo in 'owner/repo' format.
            gitea_repo: Gitea repo in 'owner/repo' format.
            gitlab_project: GitLab project in 'namespace/project' format.
            notes: Release notes markdown (GitHub auto-generates if omitted).
            dry_run: If True, validate inputs and return plan without making changes.
        """
        config = get_config()
        tag = f"v{version}"
        params = {"repo_path": repo_path, "version": version, "targets": targets, "dry_run": dry_run}
        ac = AuditCtx("release", "local", repo_path, params)

        try:
            validate_write_path(repo_path)
        except ValueError as e:
            ac.finish("error:ValueError")
            return {"error": str(e)}

        # Resolve targets
        effective_targets = targets or []
        if not effective_targets:
            if config.github_token and github_repo:
                effective_targets.append("github")
            if config.gitea_token and gitea_repo:
                effective_targets.append("gitea")
            if config.gitlab_token and gitlab_project:
                effective_targets.append("gitlab")
            if config.pypi_token:
                effective_targets.append("pypi")
            if config.npm_token:
                effective_targets.append("npm")

        if dry_run:
            ac.finish("ok")
            return {
                "dry_run": True,
                "version": version,
                "tag": tag,
                "targets": effective_targets,
                "repo_path": repo_path,
            }

        import git
        try:
            repo = git.Repo(repo_path, search_parent_directories=False)
        except Exception as e:
            ac.finish("error:git")
            return {"error": f"Cannot open repo: {e}"}

        if repo.is_dirty(untracked_files=False):
            ac.finish("error:dirty")
            return {"error": "Working tree is dirty — commit or stash changes before releasing"}

        if tag in [t.name for t in repo.tags]:
            ac.finish("error:tag_exists")
            return {"error": f"Tag '{tag}' already exists"}

        urls: dict = {}
        created: list[str] = []

        # Step 1: git tag + push
        try:
            git_tag = repo.create_tag(tag, message=f"Release {tag}")
            repo.remotes["origin"].push(tag)
            created.append("git_tag")
            log.info("release_tag_created", tag=tag)
        except Exception as e:
            _rollback(repo, tag, created, urls)
            ac.finish("error:git_tag")
            return {"error": f"Failed to create/push tag: {e}"}

        # Step 2: GitHub release
        if "github" in effective_targets and github_repo:
            try:
                url = await _create_release_sync(github_repo, tag, notes)
                urls["github"] = url
                created.append("github")
                emit_release_target("github", "ok")
            except Exception as e:
                emit_release_target("github", "error")
                _rollback(repo, tag, created, urls, github_repo, gitea_repo, gitlab_project)
                ac.finish("error:github")
                return {"error": f"GitHub release failed: {e}", "rolled_back": True}

        # Step 3: Gitea release
        if "gitea" in effective_targets and gitea_repo:
            try:
                from .._providers.gitea_client import gitea_post
                from ..config import get_config as _gc
                owner = gitea_repo.split("/")[0] if "/" in gitea_repo else _gc().gitea_owner
                repo_name = gitea_repo.split("/")[-1]
                data = {"tag_name": tag, "name": tag, "body": notes or ""}
                result = await gitea_post(f"/repos/{owner}/{repo_name}/releases", data)
                urls["gitea"] = result.get("html_url", "")
                created.append("gitea")
                emit_release_target("gitea", "ok")
            except Exception as e:
                emit_release_target("gitea", "error")
                _rollback(repo, tag, created, urls, github_repo, gitea_repo, gitlab_project)
                ac.finish("error:gitea")
                return {"error": f"Gitea release failed: {e}", "rolled_back": True}

        # Step 4: GitLab release
        if "gitlab" in effective_targets and gitlab_project:
            try:
                from .._providers.gitlab_client import get_gitlab, gitlab_call
                gl = get_gitlab()
                proj = gitlab_call(gl.projects.get, gitlab_project)
                gl_rel = gitlab_call(
                    proj.releases.create,
                    {"name": tag, "tag_name": tag, "description": notes or ""},
                )
                urls["gitlab"] = getattr(gl_rel, "_links", {}).get("self", "")
                created.append("gitlab")
                emit_release_target("gitlab", "ok")
            except Exception as e:
                emit_release_target("gitlab", "error")
                _rollback(repo, tag, created, urls, github_repo, gitea_repo, gitlab_project)
                ac.finish("error:gitlab")
                return {"error": f"GitLab release failed: {e}", "rolled_back": True}

        # Step 5: PyPI (immutable — no rollback)
        if "pypi" in effective_targets:
            try:
                import subprocess, os as _os
                config = get_config()
                dist_path = _os.path.join(repo_path, "dist")
                upload_env = {**_os.environ, "TWINE_PASSWORD": config.pypi_token, "TWINE_USERNAME": "__token__"}
                result = subprocess.run(
                    ["twine", "upload", f"{dist_path}/*"],
                    cwd=repo_path, env=upload_env, capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    urls["pypi"] = f"https://pypi.org/project/{_get_package_name(repo_path)}/{version}/"
                    emit_release_target("pypi", "ok")
                else:
                    emit_release_target("pypi", "error")
                    log.warning("pypi_publish_failed", stderr=result.stderr[:200])
            except Exception as e:
                emit_release_target("pypi", "error")
                log.warning("pypi_publish_exception", error=str(e))

        # Step 6: npm (immutable — no rollback)
        if "npm" in effective_targets:
            try:
                import subprocess, os as _os
                config = get_config()
                pub_env = {**_os.environ, "NPM_TOKEN": config.npm_token}
                result = subprocess.run(
                    ["npm", "publish"],
                    cwd=repo_path, env=pub_env, capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    emit_release_target("npm", "ok")
                else:
                    emit_release_target("npm", "error")
                    log.warning("npm_publish_failed", stderr=result.stderr[:200])
            except Exception as e:
                emit_release_target("npm", "error")
                log.warning("npm_publish_exception", error=str(e))

        ac.finish("ok")
        return {
            "success": True,
            "version": version,
            "tag": tag,
            "targets": effective_targets,
            "urls": urls,
        }


def _rollback(
    repo,
    tag: str,
    created: list,
    urls: dict,
    github_repo: str | None = None,
    gitea_repo: str | None = None,
    gitlab_project: str | None = None,
) -> None:
    log.warning("release_rollback", tag=tag, created=created, orphan_urls=urls)

    # Attempt to delete provider releases in reverse creation order
    if "github" in created and github_repo:
        try:
            from .._providers.github_client import get_github, github_call
            gh = get_github()
            gh_repo = github_call(gh.get_repo, github_repo)
            rel = github_call(gh_repo.get_release, tag)
            github_call(rel.delete_release)
            log.info("rollback_deleted_github_release", tag=tag)
        except Exception as exc:
            log.warning("rollback_github_release_failed", tag=tag, url=urls.get("github"), error=str(exc))

    if "gitlab" in created and gitlab_project:
        try:
            from .._providers.gitlab_client import get_gitlab, gitlab_call
            gl = get_gitlab()
            proj = gitlab_call(gl.projects.get, gitlab_project)
            gitlab_call(proj.releases.delete, tag)
            log.info("rollback_deleted_gitlab_release", tag=tag)
        except Exception as exc:
            log.warning("rollback_gitlab_release_failed", tag=tag, url=urls.get("gitlab"), error=str(exc))

    if "gitea" in created:
        # No delete client implemented — log orphan for manual cleanup
        log.warning("rollback_gitea_orphan", tag=tag, url=urls.get("gitea"),
                    note="Gitea release delete not implemented; manual cleanup required")

    # Delete local and remote git tag
    if "git_tag" in created:
        try:
            repo.delete_tag(tag)
        except Exception:
            pass
        try:
            repo.remotes["origin"].push(f":refs/tags/{tag}")
        except Exception:
            pass


async def _create_release_sync(github_repo: str, tag: str, notes: str | None) -> str:
    from .._providers.github_client import get_github, github_call
    gh = get_github()
    gh_repo = github_call(gh.get_repo, github_repo)
    release = github_call(
        gh_repo.create_git_release,
        tag=tag,
        name=tag,
        message=notes or "",
        draft=False,
        prerelease=False,
    )
    return release.html_url


def _get_package_name(repo_path: str) -> str:
    import re, os
    toml_path = os.path.join(repo_path, "pyproject.toml")
    if os.path.exists(toml_path):
        with open(toml_path) as f:
            for line in f:
                m = re.match(r'^name\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    return "unknown"

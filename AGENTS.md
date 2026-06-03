# githost-mcp

FastMCP server providing 39 tools across local git, GitHub, Gitea, GitLab, Woodpecker CI, release orchestration, registry publishing, and audit log queries.

## Structure

```
src/githost_mcp/
  server.py                  Entry point — registers all tool modules, runs lifespan
  tools/
    git_local.py             Local git ops via gitpython: status, diff, add, commit,
                             push, pull, log, show, checkout, branch, tag
    github.py                GitHub PR listing, comments, workflow status, releases
    gitea.py                 Gitea PR CRUD and merge via httpx
    gitlab.py                GitLab MR listing and releases
    release.py               Coordinated release tool: tag + publish in one operation
    registry.py              npm_publish, pypi_publish via subprocess
    woodpecker.py            Pipeline list/status/logs/trigger/cancel
    audit_query.py           Query the structured JSONL audit log
  _providers/
    github_client.py         PyGitHub wrapper
    gitea_client.py          httpx Gitea API client
    gitlab_client.py         httpx GitLab API client
  audit.py                   JSONL audit logging — every tool call recorded
  security.py                ALLOWED_REPO_ROOTS enforcement for write operations
  config.py                  Env var loading
  observability.py           OTEL + Prometheus init
tests/                       pytest tests
pyproject.toml
```

## Source files

| File                    | Role                                                     |
|-------------------------|----------------------------------------------------------|
| `server.py`             | FastMCP app, lifespan, tool module registration          |
| `tools/git_local.py`    | 11 local git tools                                       |
| `tools/github.py`       | GitHub PRs, workflows, releases                          |
| `tools/gitea.py`        | Gitea PR create/get/list/comment/merge                   |
| `tools/gitlab.py`       | GitLab MRs and releases                                  |
| `tools/release.py`      | Coordinated tag-and-publish release flow                 |
| `tools/registry.py`     | PyPI and npm publish via subprocess                      |
| `tools/woodpecker.py`   | Woodpecker CI pipeline management                        |
| `tools/audit_query.py`  | Query JSONL audit trail                                  |
| `audit.py`              | Writes one audit record per tool call                    |
| `security.py`           | `ALLOWED_REPO_ROOTS` path enforcement                    |
| `config.py`             | All env var defaults in one place                        |

## Configuration

| Env var                    | Purpose                                                     |
|----------------------------|-------------------------------------------------------------|
| `GITHUB_TOKEN`             | GitHub API auth                                             |
| `GITEA_TOKEN`              | Gitea API auth                                              |
| `GITEA_URL`                | Gitea base URL                                              |
| `GITLAB_TOKEN`             | GitLab API auth                                             |
| `GITLAB_URL`               | GitLab base URL                                             |
| `WOODPECKER_URL`           | Woodpecker CI base URL                                      |
| `WOODPECKER_TOKEN`         | Woodpecker API token                                        |
| `ALLOWED_REPO_ROOTS`       | Colon-separated paths; required for write operations        |
| `AGENT_ID`                 | Injected into audit log records                             |
| `LOG_LEVEL`                | Logging verbosity                                           |

## Architecture decisions

- **`ALLOWED_REPO_ROOTS` is a security control** — write operations (commit, push, merge, publish) check the repo path against this allowlist before executing. Do not bypass this check or widen the matching logic.
- **Every tool call is audit-logged** — `audit.py` writes a JSONL record with agent_id, tool name, args, and outcome for every invocation. `audit_query` lets agents review their own call history.
- **`woodpecker_get_logs` content is excluded from audit** — log output can be large and may contain sensitive values. Only the call metadata (pipeline_id, step_name) is recorded; the log text itself is not.
- **Registry publishing uses subprocess** — `npm_publish` and `pypi_publish` shell out rather than using library APIs. This keeps credentials out of the server process and delegates to the standard toolchains.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.

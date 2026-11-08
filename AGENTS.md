# githost-mcp

FastMCP server providing 79 tools across local git, GitHub, Gitea, GitLab, Woodpecker CI, release orchestration, registry publishing, and audit log queries. PR/MR review, CI control, and issues are exposed as method-dispatch tools (one tool, a `method` argument, per-operation audit).

## Structure

```
src/githost_mcp/
  server.py                  Entry point — registers all tool modules, runs lifespan
  tools/
    git_local.py             Local git ops via gitpython: status, diff, add, commit,
                             push, force-with-lease push, pull, log, show, checkout,
                             branch, tag, remote, worktree, clone
    github.py                GitHub PRs + review/diff, Actions control, release
                             CRUD, issues (method-dispatch), workflow status
    gitea.py                 Gitea PRs + review/diff, Actions control, release
                             CRUD, issues (method-dispatch) via httpx
    gitlab.py                GitLab MRs + review/approve, pipeline control,
                             release CRUD, issues (method-dispatch)
    release.py               Coordinated release tool: tag + publish in one operation
    registry.py              npm_publish, pypi_publish via subprocess
    woodpecker.py            Pipeline list/status/logs/config/queue/trigger/restart/cancel
    audit_query.py           Query the structured JSONL audit log
  _providers/
    github_client.py         PyGitHub wrapper
    gitea_client.py          httpx Gitea API client
    gitlab_client.py         httpx GitLab API client
  logview.py                 Bounded, scrubbed CI log view (tail/head/range, literal grep)
  bootstrap.py               git_clone / *_repo_create guards: names, ownership,
                             computed clone URL and destination, timed clone
  audit.py                   JSONL audit logging — every tool call recorded; AuditCtx
                             owns the githost.<tool> span and the effect record
  effects.py                 EFFECT_TOOLS / READ_TOOLS classification, effect bounding
  correlation.py             X-Forge-* correlation ids; per-call scope middleware
  httptrace.py               httpx clients traced as child spans (path templates only)
  security.py                ALLOWED_REPO_ROOTS enforcement for write operations
  config.py                  Env var loading
  observability.py           OTel init, span helpers, metrics
tests/                       pytest tests
pyproject.toml
```

## Source files

| File                    | Role                                                     |
|-------------------------|----------------------------------------------------------|
| `server.py`             | FastMCP app, lifespan, tool module registration          |
| `tools/git_local.py`    | 16 local git tools, including `git_clone`                |
| `tools/github.py`       | GitHub PRs + review, Actions, release CRUD, issues       |
| `tools/gitea.py`        | Gitea PRs + review, Actions, release CRUD, issues        |
| `tools/gitlab.py`       | GitLab MRs + review, pipelines, release CRUD, issues     |
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
| `ALLOWED_REPO_ROOTS`       | Comma-separated paths; required for write operations         |
| `GITHOST_WORKTREE_ROOT`    | Where `git_worktree` puts worktrees (default `~/repos/personal/_worktrees`). A location, not a grant: still checked against the write roots |
| `AGENT_ID`                 | Injected into audit log records                             |
| `AGENT_MANIFEST_PATH`      | Path to this agent's manifest YAML; used as an allowlist fallback when `ALLOWED_REPO_ROOTS` is unset (default: `~/.claude/manifests/{AGENT_ID}-agent.yml`) |
| `LOG_LEVEL`                | Logging verbosity                                           |
| `TRANSPORT`                | `stdio` (default) or `http` — see `server.py::main()` and README "Deploy" |
| `HTTP_HOST`                | HTTP transport bind address; must be loopback unless `GITHOST_MCP_ALLOW_NONLOOPBACK=1` |
| `HTTP_PORT`                | HTTP transport port (one per agent PM2 process)              |
| `GITHOST_MCP_AUTH_TOKEN`   | Bearer token for HTTP transport; required whenever `TRANSPORT=http`, must be >= 16 chars |

## Architecture decisions

- **`ALLOWED_REPO_ROOTS` is a security control** — write operations (commit, push, merge, publish) check the repo path against this allowlist before executing. Do not bypass this check or widen the matching logic. If unset (or empty), `config.py` falls back to reading `git_backed: true` entries from the agent's manifest at `AGENT_MANIFEST_PATH` — the explicit env var always wins when set.
- **Every tool call is audit-logged** — `audit.py` writes a JSONL record with agent_id, tool name, args, and outcome for every invocation. `audit_query` lets agents review their own call history.
- **A new tool is classified before it ships** — every tool belongs to exactly one of `effects.EFFECT_TOOLS` or `effects.READ_TOOLS` (`tests/test_effects.py` fails otherwise). An effect tool calls `ac.effect(...)` before `ac.finish("ok")`, with ids, SHAs, refs and counts only — never a message, body, file name or URL. A tool that writes only for some `method`/`action` values names them in `EFFECT_SELECTORS`. The test suite fails any test in which a successful write call recorded no effect (`tests/conftest.py`).
- **`X-Forge-Run-Id` / `-Task-Id` / `-Session-Id` are attribution only** — `correlation.py` validates and records them; nothing may gate on them. Any holder of the bearer token can send any value.
- **Spans and their attributes** — `AuditCtx` opens `githost.<tool>` at construction; wrap git operations in `ac.child("git.<op>")` and build HTTP clients with `httptrace.async_client()`/`sync_client()`. Child-span attributes are filtered to `observability.CHILD_ATTR_KEYS`; never put a URL, query string, body or exception message on a span.
- **`woodpecker_get_logs` content is excluded from audit** — log output can be large and may contain sensitive values. Only the call metadata (repo, pipeline_id, step_name, workflow, mode, whether `grep` was given; `file` for `woodpecker_pipeline_config`) is recorded; the log text and the grep string are not. The same holds for `gitea_actions`/`github_actions` `get_job_log`.
- **Registry publishing uses subprocess** — `npm_publish` and `pypi_publish` shell out rather than using library APIs. This keeps credentials out of the server process and delegates to the standard toolchains.
- **Per-agent processes, not a shared process (Option A)** — `TRANSPORT=http` runs one PM2
  service per agent (`ecosystem.config.js`), each with its own `AGENT_ID`, tokens, and
  `ALLOWED_REPO_ROOTS` in a separate address space. This keeps `AGENT_ID` non-spoofable (fixed
  at process launch, not read from a request header) and contains credential blast radius. Do
  not collapse this to a single shared process with per-request identity without a deliberate
  decision — that's "Option B", explicitly deferred. `main()` hard-fails closed on a
  non-loopback `HTTP_HOST`, a missing `GITHOST_MCP_AUTH_TOKEN`, and a token shorter than 16
  chars (the credential filter in `audit.py`/`security.py` only redacts tokens over 4 chars,
  so a too-short token could leak into logs) — all three checks are load-bearing, not
  defensive filler.

## Testing

```bash
uv sync --frozen --extra dev --extra observability
.venv/bin/python -m pytest
```

Dependencies are locked in `uv.lock`. After changing `pyproject.toml`, run `uv lock` and
commit both files. CI fails if they are out of sync (`uv lock --check`).

## Git workflow

Branch before editing — do not commit directly to `main`.

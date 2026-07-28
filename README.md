[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# githost-mcp

Unified local + multi-provider git MCP server with a per-agent audit trail as a first-class feature.

Every tool call is tagged with the caller agent (`AGENT_ID`), written to a structured JSONL audit log, and write operations emit agent-bus events. Local git operations run entirely through gitpython — no subprocess, no injection risk.

## Architecture

Every tool call dispatches to one provider and writes a signed audit entry before returning; write operations additionally emit an agent-bus event.

```mermaid
flowchart LR
    client[MCP client / agent] -->|tool call + AGENT_ID| tool[githost-mcp tool]
    tool --> dispatch{provider}
    dispatch -->|local| localgit[gitpython]
    dispatch -->|GitHub| gh[PyGithub]
    dispatch -->|Gitea| gitea[httpx]
    dispatch -->|GitLab| glab[python-gitlab]
    dispatch -->|Woodpecker| wp[httpx]
    localgit --> audit[audit.py]
    gh --> audit
    gitea --> audit
    glab --> audit
    wp --> audit
    audit -->|HMAC-signed JSONL| logfile[(audit log)]
    tool -.->|write ops| bus[[agent-bus event]]
```

Path-taking tools resolve their allowlist before touching the filesystem. An explicit `ALLOWED_REPO_ROOTS` always wins; otherwise the agent manifest is consulted as a fallback; if neither yields a root, the operation fails closed.

```mermaid
flowchart TD
    call[path-taking tool call] --> env{ALLOWED_REPO_ROOTS set?}
    env -->|yes| envroots[use env roots]
    env -->|no| manifest{manifest roots available?}
    manifest -->|yes| mroots[use manifest-declared roots]
    manifest -->|no| deny[deny — fail closed]
    envroots --> validate{path under an allowed root?}
    mroots --> validate
    validate -->|yes| allow[proceed]
    validate -->|no| deny
```

## Why githost-mcp?

| Tool | What it covers | Gap |
|------|----------------|-----|
| `cyanheads/git-mcp-server` (TS) | 28 local git tools | No remote providers, no agent attribution, no audit trail |
| `poly-git-mcp` | GitHub + GitLab + Gitea | Wraps CLI tools — fragile, no audit, no agent ID |
| Official GitHub MCP | GitHub only | No local git, no Gitea/GitLab |
| Official Gitea MCP | Gitea only | No local git, no GitHub/GitLab |
| `j04n-f/woodpecker-mcp` | Woodpecker (6 read-only tools) | No trigger/cancel — githost already exceeds it |

**githost-mcp fills the gap:** local git + multi-provider remote via native APIs + per-agent structured audit trail. As of the Tier-1 parity release it matches the single-provider servers on PR/MR review + diff, CI trigger/rerun/cancel, full release CRUD, and issues — across all three providers behind one audited server.

## Tools (63 total)

Higher-verb-count capabilities (PR/MR review, CI control, issues) are exposed as
**method-dispatch** tools — one tool takes a `method` argument and routes internally —
mirroring how the official GitHub/Gitea servers structure theirs. This adds ~40
operations without the tool count ballooning past what every agent pays for in context.
Each `method` still writes its own per-operation audit entry.

### Local Git (11)
`git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_checkout`, `git_add`, `git_commit`, `git_push`, `git_pull`, `git_tag`

`git_push` reports failure explicitly (as of 0.9.0): if any of `ERROR` / `REJECTED` /
`REMOTE_REJECTED` / `REMOTE_FAILURE` is set on the push result — including an empty
ref-update ack — it returns `{"error": ..., "summary": ..., "flags": [...]}` with **no**
`pushed` key. A caller that only checks for `pushed` will no longer silently treat a
rejected push as a success. `flags` are decoded to reason names (e.g. `["REJECTED",
"ERROR"]`), not a raw integer bitmask. `summary` is the remote's human-readable reason,
credential-scrubbed (see Credential isolation below). On success, a missing upstream is
set automatically and reported as `upstream_set: true`.

### GitHub (16)
`github_create_release`, `github_get_release`, `github_list_releases`, `github_release_update`, `github_release_delete`, `github_workflow_list`, `github_workflow_status`, `github_actions` *(run/rerun/rerun_failed/cancel/logs)*, `github_pr_list`, `github_pr_comments`, `github_pr_create`, `github_pr_get`, `github_pr_merge`, `github_pr_review` *(get_diff/get_files/get_reviews/submit_review/dismiss_review)*, `github_issue_read` *(get/list/comments)*, `github_issue_write` *(create/update/add_comment/close/reopen)*

### Gitea (14)
`gitea_create_release`, `gitea_get_release`, `gitea_list_releases`, `gitea_release_update`, `gitea_release_delete`, `gitea_pr_list`, `gitea_pr_create`, `gitea_pr_get`, `gitea_pr_comment`, `gitea_pr_merge`, `gitea_pr_review` *(get_diff/get_files/submit_review/dismiss_review)*, `gitea_actions` *(list_runs/get_run/list_jobs/get_job_log/dispatch_workflow/rerun_run/rerun_failed_jobs)*, `gitea_issue_read` *(get/list/comments)*, `gitea_issue_write` *(create/update/add_comment/close/reopen)*

### GitLab (13)
`gitlab_create_release`, `gitlab_get_release`, `gitlab_list_releases`, `gitlab_release_update`, `gitlab_release_delete`, `gitlab_mr_list`, `gitlab_mr_create`, `gitlab_mr_get`, `gitlab_mr_merge`, `gitlab_mr_review` *(get_diffs/get_changed_files/approve/unapprove/get_approval_state)*, `gitlab_pipeline` *(list/get/create/retry/cancel/get_job_log)*, `gitlab_issue_read` *(get/list/comments)*, `gitlab_issue_write` *(create/update/add_comment/close/reopen)*

### Release Orchestration (1)
`release` — coordinated multi-target release: git tag → GitHub/Gitea/GitLab release → PyPI → npm, with rollback on failure

### Registry (2)
`pypi_publish`, `npm_publish`

### Woodpecker CI (5)
`woodpecker_trigger`, `woodpecker_list_pipelines`, `woodpecker_get_logs`, `woodpecker_pipeline_cancel`, `woodpecker_status`

`woodpecker_trigger` returns the per-repo pipeline **number** as `pipeline_id` (as of
0.9.0) — this is the value to pass straight into `woodpecker_status`,
`woodpecker_get_logs`, and `woodpecker_pipeline_cancel`, which all resolve `pipeline_id`
as a per-repo number, not Woodpecker's global id. The global id is still returned, as
`internal_id`, for reference only. Previously `trigger` returned the global id, which the
other three tools 404 on — chaining `trigger` into `status`/`get_logs`/`cancel` never
worked prior to this fix.

### Audit (1)
`audit_log_query` — query the JSONL audit log by agent, tool, repo, or time range

## Audit Architecture

Every tool call writes a JSONL entry before returning:

```json
{
  "ts": "2026-05-27T09:14:23.000Z",
  "agent_id": "sysadmin",
  "tool": "git_push",
  "provider": "local",
  "repo": "/home/ted/repos/personal/signoz-mcp",
  "params": {"remote": "origin", "branch": "main"},
  "result": "ok",
  "duration_ms": 312,
  "hmac": "a3f8..."
}
```

Each entry is HMAC-SHA256 signed with `AUDIT_SIGNING_KEY`. The `audit_log_query` tool verifies every returned entry and includes `tamper_detected: true` on any entry that fails.

Example — what did the sysadmin agent push last week?

```python
audit_log_query(agent_id="sysadmin", tool="git_push", since="2026-05-20")
```

## Security Model

### Repo path allowlist

All tools — both read and write — reject any path not under the resolved allowlist. Read tools (`git_status`, `git_diff`, `git_log`, `git_show`) and write tools (`git_add`, `git_commit`, `git_push`, `git_tag`, `git_checkout`, `git_branch create/delete`, `release`) are validated against it. An explicit `ALLOWED_REPO_ROOTS` always wins when set; otherwise `config.py` falls back to the `git_backed: true` `workspace_access` entries in the agent's manifest at `AGENT_MANIFEST_PATH` (see Architecture diagram above). **When neither source yields a root, all operations are disabled** — fail closed, not open. A malformed or unreadable manifest resolves to zero roots rather than raising, so it fails closed the same way an unset `ALLOWED_REPO_ROOTS` does.

### Per-agent committer identity

`GIT_AGENT_NAME` and `GIT_AGENT_EMAIL` set the git author/committer on commits. Defaults to `{AGENT_ID}-agent` / `{AGENT_ID}@forge` when not explicitly set. Values are sanitized (newlines and null bytes stripped) to prevent git header injection. Each commit also appends `agent-id: {AGENT_ID}` as a trailer.

### Query limits

`git_log` caps the `limit` parameter at 200 entries regardless of the requested value, preventing excessive history traversal.

### No subprocess git

All local git operations use **gitpython** (Python library), not subprocess. This eliminates command injection risk via crafted `repo_path` or `branch` values.

### Credential isolation

Token values never appear in:
- JSONL audit entries (credential filter applied before write)
- structlog output (processor filter bound to logger)
- tool return values (scrubbed before return)
- exception messages (caught at provider layer and re-raised without token value)

As of 0.9.0, every caller-facing error return across all 27 sites (`git_local.py`,
`release.py`, `woodpecker.py`, `registry.py`, `gitea.py`/`github.py`/`gitlab.py`) is
scrubbed via **`security.scrub()`** — `redact_url_credentials(mask_credentials(text))` —
rather than `mask_credentials()` alone. `mask_credentials()` only replaces githost-mcp's
own *configured* token values, so a credential a human embedded in a remote by hand
(`https://user:token@host/...`) previously survived it. `redact_url_credentials()` strips
the userinfo component from any scheme-qualified URL by shape, independent of whether
githost-mcp knows the token. scp-style remotes (`git@github.com:owner/repo.git`) have no
scheme and are left readable — that's the form every forge remote actually uses. This
closes the gap where `git_push`'s new `summary` field (the remote's raw rejection text)
could otherwise have surfaced a credential verbatim.

Each provider has its own env vars — a compromised GitHub token does not expose Gitea or GitLab credentials.

### HMAC tamper-evidence

`AUDIT_SIGNING_KEY` (required) is a server-side secret set in the launcher. Each JSONL entry includes `hmac: HMAC-SHA256(canonical_json, key)`. The `audit_log_query` tool verifies every returned entry. This is symmetric (same key signs and verifies) — it proves the file wasn't edited after write, not that the agent identity is genuine. Agent identity proof is the scoped-mcp layer's job.

### HTTP transport surface

`TRANSPORT=http` (see [Deploy](#deploy)) opens a local network listener where stdio mode has
none. Two controls are mandatory together, not either/or:

- **Loopback-only bind, fail closed.** `main()` refuses to start if `HTTP_HOST` resolves to
  anything other than `127.0.0.1` / `localhost` / `::1`, unless `GITHOST_MCP_ALLOW_NONLOOPBACK=1`
  is set explicitly. There is no default that silently exposes the port beyond the host.
- **Bearer token auth.** When `GITHOST_MCP_AUTH_TOKEN` is set, FastMCP's built-in
  `StaticTokenVerifier` rejects any request without a matching `Authorization: Bearer <token>`
  header (401). scoped-mcp's manifest `headers` block supplies it — see the Configuration block
  in the build plan. The token is included in the credential filter (`audit.py` / `security.py`),
  so it's never written to logs or the audit trail — **provided it's at least 16 characters**;
  the scrub only redacts tokens over 4 characters, so `main()` separately hard-fails on a
  shorter token rather than silently accepting one the filter can't reliably catch.

githost-mcp will not ship `TRANSPORT=http` with a reachable port and no token configured, or with
a token under 16 characters — both are config errors, not a supported deploy shape. In stdio mode
(the default), neither control is relevant: there's no listening port to protect.

## Environment Variables

### Required

```env
AGENT_ID=dev                     # agent attribution — set per launcher
AUDIT_SIGNING_KEY=<32-byte-hex>  # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
ALLOWED_REPO_ROOTS=/home/user/repos/personal,/home/user/repos/work  # enforced on ALL tools (read + write)
```

`ALLOWED_REPO_ROOTS` is not strictly required if `AGENT_MANIFEST_PATH` resolves to a manifest
with `git_backed: true` `workspace_access` entries (see `AGENT_MANIFEST_PATH` below) — but one
of the two must yield at least one root, or every path-taking tool is denied.

```env
AGENT_MANIFEST_PATH=/home/user/.claude/manifests/dev-agent.yml  # optional — allowlist fallback
# Default: ~/.claude/manifests/{AGENT_ID}-agent.yml (only when AGENT_ID is set to a real identity)
# On forge, ecosystem.config.js overrides this per-process to
# /etc/forge/manifests/<agent>-agent.yml — see Deploy > Manifest allowlist path.
```

### Agent Identity (optional)

```env
GIT_AGENT_NAME=dev-agent         # git author/committer name (default: {AGENT_ID}-agent)
GIT_AGENT_EMAIL=dev@forge        # git author/committer email (default: {AGENT_ID}@forge)
```

### GitHub

```env
GITHUB_TOKEN=<PAT with repo scope>
GITHUB_OWNER=YourOrg
```

### Gitea

```env
GITEA_URL=https://gitea.example.com
GITEA_TOKEN=<PAT>
GITEA_OWNER=youruser
```

### GitLab

```env
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=<PAT>
```

### Registry

```env
PYPI_TOKEN=<API token>
NPM_TOKEN=<automation token>
```

### Logging (always on)

```env
LOG_FILE=/opt/appdata/githost-mcp/logs/githost-mcp.log
AUDIT_LOG_FILE=/opt/appdata/githost-mcp/audit/githost.jsonl
```

### Observability (all opt-in)

```env
# OTEL (SigNoz, Honeycomb, Grafana Tempo, Jaeger, Datadog — same env var)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# Loki
LOKI_URL=http://localhost:3100

# Prometheus scrape endpoint
METRICS_PORT=9185

# NATS
NATS_URL=nats://localhost:4222
```

> **`METRICS_PORT` is re-enabled on the forge PM2 deploy (0.9.0+), loopback-only.**
> `start_http_server()` in `observability.py` now binds `addr=127.0.0.1` explicitly, via
> a hardcoded `observability.METRICS_BIND_ADDR` (not itself configurable — no deployment
> wants a LAN-reachable metrics endpoint, and an env knob is how the previous `0.0.0.0`
> bind regressed). `ecosystem.config.js` sets one port per agent, 9620-9625, mirroring
> the 8620-8625 HTTP block. Verify with `ss -tlnp` showing `127.0.0.1` — a successful
> `curl` to localhost alone doesn't distinguish a loopback bind from a `0.0.0.0` one.

### Transport (optional — default stdio)

```env
TRANSPORT=stdio          # or "http" — see Deploy below
HTTP_HOST=127.0.0.1      # http mode only; must be loopback unless overridden below
HTTP_PORT=8620           # http mode only
GITHOST_MCP_ALLOW_NONLOOPBACK=   # set to "1" to bind a non-loopback HTTP_HOST (not recommended)
GITHOST_MCP_AUTH_TOKEN=  # required whenever TRANSPORT=http; must be >= 16 chars
```

## Installation

```bash
pip install githost-mcp

# With observability extras
pip install "githost-mcp[observability]"
```

## Launcher pattern (scoped-mcp subprocess)

```bash
#!/bin/bash
# run-githost-mcp-dev.sh
export AGENT_ID="dev"
export ALLOWED_REPO_ROOTS="/home/ted/repos/personal,/home/ted/repos/work"
export AUDIT_SIGNING_KEY="$(cat /run/secrets/githost_audit_key)"
export GITHUB_TOKEN="$(cat /run/secrets/github_token)"
export GITEA_TOKEN="$(cat /run/secrets/gitea_token)"
export LOG_FILE="/opt/appdata/githost-mcp/logs/githost-mcp.log"
export AUDIT_LOG_FILE="/opt/appdata/githost-mcp/audit/githost.jsonl"
exec /opt/agents/dev/venv/bin/python3 -m githost_mcp.server
```

This is `TRANSPORT=stdio` (the default): scoped-mcp spawns a fresh subprocess per call and
tears it down afterward. Simple, but Prometheus counters, OTEL spans, and Loki pushes rarely
survive that short a lifetime — they reset or drop every call.

## Deploy

Transport is selected by `TRANSPORT` (`stdio` default, or `http`) so both models are supported
by the same codebase — no fork, no rewrite to move between them.

| | `stdio` (default) | `http` |
|---|---|---|
| Process lifetime | One per scoped-mcp call, recycled every turn | Long-lived, one PM2 service per agent |
| Observability | Rotating file log + audit JSONL only — Prometheus/OTEL/Loki/NATS rarely survive teardown | All of it actually works — metrics accumulate, spans flush, NATS stays connected |
| Restart | N/A — recycled automatically | `pm2 restart githost-mcp-<agent>`, independent of scoped-mcp |
| Network surface | None | Local HTTP listener — must be loopback-bound + token-authed (see [Security Model](#http-transport-surface)) |

**Per-agent processes, not one shared process.** Each agent gets its own OS process (own
`AGENT_ID`, own tokens, own `ALLOWED_REPO_ROOTS`), so a compromised process can't see another
agent's credentials and `AGENT_ID` can't be spoofed via a request header. This is a deliberate
security tradeoff over a single shared process with per-request identity — see the build plan's
"Option A vs Option B" rationale if that tradeoff ever needs revisiting.

### Running as PM2 services

`ecosystem.config.js` in this repo builds one app per agent from a single `AGENT_ID -> {httpPort,
metricsPort}` map, reusing the same per-agent secrets files (`~/.secrets/githost-mcp-<agent>.env`)
and shared tokens (`~/.secrets/forge.env`) the stdio launchers already read — no separate secret
plumbing to maintain.

```bash
pm2 start ecosystem.config.js
pm2 save
```

Each service comes up with `TRANSPORT=http`, `HTTP_HOST=127.0.0.1`, its own `HTTP_PORT` /
`METRICS_PORT`, and `GITHOST_MCP_AUTH_TOKEN` sourced from `~/.secrets/forge.env`. Point
scoped-mcp's manifest at the corresponding `http://127.0.0.1:<port>/mcp/` URL with the token in
an `Authorization: Bearer` header (requires `type: http` on the manifest block — a bare
`{url, headers}` entry is silently skipped).

### Manifest allowlist path

When `ALLOWED_REPO_ROOTS` is unset for an agent, `AGENT_MANIFEST_PATH` (default
`~/.claude/manifests/{AGENT_ID}-agent.yml`) is the allowlist's only other source. On forge,
`ecosystem.config.js` overrides that default to `/etc/forge/manifests/<agent>-agent.yml` for
every agent process — a root-owned, `0644` copy published from `origin/main` by
`host-forge-scripts/scripts/agent-manifests-deploy.sh`, not a symlink into a live git working
tree. The target directory matters as much as the file mode: directory *write* permission
governs `rename`/`unlink` regardless of who owns the file inside it, so a root-owned file
under a `ted`-writable parent (e.g. `/opt/appdata`) isn't actually protected — anyone who can
write the directory can swap the file out from under its own permissions. `/etc` is root-owned
end to end, which is why the deployed copy lives there instead.

**Deployers must create and populate `/etc/forge/manifests` before unsetting
`ALLOWED_REPO_ROOTS` for any agent.** `config.py` does not fall back further if the target file
is missing or unreadable — the allowlist resolves empty (fail closed), and the agent loses all
repo access, rather than silently reusing the old default path.

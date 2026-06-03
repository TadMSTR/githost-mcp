# githost-mcp

Unified local + multi-provider git MCP server with a per-agent audit trail as a first-class feature.

Every tool call is tagged with the caller agent (`AGENT_ID`), written to a structured JSONL audit log, and write operations emit agent-bus events. Local git operations run entirely through gitpython — no subprocess, no injection risk.

## Why githost-mcp?

| Tool | What it covers | Gap |
|------|----------------|-----|
| `cyanheads/git-mcp-server` (TS) | 28 local git tools | No remote providers, no agent attribution, no audit trail |
| `poly-git-mcp` | GitHub + GitLab + Gitea | Wraps CLI tools — fragile, no audit, no agent ID |
| Official GitHub MCP | GitHub only | No local git, no Gitea/GitLab |
| Official Gitea MCP | Gitea only | No local git, no GitHub/GitLab |

**githost-mcp fills the gap:** local git + multi-provider remote via native APIs + per-agent structured audit trail.

## Tools (32 total)

### Local Git (11)
`git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_checkout`, `git_add`, `git_commit`, `git_push`, `git_pull`, `git_tag`

### GitHub (7)
`github_create_release`, `github_get_release`, `github_list_releases`, `github_workflow_list`, `github_workflow_status`, `github_pr_list`, `github_pr_comments`

### Gitea (4)
`gitea_create_release`, `gitea_get_release`, `gitea_list_releases`, `gitea_pr_list`

### GitLab (4)
`gitlab_create_release`, `gitlab_get_release`, `gitlab_list_releases`, `gitlab_mr_list`

### Release Orchestration (1)
`release` — coordinated multi-target release: git tag → GitHub/Gitea/GitLab release → PyPI → npm, with rollback on failure

### Registry (2)
`pypi_publish`, `npm_publish`

### Woodpecker CI (2)
`woodpecker_trigger`, `woodpecker_status`

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

All tools — both read and write — reject any path not under `ALLOWED_REPO_ROOTS`. Read tools (`git_status`, `git_diff`, `git_log`, `git_show`) and write tools (`git_add`, `git_commit`, `git_push`, `git_tag`, `git_checkout`, `git_branch create/delete`, `release`) are validated against the allowlist. **When `ALLOWED_REPO_ROOTS` is not set, all operations are disabled** — fail closed, not open.

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
- tool return values (masked before return)
- exception messages (caught at provider layer and re-raised without token value)

Each provider has its own env vars — a compromised GitHub token does not expose Gitea or GitLab credentials.

### HMAC tamper-evidence

`AUDIT_SIGNING_KEY` (required) is a server-side secret set in the launcher. Each JSONL entry includes `hmac: HMAC-SHA256(canonical_json, key)`. The `audit_log_query` tool verifies every returned entry. This is symmetric (same key signs and verifies) — it proves the file wasn't edited after write, not that the agent identity is genuine. Agent identity proof is the scoped-mcp layer's job.

## Environment Variables

### Required

```env
AGENT_ID=dev                     # agent attribution — set per launcher
AUDIT_SIGNING_KEY=<32-byte-hex>  # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
ALLOWED_REPO_ROOTS=/home/user/repos/personal,/home/user/repos/work  # enforced on ALL tools (read + write)
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

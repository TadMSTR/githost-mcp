# Changelog

## [0.4.0] — 2026-07-13

### Added
- Manifest-aware fallback for `ALLOWED_REPO_ROOTS`: when the env var is unset or empty,
  the allowlist now resolves from the requesting agent's manifest
  (`AGENT_MANIFEST_PATH`, default `~/.claude/manifests/{AGENT_ID}-agent.yml`), using
  `git_backed: true` entries from `workspace_access`. Explicit `ALLOWED_REPO_ROOTS`
  always wins when set — no behavior change for any existing deployment. Root-cause fix
  for GHOST-7, GHOST-5, GHOST-2 (allowlist drift).
- `pytest-cov` wired into CI (`.woodpecker.yml`) with a `--cov-fail-under=60` gate.

### Fixed
- `audit_log_query(since=...)` raised an uncaught `TypeError` (naive vs. aware datetime
  comparison) whenever `since` was passed without a timezone suffix — exactly the format
  shown in its own docstring example (`'2026-05-20'`).

### Changed
- Test coverage: `tools/registry.py` 0% → 96%, `tools/audit_query.py` 0% → 97%,
  `tools/release.py` 22% → 69%. Overall coverage 49% → 64%.

### Docs
- `AGENTS.md`: fixed `ALLOWED_REPO_ROOTS` colon/comma mismatch; documented the new
  `AGENT_MANIFEST_PATH` fallback.

### Security
- Audited 2026-07-13 (`githost-mcp-allowlist-and-coverage-2026-07`): 0 critical/high, 2
  medium, 1 low, 2 info. M-2 (manifest fallback doesn't honor `access: readonly`, dormant
  until manifest reconciliation) accepted as known risk — see
  `host-forge/security/accepted-risks.md`. M-1 (credential-masking asymmetry, pre-existing)
  and L-1 (transitive dependency CVEs) filed as follow-up tickets (GHOST-8, GHOST-9).

## [0.3.0] — 2026-06-01

### Added
- Per-agent git committer identity: `GIT_AGENT_NAME` and `GIT_AGENT_EMAIL` env vars set the
  git author and committer fields on every `git_commit` call. When unset, defaults are derived
  from `AGENT_ID` (`{id}-agent` / `{id}@forge`). Enables `git log --author` filtering by agent.
- `validate_read_path` in `security.py`: read tools (`git_status`, `git_diff`, `git_log`,
  `git_show`, `git_branch list`) now enforce the same `ALLOWED_REPO_ROOTS` allowlist as write
  tools, preventing unrestricted filesystem reads.
- `git_log` limit capped at 200 (consistent with existing list tool caps).

### Security
- Read tools previously bypassed `ALLOWED_REPO_ROOTS` entirely; an agent could call them on
  any path on the filesystem. All five read tools now call `validate_read_path` (same allowlist
  logic as write path, distinct error message).

## [0.2.1] — 2026-05-31

### Fixed
- All 5 Woodpecker tools now resolve `owner/name` to a numeric repo ID via
  `GET /api/repos/lookup/{owner}/{name}` before constructing any pipeline URL.
  Woodpecker 3.x removed name-based repo routing; all pipeline endpoints now
  require a numeric ID. Previously, name-based paths returned the SPA frontend
  (HTTP 200, HTML body), causing a JSON parse error in every Woodpecker tool.
- 404 from the lookup endpoint returns a clear "not found in Woodpecker" error
  rather than an uncaught exception.

## [0.2.0] — 2026-05-31

### Added
- `woodpecker_list_pipelines` — list recent pipeline runs with optional status filter; limit capped at 100
- `woodpecker_get_logs` — fetch step output by name or first step; truncated at 500 lines; log content excluded from audit trail
- `woodpecker_pipeline_cancel` — cancel a running pipeline; HITL gated in scoped-mcp manifests
- `gitea_pr_create` — open a pull request from a feature branch
- `gitea_pr_get` — get PR details including mergeable status and labels
- `gitea_pr_comment` — post a comment on a PR (via Gitea issues endpoint)
- `gitea_pr_merge` — merge a PR with style (merge/squash/rebase); HITL gated; validates merge_style enum
- `gitea_post_void` internal helper for 204 No Content responses (Gitea merge endpoint)
- `.woodpecker.yml` CI pipeline: test + pip-audit steps

### Security
- `repo` arg now validated against `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$` in all 9 Woodpecker/Gitea tools (IV-01)
- `limit` param capped at 100 in all list tools to prevent unbounded memory allocation (I-03)
- `step_id` cast to `int()` before URL construction in `woodpecker_get_logs` (I-04)
- `gitea_pr_merge` and `woodpecker_pipeline_cancel` HITL gated in sysadmin and developer scoped-mcp manifests
- Research agent manifest denylists both destructive tools

## [0.1.0] — 2026-05-27

### Added
- 32 tools across 8 categories: local git (11), GitHub (7), Gitea (4), GitLab (4),
  release orchestration (1), registry (2), Woodpecker CI (2), audit query (1)
- Per-agent JSONL audit trail with HMAC-SHA256 tamper-evidence for every tool call
- `ALLOWED_REPO_ROOTS` allowlist enforced on all write operations; fails closed when unset
- Credential filter processor via structlog — tokens scrubbed from all log output, JSONL
  entries, OTEL span attributes, and tool return values
- 401/403 sanitization in all three provider clients (GitHub, Gitea, GitLab)
- Coordinated multi-target release tool (`release`) with rollback: git tag → GitHub/Gitea/
  GitLab release → PyPI → npm; rollback deletes remote releases on downstream failure
- `pypi_publish` and `npm_publish` — tokens injected via `env=` kwarg, never in CLI args
- OTEL traces, Prometheus metrics, Loki push, NATS publisher — all opt-in via env vars
- FastMCP stdio transport; runs as a subprocess via scoped-mcp on forge

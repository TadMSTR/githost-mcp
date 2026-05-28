# Changelog

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

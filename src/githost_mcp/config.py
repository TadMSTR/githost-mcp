"""Environment variable loading and configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import structlog
import yaml

log = structlog.get_logger(__name__)


@dataclass
class Config:
    agent_id: str = "unknown"
    log_level: str = "INFO"
    log_file: str = "/opt/appdata/githost-mcp/logs/githost-mcp.log"
    audit_log_file: str = "/opt/appdata/githost-mcp/audit/githost.jsonl"
    audit_log_max_bytes: int = 10_485_760
    audit_log_backup_count: int = 5
    audit_signing_key: str = ""
    allowed_repo_roots: list[str] = field(default_factory=list)
    allowlist_source: str = "none"
    git_signing_key: str = ""
    git_agent_name: str = ""
    git_agent_email: str = ""
    # OTEL
    otel_endpoint: str = ""
    otel_protocol: str = "grpc"
    otel_service_name: str = "githost-mcp"
    # Loki
    loki_url: str = ""
    loki_labels: str = "app=githost-mcp"
    # Prometheus
    metrics_port: int | None = None
    # NATS
    nats_url: str = ""
    nats_subject_prefix: str = "githost"
    # GitHub
    github_token: str = ""
    github_owner: str = ""
    # Gitea
    gitea_url: str = ""
    gitea_token: str = ""
    gitea_owner: str = ""
    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    # Registry
    pypi_token: str = ""
    pypi_test_token: str = ""
    npm_token: str = ""
    # Woodpecker
    woodpecker_url: str = ""
    woodpecker_token: str = ""
    # Transport
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int | None = None
    allow_nonloopback: bool = False
    auth_token: str = ""


def _parse_allowed_roots(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()] if raw else []


def _default_manifest_path(agent_id: str) -> str:
    """Default AGENT_MANIFEST_PATH — only when a real agent identity is known."""
    if not agent_id or agent_id == "unknown":
        return ""
    return os.path.expanduser(f"~/.claude/manifests/{agent_id}-agent.yml")


def _load_manifest_roots(manifest_path: str) -> list[str]:
    """Parse git_backed workspace_access entries from an agent manifest.

    Returns an empty list on any parse failure so callers fail closed the same
    way an unset ALLOWED_REPO_ROOTS does, instead of raising.
    """
    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        log.warning("manifest_allowlist_load_failed", path=manifest_path, error=str(e))
        return []

    if not isinstance(data, dict):
        return []

    entries = data.get("workspace_access") or []
    if not isinstance(entries, list):
        return []

    roots = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("git_backed") is True and entry.get("path"):
            roots.append(os.path.expanduser(str(entry["path"])))
    return roots


def _resolve_allowed_roots(env_raw: str, manifest_path: str) -> tuple[list[str], str]:
    """Resolve the allowlist. Explicit ALLOWED_REPO_ROOTS always wins when set."""
    explicit = _parse_allowed_roots(env_raw)
    if explicit:
        return explicit, "env"

    if manifest_path and os.path.exists(manifest_path):
        manifest_roots = _load_manifest_roots(manifest_path)
        if manifest_roots:
            return manifest_roots, f"manifest:{manifest_path}"

    return [], "none"


def load_config() -> Config:
    metrics_raw = os.getenv("METRICS_PORT", "")
    http_port_raw = os.getenv("HTTP_PORT", "")
    _agent_id = os.getenv("AGENT_ID", "unknown")
    _git_agent_name = (
        (os.getenv("GIT_AGENT_NAME") or (f"{_agent_id}-agent" if _agent_id != "unknown" else ""))
        .replace("\n", "")
        .replace("\r", "")
        .replace("\0", "")
    )
    _git_agent_email = (
        (os.getenv("GIT_AGENT_EMAIL") or (f"{_agent_id}@forge" if _agent_id != "unknown" else ""))
        .replace("\n", "")
        .replace("\r", "")
        .replace("\0", "")
    )

    _manifest_path_raw = os.getenv("AGENT_MANIFEST_PATH", "") or _default_manifest_path(_agent_id)
    _manifest_path = os.path.expanduser(_manifest_path_raw) if _manifest_path_raw else ""
    _allowed_roots, _allowlist_source = _resolve_allowed_roots(
        os.getenv("ALLOWED_REPO_ROOTS", ""), _manifest_path
    )

    return Config(
        agent_id=_agent_id,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE", "/opt/appdata/githost-mcp/logs/githost-mcp.log"),
        audit_log_file=os.getenv("AUDIT_LOG_FILE", "/opt/appdata/githost-mcp/audit/githost.jsonl"),
        audit_log_max_bytes=int(os.getenv("AUDIT_LOG_MAX_BYTES", "10485760")),
        audit_log_backup_count=int(os.getenv("AUDIT_LOG_BACKUP_COUNT", "5")),
        audit_signing_key=os.getenv("AUDIT_SIGNING_KEY", ""),
        allowed_repo_roots=_allowed_roots,
        allowlist_source=_allowlist_source,
        git_signing_key=os.getenv("GIT_SIGNING_KEY", ""),
        git_agent_name=_git_agent_name,
        git_agent_email=_git_agent_email,
        otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        otel_protocol=os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "githost-mcp"),
        loki_url=os.getenv("LOKI_URL", ""),
        loki_labels=os.getenv("LOKI_LABELS", "app=githost-mcp"),
        metrics_port=int(metrics_raw) if metrics_raw else None,
        nats_url=os.getenv("NATS_URL", ""),
        nats_subject_prefix=os.getenv("NATS_SUBJECT_PREFIX", "githost"),
        github_token=os.getenv("GITHUB_TOKEN", ""),
        github_owner=os.getenv("GITHUB_OWNER", ""),
        gitea_url=os.getenv("GITEA_URL", ""),
        gitea_token=os.getenv("GITEA_TOKEN", ""),
        gitea_owner=os.getenv("GITEA_OWNER", ""),
        gitlab_url=os.getenv("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.getenv("GITLAB_TOKEN", ""),
        pypi_token=os.getenv("PYPI_TOKEN", ""),
        pypi_test_token=os.getenv("PYPI_TEST_TOKEN", ""),
        npm_token=os.getenv("NPM_TOKEN", ""),
        woodpecker_url=os.getenv("WOODPECKER_URL", ""),
        woodpecker_token=os.getenv("WOODPECKER_TOKEN", ""),
        transport=os.getenv("TRANSPORT", "stdio"),
        http_host=os.getenv("HTTP_HOST", "127.0.0.1"),
        http_port=int(http_port_raw) if http_port_raw else None,
        allow_nonloopback=os.getenv("GITHOST_MCP_ALLOW_NONLOOPBACK", "") == "1",
        auth_token=os.getenv("GITHOST_MCP_AUTH_TOKEN", ""),
    )


_config: Config | None = None
_loading: bool = False


def get_config() -> Config:
    global _config, _loading
    if _config is None:
        if _loading:
            # Re-entered while load_config() is still running — a log call made
            # during loading (e.g. a manifest-parse warning) flows through
            # structlog's credential-filter processor, which itself calls
            # get_config(). Return an empty placeholder so the filter finds no
            # tokens to scrub, instead of recursing into load_config() again.
            return Config()
        _loading = True
        try:
            _config = load_config()
        finally:
            _loading = False
        log.info(
            "allowlist_resolved",
            source=_config.allowlist_source,
            root_count=len(_config.allowed_repo_roots),
        )
    return _config


def reset_config() -> None:
    """Reset cached config — used in tests."""
    global _config, _loading
    _config = None
    _loading = False

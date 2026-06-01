"""Environment variable loading and configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


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
    metrics_port: Optional[int] = None
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


def _parse_allowed_roots(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()] if raw else []


def load_config() -> Config:
    metrics_raw = os.getenv("METRICS_PORT", "")
    _agent_id = os.getenv("AGENT_ID", "unknown")
    _git_agent_name = (os.getenv("GIT_AGENT_NAME") or (
        f"{_agent_id}-agent" if _agent_id != "unknown" else ""
    )).replace("\n", "").replace("\r", "").replace("\0", "")
    _git_agent_email = (os.getenv("GIT_AGENT_EMAIL") or (
        f"{_agent_id}@forge" if _agent_id != "unknown" else ""
    )).replace("\n", "").replace("\r", "").replace("\0", "")
    return Config(
        agent_id=_agent_id,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE", "/opt/appdata/githost-mcp/logs/githost-mcp.log"),
        audit_log_file=os.getenv("AUDIT_LOG_FILE", "/opt/appdata/githost-mcp/audit/githost.jsonl"),
        audit_log_max_bytes=int(os.getenv("AUDIT_LOG_MAX_BYTES", "10485760")),
        audit_log_backup_count=int(os.getenv("AUDIT_LOG_BACKUP_COUNT", "5")),
        audit_signing_key=os.getenv("AUDIT_SIGNING_KEY", ""),
        allowed_repo_roots=_parse_allowed_roots(os.getenv("ALLOWED_REPO_ROOTS", "")),
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
    )


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset cached config — used in tests."""
    global _config
    _config = None

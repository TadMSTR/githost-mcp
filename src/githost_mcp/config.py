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
    # These govern the audit JSONL. They used to be consumed by the *application*
    # log's handler instead, so the audit log itself never rotated at all.
    audit_log_max_bytes: int = 10_485_760
    audit_log_backup_count: int = 5
    # The application log's own knobs, defaulted to the audit values so no
    # deployed env file has to change.
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5
    audit_signing_key: str = ""
    # Deprecated alias of allowed_write_roots, kept so any caller not yet
    # migrated to the read/write split keeps working. release.py's docstring
    # names it, but the code there only ever calls validate_write_path(), so
    # aliasing to the write list (not read+write) preserves its behaviour.
    allowed_repo_roots: list[str] = field(default_factory=list)
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    write_globs: list[str] = field(default_factory=list)
    write_globs_deny: list[str] = field(default_factory=list)
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


_DEFAULT_POLICY_PATH = "/etc/forge/workspace-policy.yml"


def _default_manifest_path(agent_id: str) -> str:
    """Default AGENT_MANIFEST_PATH — only when a real agent identity is known."""
    if not agent_id or agent_id == "unknown":
        return ""
    return os.path.expanduser(f"~/.claude/manifests/{agent_id}-agent.yml")


def _load_manifest_roots(manifest_path: str) -> tuple[list[str], list[str]]:
    """Parse git_backed workspace_access entries from an agent manifest.

    Returns ``(read_roots, write_roots)``. ``access: readwrite`` grants both;
    ``access: readonly`` grants read only. Previously a readonly entry was
    dropped entirely — the manifest loader admitted only readwrite entries, so
    ``access: readonly`` granted nothing at all, not even read. That was the
    root cause of the recurring per-agent read-grant drift (#203/#332/#308,
    workspace-policy plan 2026-08). This is a behaviour change to what an
    existing ``access: readonly`` manifest key means — see CHANGELOG.

    Any other ``access:`` value, including a missing one, grants nothing.

    Returns ``([], [])`` on any parse failure so callers fail closed the same
    way an unset ALLOWED_REPO_ROOTS does, instead of raising.
    """
    try:
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        log.warning("manifest_allowlist_load_failed", path=manifest_path, error=str(e))
        return [], []

    if not isinstance(data, dict):
        return [], []

    entries = data.get("workspace_access") or []
    if not isinstance(entries, list):
        return [], []

    read_roots: list[str] = []
    write_roots: list[str] = []
    for entry in entries:
        if not (isinstance(entry, dict) and entry.get("git_backed") is True and entry.get("path")):
            continue
        access = entry.get("access")
        path = os.path.expanduser(str(entry["path"]))
        if access == "readwrite":
            read_roots.append(path)
            write_roots.append(path)
        elif access == "readonly":
            read_roots.append(path)
        else:
            # Skipping here narrows the allowlist. Log it so a mysteriously
            # missing root is diagnosable rather than silent.
            log.warning(
                "manifest_allowlist_entry_skipped",
                path=manifest_path,
                entry_path=str(entry["path"]),
                access=access,
                reason="access is neither readwrite nor readonly",
            )
    return read_roots, write_roots


def _load_policy(
    agent_id: str, policy_path: str
) -> tuple[list[str], list[str], list[str], list[str]] | None:
    """Parse this agent's grant from /etc/forge/workspace-policy.yml.

    Returns ``(read_roots, write_roots, write_globs, write_globs_deny)``, or
    ``None`` if the file is missing, unreadable, or not a YAML mapping — the
    caller falls through to the manifest in that case, same as a missing
    manifest file today.

    Once the file loads successfully it is authoritative for this agent, even
    if that agent has no entry and the result is all-empty: `agents:` not
    listing an agent, and `explicit_agents:` being itemized rather than
    inherited, are both deliberate denials, not gaps to patch by falling back
    to the manifest. A caller must not treat an empty-but-successfully-parsed
    result as "no match" — see _resolve_allowed_roots.
    """
    try:
        with open(policy_path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        log.warning("policy_load_failed", path=policy_path, error=str(e))
        return None

    if not isinstance(data, dict):
        log.warning("policy_load_failed", path=policy_path, error="not a mapping")
        return None

    roots_raw = data.get("roots") or []
    root_paths = (
        [
            os.path.expanduser(str(r["path"]))
            for r in roots_raw
            if isinstance(r, dict) and r.get("path")
        ]
        if isinstance(roots_raw, list)
        else []
    )

    agents = data.get("agents")
    agents = agents if isinstance(agents, dict) else {}
    agent_entry = agents.get(agent_id)
    agent_entry = agent_entry if isinstance(agent_entry, dict) else None

    explicit_agents = data.get("explicit_agents")
    explicit_agents = explicit_agents if isinstance(explicit_agents, dict) else {}
    explicit_entry = explicit_agents.get(agent_id)
    explicit_entry = explicit_entry if isinstance(explicit_entry, dict) else None

    read_roots: list[str] = []
    write_roots: list[str] = []
    write_globs: list[str] = []
    write_globs_deny: list[str] = []

    if agent_entry is not None:
        # Listed in `agents:` — gets read to every root when default_read: all,
        # regardless of that agent's own write_roots (e.g. research/security
        # are listed with write_roots: [] and still get read).
        if data.get("default_read") == "all":
            read_roots = list(root_paths)
        write_roots = _as_str_list(agent_entry.get("write_roots"))
        write_globs = _as_str_list(agent_entry.get("write_globs"))
        write_globs_deny = _as_str_list(agent_entry.get("write_globs_deny"))
    elif explicit_entry is not None:
        # Narrow, itemized grant for an agent outside the platform default —
        # does not inherit default_read, must name its own roots.
        read_roots = _as_str_list(explicit_entry.get("read_roots"))
        write_roots = _as_str_list(explicit_entry.get("write_roots"))
        write_globs = _as_str_list(explicit_entry.get("write_globs"))
        write_globs_deny = _as_str_list(explicit_entry.get("write_globs_deny"))

    return read_roots, write_roots, write_globs, write_globs_deny


def _as_str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [os.path.expanduser(str(v)) for v in raw]


def _resolve_allowed_roots(
    agent_id: str, env_raw: str, policy_path: str, manifest_path: str
) -> tuple[list[str], list[str], list[str], list[str], str]:
    """Resolve read/write roots. First match wins: env > policy > manifest > empty.

    Returns ``(read_roots, write_roots, write_globs, write_globs_deny, source)``.
    """
    explicit = _parse_allowed_roots(env_raw)
    if explicit:
        # The break-glass env override is coarser than the policy/manifest
        # split by design — it grants the same roots for read and write.
        return explicit, explicit, [], [], "env"

    if policy_path and os.path.exists(policy_path):
        policy_result = _load_policy(agent_id, policy_path)
        if policy_result is not None:
            read_roots, write_roots, write_globs, write_globs_deny = policy_result
            return read_roots, write_roots, write_globs, write_globs_deny, f"policy:{policy_path}"

    if manifest_path and os.path.exists(manifest_path):
        read_roots, write_roots = _load_manifest_roots(manifest_path)
        if read_roots or write_roots:
            return read_roots, write_roots, [], [], f"manifest:{manifest_path}"

    return [], [], [], [], "none"


def _env_int(name: str, raw: str) -> int:
    """Parse an integer env var, naming the variable and the bad value on failure.

    get_config() runs at import time (server.py), so a malformed METRICS_PORT used
    to kill the process with a bare `invalid literal for int()` traceback that did
    not say which variable was at fault.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


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
    _policy_path_raw = os.getenv("WORKSPACE_POLICY_PATH", "") or _DEFAULT_POLICY_PATH
    _policy_path = os.path.expanduser(_policy_path_raw)
    _read_roots, _write_roots, _write_globs, _write_globs_deny, _allowlist_source = (
        _resolve_allowed_roots(
            _agent_id, os.getenv("ALLOWED_REPO_ROOTS", ""), _policy_path, _manifest_path
        )
    )

    # The application log defaults to the audit log's sizing so that splitting the
    # two settings apart needs no change to any deployed env file.
    _audit_max_bytes_raw = os.getenv("AUDIT_LOG_MAX_BYTES", "10485760")
    _audit_backup_raw = os.getenv("AUDIT_LOG_BACKUP_COUNT", "5")

    return Config(
        agent_id=_agent_id,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE", "/opt/appdata/githost-mcp/logs/githost-mcp.log"),
        audit_log_file=os.getenv("AUDIT_LOG_FILE", "/opt/appdata/githost-mcp/audit/githost.jsonl"),
        audit_log_max_bytes=_env_int("AUDIT_LOG_MAX_BYTES", _audit_max_bytes_raw),
        audit_log_backup_count=_env_int("AUDIT_LOG_BACKUP_COUNT", _audit_backup_raw),
        log_max_bytes=_env_int("LOG_MAX_BYTES", os.getenv("LOG_MAX_BYTES", _audit_max_bytes_raw)),
        log_backup_count=_env_int(
            "LOG_BACKUP_COUNT", os.getenv("LOG_BACKUP_COUNT", _audit_backup_raw)
        ),
        audit_signing_key=os.getenv("AUDIT_SIGNING_KEY", ""),
        allowed_repo_roots=_write_roots,
        allowed_read_roots=_read_roots,
        allowed_write_roots=_write_roots,
        write_globs=_write_globs,
        write_globs_deny=_write_globs_deny,
        allowlist_source=_allowlist_source,
        git_signing_key=os.getenv("GIT_SIGNING_KEY", ""),
        git_agent_name=_git_agent_name,
        git_agent_email=_git_agent_email,
        otel_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        otel_protocol=os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "githost-mcp"),
        loki_url=os.getenv("LOKI_URL", ""),
        loki_labels=os.getenv("LOKI_LABELS", "app=githost-mcp"),
        metrics_port=_env_int("METRICS_PORT", metrics_raw) if metrics_raw else None,
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
        http_port=_env_int("HTTP_PORT", http_port_raw) if http_port_raw else None,
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
            read_root_count=len(_config.allowed_read_roots),
            write_root_count=len(_config.allowed_write_roots),
        )
    return _config


def reset_config() -> None:
    """Reset cached config — used in tests."""
    global _config, _loading
    _config = None
    _loading = False

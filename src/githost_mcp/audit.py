"""Structured JSONL audit log with HMAC tamper-evidence and credential filtering."""

from __future__ import annotations

import contextlib
import hashlib
import hmac as _hmac
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any

import structlog

from .config import get_config

# Bound at init_logging() time
_agent_id: str = "unknown"
_audit_log_path: str = ""
_signing_key: bytes = b""

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Credential filter (structlog processor)
# ---------------------------------------------------------------------------


def _credential_filter(logger: Any, method: str, event_dict: dict) -> dict:
    config = get_config()
    tokens = [
        t
        for t in [
            config.github_token,
            config.gitea_token,
            config.gitlab_token,
            config.woodpecker_token,
            config.pypi_token,
            config.pypi_test_token,
            config.npm_token,
            config.audit_signing_key,
            config.auth_token,
        ]
        if t and len(t) > 4
    ]
    if not tokens:
        return event_dict
    for key, val in list(event_dict.items()):
        if isinstance(val, str):
            for tok in tokens:
                if tok in val:
                    event_dict[key] = val.replace(tok, "***")
    return event_dict


# ---------------------------------------------------------------------------
# Logging initialisation
# ---------------------------------------------------------------------------


def init_logging() -> None:
    global _agent_id, _audit_log_path, _signing_key
    config = get_config()
    _agent_id = config.agent_id
    _audit_log_path = config.audit_log_file
    _signing_key = config.audit_signing_key.encode() if config.audit_signing_key else b""

    log_dir = os.path.dirname(config.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    level = getattr(logging, config.log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _credential_filter,
    ]

    stderr_handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handlers: list[logging.Handler] = [stderr_handler]
    if config.log_file:
        handlers.append(
            RotatingFileHandler(
                config.log_file,
                maxBytes=config.log_max_bytes,
                backupCount=config.log_backup_count,
            )
        )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(agent_id=_agent_id)


# ---------------------------------------------------------------------------
# HMAC helpers
# ---------------------------------------------------------------------------


def _compute_hmac(entry_without_hmac: dict) -> str:
    canonical = json.dumps(entry_without_hmac, sort_keys=True, separators=(",", ":"))
    return _hmac.new(_signing_key, canonical.encode(), hashlib.sha256).hexdigest()


def verify_entry_hmac(entry: dict) -> bool:
    """Return True if entry HMAC is valid (or if no signing key is configured)."""
    if not _signing_key:
        return True
    stored = entry.get("hmac", "")
    without_hmac = {k: v for k, v in entry.items() if k != "hmac"}
    expected = _compute_hmac(without_hmac)
    return _hmac.compare_digest(stored, expected)


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------

_write_lock = threading.Lock()


def audit_backup_paths(path: str, backup_count: int) -> list[str]:
    """Rotated backups for `path`, newest first. Shared with audit_log_query so the
    two agree on the naming scheme."""
    return [f"{path}.{i}" for i in range(1, backup_count + 1)]


def _rotate_if_needed(incoming_bytes: int) -> None:
    """Roll the audit JSONL when the next line would exceed audit_log_max_bytes.

    Matches RotatingFileHandler's scheme (`.jsonl.1` is newest) so the layout is
    the familiar one, but done inline: write_audit_entry appends directly rather
    than going through the logging stack.

    Existing entries are only ever renamed, never truncated or rewritten — the
    audit trail is the tamper-evident record. HMACs are per-entry rather than a
    chain, so a rotated entry verifies exactly as it did before the rename.

    Callers must hold _write_lock.
    """
    config = get_config()
    max_bytes = config.audit_log_max_bytes
    backup_count = config.audit_log_backup_count
    if max_bytes <= 0 or backup_count <= 0:
        return  # rotation disabled

    try:
        current = os.path.getsize(_audit_log_path)
    except OSError:
        return  # no file yet, or unreadable — the append below will report it

    if current == 0 or current + incoming_bytes <= max_bytes:
        return

    # Drop the oldest, then shift each backup down one. The final rename moves the
    # live file aside; the next append recreates it.
    oldest = f"{_audit_log_path}.{backup_count}"
    with contextlib.suppress(OSError):
        os.remove(oldest)
    for i in range(backup_count - 1, 0, -1):
        src, dst = f"{_audit_log_path}.{i}", f"{_audit_log_path}.{i + 1}"
        if os.path.exists(src):
            with contextlib.suppress(OSError):
                os.replace(src, dst)
    with contextlib.suppress(OSError):
        os.replace(_audit_log_path, f"{_audit_log_path}.1")
    log.info("audit_log_rotated", path=_audit_log_path, size_bytes=current)


def _scrub_dict(d: dict) -> dict:
    """Recursively replace credential values in a dict."""
    config = get_config()
    tokens = [
        t
        for t in [
            config.github_token,
            config.gitea_token,
            config.gitlab_token,
            config.woodpecker_token,
            config.pypi_token,
            config.pypi_test_token,
            config.npm_token,
            config.audit_signing_key,
            config.auth_token,
        ]
        if t and len(t) > 4
    ]
    if not tokens:
        return d

    def _scrub_val(val):
        if isinstance(val, str):
            for tok in tokens:
                val = val.replace(tok, "***")
            return val
        if isinstance(val, dict):
            return {k: _scrub_val(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_scrub_val(i) for i in val]
        return val

    return {k: _scrub_val(v) for k, v in d.items()}


def write_audit_entry(
    tool: str,
    provider: str,
    repo: str,
    params: dict,
    result: str,
    duration_ms: int,
) -> None:
    # Scrub credentials from params and result before writing
    config = get_config()
    tokens = [
        t
        for t in [
            config.github_token,
            config.gitea_token,
            config.gitlab_token,
            config.woodpecker_token,
            config.pypi_token,
            config.pypi_test_token,
            config.npm_token,
            config.audit_signing_key,
            config.auth_token,
        ]
        if t and len(t) > 4
    ]

    def _scrub_str(s: str) -> str:
        for tok in tokens:
            s = s.replace(tok, "***")
        return s

    def _scrub_val(val):
        if isinstance(val, str):
            return _scrub_str(val)
        if isinstance(val, dict):
            return {k: _scrub_val(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_scrub_val(i) for i in val]
        return val

    safe_params = {k: _scrub_val(v) for k, v in params.items()}
    safe_result = _scrub_str(result)

    entry: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "agent_id": _agent_id,
        "tool": tool,
        "provider": provider,
        "repo": repo,
        "params": safe_params,
        "result": safe_result,
        "duration_ms": duration_ms,
    }
    if _signing_key:
        entry["hmac"] = _compute_hmac(entry)

    audit_dir = os.path.dirname(_audit_log_path)
    if audit_dir:
        os.makedirs(audit_dir, exist_ok=True)

    line = json.dumps(entry) + "\n"
    try:
        # One lock for check-and-rotate plus write. Under the HTTP transport this
        # runs on a threadpool, so an unguarded rotate would race a concurrent
        # append and lose entries.
        with _write_lock:
            _rotate_if_needed(len(line.encode()))
            with open(_audit_log_path, "a") as f:
                f.write(line)
    except OSError as e:
        log.warning("audit_write_failed", error=str(e))


# ---------------------------------------------------------------------------
# Timing context helper
# ---------------------------------------------------------------------------


class AuditCtx:
    """Capture duration and write audit entry on finish()."""

    def __init__(self, tool: str, provider: str, repo: str, params: dict) -> None:
        self.tool = tool
        self.provider = provider
        self.repo = repo
        self.params = params
        self._t0 = time.perf_counter()

    def finish(self, result: str = "ok") -> None:
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        write_audit_entry(self.tool, self.provider, self.repo, self.params, result, duration_ms)

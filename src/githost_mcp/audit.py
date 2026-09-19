"""Structured JSONL audit log with HMAC tamper-evidence and credential filtering."""

from __future__ import annotations

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
from .errors import classify_reason
from .observability import emit_tool_event_sync
from .security import scrub

#: Cap on any single string written into an audit record. `repo` at an audit_rejection()
#: call site is precisely the string that just FAILED validation — arbitrary agent-supplied
#: text — and it lands in a durable, HMAC-signed log that nothing prunes. json.dumps()
#: escaping keeps it from breaking the JSONL framing, so this is bloat rather than
#: corruption, but there was no bound anywhere in the write path. Generous enough that no
#: real repo path, branch name or exception message is touched.
MAX_AUDIT_FIELD_CHARS = 2048

# Bound at init_logging() time
_agent_id: str = "unknown"
_audit_log_path: str = ""
_signing_key: bytes = b""

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Credential filter (structlog processor)
# ---------------------------------------------------------------------------


def _config_tokens() -> list[str]:
    """Every configured secret, long enough to be worth matching on."""
    config = get_config()
    return [
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


def _truncate(text: str) -> str:
    """Bound a single audit field, saying so rather than silently losing the tail."""
    if len(text) <= MAX_AUDIT_FIELD_CHARS:
        return text
    return text[:MAX_AUDIT_FIELD_CHARS] + f"…[truncated, {len(text)} chars]"


def _truncate_deep(val: Any) -> Any:
    """Apply _truncate() to every string in a nested structure. Run it last."""
    if isinstance(val, str):
        return _truncate(val)
    if isinstance(val, dict):
        return {k: _truncate_deep(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_truncate_deep(i) for i in val]
    if isinstance(val, tuple):
        return tuple(_truncate_deep(i) for i in val)
    return val


def _scrub_value(val: Any, tokens: list[str]) -> Any:
    """Replace every configured token anywhere in a nested structure.

    Deliberately does NOT truncate. Length bounding happens after every scrubbing pass
    has run — cutting a string first can split a credential across the boundary, leaving
    a prefix that mask_credentials() no longer recognises and therefore no longer masks.
    """
    if isinstance(val, str):
        for tok in tokens:
            val = val.replace(tok, "***")
        return val
    if isinstance(val, dict):
        return {k: _scrub_value(v, tokens) for k, v in val.items()}
    if isinstance(val, list):
        return [_scrub_value(i, tokens) for i in val]
    if isinstance(val, tuple):
        return tuple(_scrub_value(i, tokens) for i in val)
    return val


def _credential_filter(logger: Any, method: str, event_dict: dict) -> dict:
    tokens = _config_tokens()
    if not tokens:
        return event_dict
    # Recurses. This used to scrub top-level strings only, so a token inside a
    # dict or list passed to a log call went to the log verbatim — while the
    # audit writer alongside it recursed correctly.
    for key, val in list(event_dict.items()):
        event_dict[key] = _scrub_value(val, tokens)
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

    # Emitted after structlog is configured, so it lands in the real log rather
    # than a bootstrap logger. Without this the unsigned condition is only
    # discoverable by querying the audit log and noticing the absent hmac field —
    # which is exactly how it went unnoticed for the writer agent from deploy
    # until 2026-08-01 (vikunja#301, id 312). Named per-agent because the key is
    # supplied per-agent from ~/.secrets/githost-mcp-<agent>.env, so this is
    # always one agent's problem, not the fleet's.
    if not _signing_key:
        log.warning(
            "audit_signing_key_unset",
            agent_id=_agent_id,
            audit_log_file=_audit_log_path,
            detail=(
                "AUDIT_SIGNING_KEY is not set — audit entries for this agent are written "
                "with no tamper evidence and audit_log_query will report them as unsigned."
            ),
        )


# ---------------------------------------------------------------------------
# HMAC helpers
# ---------------------------------------------------------------------------


def _compute_hmac(entry_without_hmac: dict) -> str:
    canonical = json.dumps(entry_without_hmac, sort_keys=True, separators=(",", ":"))
    return _hmac.new(_signing_key, canonical.encode(), hashlib.sha256).hexdigest()


# Integrity states for a single audit entry. Four distinct facts, deliberately not
# collapsed into a bool: "we checked and it is intact" and "there was nothing to
# check" are not the same answer, and reporting the second as the first is a false
# assurance rather than a missing one (vikunja#301, id 312).
INTEGRITY_VERIFIED = "verified"  # signed, key present, HMAC matches
INTEGRITY_TAMPERED = "tampered"  # signed, key present, HMAC does not match
INTEGRITY_UNSIGNED = "unsigned"  # entry carries no hmac — it was never signed
INTEGRITY_UNVERIFIABLE = "unverifiable"  # entry is signed but no key is configured here


def verify_entry_integrity(entry: dict) -> str:
    """Classify one audit entry's integrity as one of the INTEGRITY_* constants.

    The absence of an `hmac` field is a property of the entry itself, so it is
    reported as `unsigned` whether or not a key is configured now — an entry
    written during an unsigned window stays identifiable after the key is added.
    A signed entry with no key available to check it is `unverifiable`: we hold
    no opinion on it, which is different from finding it intact.
    """
    if "hmac" not in entry:
        return INTEGRITY_UNSIGNED
    if not _signing_key:
        return INTEGRITY_UNVERIFIABLE
    stored = entry.get("hmac", "")
    without_hmac = {k: v for k, v in entry.items() if k != "hmac"}
    expected = _compute_hmac(without_hmac)
    return INTEGRITY_VERIFIED if _hmac.compare_digest(stored, expected) else INTEGRITY_TAMPERED


def verify_entry_hmac(entry: dict) -> bool:
    """Return True only if the entry was positively verified against a signing key.

    This used to return True when no signing key was configured, which meant an
    unsigned entry — one with no tamper evidence at all — was reported as intact.
    An unsigned or unverifiable entry now returns False. Callers that need to tell
    "failed verification" from "nothing to verify" must use verify_entry_integrity();
    this wrapper cannot express the difference and is kept only for callers that
    genuinely want the strict question "is this entry known-good?".
    """
    return verify_entry_integrity(entry) == INTEGRITY_VERIFIED


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
    #
    # Every step tolerates OSError so a rotation failure never takes down the
    # write that triggered it — but each one logs. A silent failure here degrades
    # invisibly: a failed final rename means the live file is never rotated and
    # every subsequent write re-attempts and re-fails, growing past
    # audit_log_max_bytes with no operator signal at all.
    oldest = f"{_audit_log_path}.{backup_count}"
    try:
        os.remove(oldest)
    except FileNotFoundError:
        pass  # nothing to age out yet — the normal case
    except OSError as e:
        log.warning("audit_rotation_step_failed", step="remove_oldest", path=oldest, error=str(e))

    for i in range(backup_count - 1, 0, -1):
        src, dst = f"{_audit_log_path}.{i}", f"{_audit_log_path}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError as e:
                log.warning(
                    "audit_rotation_step_failed",
                    step="shift_backup",
                    src=src,
                    dst=dst,
                    error=str(e),
                )

    try:
        os.replace(_audit_log_path, f"{_audit_log_path}.1")
    except OSError as e:
        # The one that matters: the live file did not roll, so it will keep
        # growing and this same failure will repeat on every write.
        log.error(
            "audit_rotation_failed",
            path=_audit_log_path,
            size_bytes=current,
            max_bytes=max_bytes,
            error=str(e),
        )
        return

    log.info("audit_log_rotated", path=_audit_log_path, size_bytes=current)


def write_audit_entry(
    tool: str,
    provider: str,
    repo: str,
    params: dict,
    result: str,
    duration_ms: int,
    reason: str | None = None,
) -> None:
    """Append one signed audit record.

    `reason` is the human-readable detail behind a non-ok `result` — usually an
    exception message. It is an audit-record field and deliberately NOT a metric
    label: messages carry repo paths and branch names, which would make Prometheus
    cardinality unbounded. The bounded counterpart is `reason_class`, emitted to the
    metrics backends by AuditCtx.finish().

    It is scrubbed harder than `result` is. `_scrub_value()` only replaces token
    values githost-mcp has in its own config, so a one-off PAT a human embedded in a
    git remote by hand survives it; security.scrub() additionally strips URL userinfo
    by shape. Exception text is exactly where such a URL surfaces, so both run.
    """
    # Scrub credentials from params and result before writing
    tokens = _config_tokens()
    safe_params = {k: _truncate_deep(_scrub_value(v, tokens)) for k, v in params.items()}
    safe_result = _scrub_value(result, tokens)

    entry: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "agent_id": _agent_id,
        "tool": tool,
        "provider": provider,
        "repo": _truncate(_scrub_value(repo, tokens)),
        "params": safe_params,
        "result": safe_result,
        "duration_ms": duration_ms,
    }
    if reason is not None:
        entry["reason"] = _truncate(scrub(_scrub_value(reason, tokens)))
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

    def finish(self, result: str = "ok", reason: str | BaseException | None = None) -> None:
        """Write the audit record and emit the metrics for this call.

        `reason` takes the exception itself wherever one is in scope, not just its
        message: the type is what classify_reason() maps to a bounded reason_class,
        and a string would throw that away. A plain string is accepted for the call
        sites that reject before any exception exists.

        This is also the only place tool metrics are produced. They previously had no
        producer at all — emit_tool_event() was defined and called by nothing, so
        githost_tool_calls_total had never incremented in production and the metrics
        ports served zero githost series.
        """
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        exc = reason if isinstance(reason, BaseException) else None
        reason_text = None if reason is None else str(reason)

        write_audit_entry(
            self.tool,
            self.provider,
            self.repo,
            self.params,
            result,
            duration_ms,
            reason=reason_text,
        )

        # Any result that is not a success is a denial for counting purposes —
        # `denied:*` policy refusals and `error:*` failures alike. The question the
        # counter answers is "which githost-mcp limits do agents actually hit", and
        # an error the caller cannot get past is a limit regardless of which prefix
        # it carries. "ok:already_absent" and friends are successes.
        reason_class = None if result.startswith("ok") else classify_reason(result, exc)

        try:
            emit_tool_event_sync(
                self.tool,
                self.provider,
                os.path.basename(self.repo.rstrip("/")) if self.repo else "",
                result,
                duration_ms,
                reason_class,
            )
        except Exception as exc_emit:  # pragma: no cover - defence in depth
            # Telemetry must never fail a tool call that otherwise succeeded.
            log.warning("tool_event_emit_failed", error=str(exc_emit))


def audit_rejection(
    tool: str,
    provider: str,
    repo: str,
    reason_class: str,
    err: dict | str,
) -> dict:
    """Record a pre-flight validation rejection, and return the caller-facing error.

    Tools validate `repo`/`project`/`tag`/enum arguments *before* they build their
    AuditCtx, and until now those guards simply returned an error dict: no audit
    record, no metric, no trace. An agent hitting one of those limits left nothing
    behind at all — which is the single case this telemetry exists to make visible.

    `tool`, `provider` and `repo` are taken from the same expressions the tool's own
    AuditCtx uses, so a rejected call and a successful call of the same tool produce
    comparably-shaped records.

    Returns the error dict so the guard stays a one-liner at the call site.
    """
    message = err["error"] if isinstance(err, dict) else err
    AuditCtx(tool, provider, repo, {"repo": repo}).finish(f"denied:{reason_class}", message)
    return {"error": message}

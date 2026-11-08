"""Structured JSONL audit log with HMAC tamper-evidence and credential filtering."""

from __future__ import annotations

import contextvars
import hashlib
import hmac as _hmac
import json
import logging
import os
import sys
import threading
import time
import types
from contextlib import nullcontext
from logging.handlers import RotatingFileHandler
from typing import Any

import structlog

from . import correlation as _correlation
from .config import get_config
from .effects import bound_effect, requires_effect
from .errors import classify_reason
from .observability import (
    child_span,
    emit_tool_event_sync,
    end_tool_span,
    start_tool_span,
    trace_id_hex,
)
from .security import scrub

#: Cap on any single string written into an audit record. `repo` at an audit_rejection()
#: call site is precisely the string that just FAILED validation — arbitrary agent-supplied
#: text — and it lands in a durable, HMAC-signed log that nothing prunes. json.dumps()
#: escaping keeps it from breaking the JSONL framing, so this is bloat rather than
#: corruption, but there was no bound anywhere in the write path. Generous enough that no
#: real repo path, branch name or exception message is touched.
MAX_AUDIT_FIELD_CHARS = 2048

#: Level of the stderr handler (PM2's -error.log), independent of LOG_LEVEL.
STDERR_LEVEL = logging.WARNING

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
    # WARNING and above only, whatever LOG_LEVEL says. PM2 writes stderr to
    # githost-mcp-<agent>-error.log, and with the root level it received every INFO line:
    # 764 of 764 lines on 2026-11-07 (#1426 (id 1509)). The file handler keeps LOG_LEVEL.
    stderr_handler.setLevel(STDERR_LEVEL)
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


def _scrub_url_deep(val: Any) -> Any:
    """security.scrub() on every string in a nested structure (URL userinfo by shape)."""
    if isinstance(val, str):
        return scrub(val)
    if isinstance(val, dict):
        return {k: _scrub_url_deep(v) for k, v in val.items()}
    if isinstance(val, list | tuple):
        return [_scrub_url_deep(i) for i in val]
    return val


def write_audit_entry(
    tool: str,
    provider: str,
    repo: str,
    params: dict,
    result: str,
    duration_ms: int,
    reason: str | None = None,
    *,
    correlation: _correlation.Correlation | None = None,
    trace_id: str | None = None,
    effect: dict | None = None,
    effect_incomplete: list[str] | None = None,
) -> None:
    """Append one signed audit record.

    `reason` is the human-readable detail behind a non-ok `result` — usually an
    exception message. It is an audit-record field and deliberately NOT a metric
    label: messages carry repo paths and branch names, which would make metric
    cardinality unbounded. The bounded counterpart is `reason_class`, emitted to the
    metrics by AuditCtx.finish().

    It is scrubbed harder than `result` is. `_scrub_value()` only replaces token
    values githost-mcp has in its own config, so a one-off PAT a human embedded in a
    git remote by hand survives it; security.scrub() additionally strips URL userinfo
    by shape. Exception text is exactly where such a URL surfaces, so both run.

    `run_id`, `task_id`, `session_id` and `trace_id` are on every entry, null when
    absent, so a reader can tell "not forwarded" from "field did not exist yet" by the
    entry's shape. `effect` is present only on a successful write call; see effects.py.
    Every one of these sits inside the HMAC'd entry.
    """
    # Scrub credentials from params and result before writing
    tokens = _config_tokens()
    safe_params = {k: _truncate_deep(_scrub_value(v, tokens)) for k, v in params.items()}
    safe_result = _scrub_value(result, tokens)
    corr = correlation or _correlation.Correlation()

    entry: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "agent_id": _agent_id,
        "tool": tool,
        "provider": provider,
        "repo": _truncate(_scrub_value(repo, tokens)),
        "params": safe_params,
        "result": safe_result,
        "duration_ms": duration_ms,
        "run_id": corr.run_id,
        "task_id": corr.task_id,
        "session_id": corr.session_id,
        "trace_id": trace_id,
    }
    if corr.invalid:
        entry["correlation_invalid"] = list(corr.invalid)
    if reason is not None:
        entry["reason"] = _truncate(scrub(_scrub_value(reason, tokens)))
    if effect is not None:
        # Scrubbed (configured tokens, then URL userinfo by shape) BEFORE it is bounded:
        # cutting first can split a credential so neither pass recognises it.
        entry["effect"] = _truncate_deep(
            bound_effect(_scrub_url_deep(_scrub_value(effect, tokens)))
        )
    if effect_incomplete:
        entry["effect_incomplete"] = list(effect_incomplete)
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

#: The innermost unfinished AuditCtx in this context. Lets the HTTP transport and the
#: PyGithub/python-gitlab wrappers open child spans without every call site passing the
#: ctx down. A sync tool runs on a worker thread with its own copy of the context, so
#: concurrent calls never see each other's ctx.
_current_ctx: contextvars.ContextVar[AuditCtx | None] = contextvars.ContextVar(
    "githost_audit_ctx", default=None
)


def _report_missing_effect(tool: str, params: dict) -> None:
    """A successful write call that recorded no effect. Tests replace this to fail."""
    log.warning("effect_not_recorded", tool=tool)


class AuditCtx:
    """One tool call: its span, its effect, and its audit entry, written by finish().

    The `githost.<tool>` span opens here, as a child of the current span (FastMCP's
    server span), and finish() ends it, so the span covers the work. Use as a context
    manager where a code path could return without calling finish(): `__exit__` finishes
    with `error:unfinished` (or the exception's type) so the span cannot stay open. In
    the HTTP server, correlation.CallScopeMiddleware does the same for every call.
    """

    def __init__(self, tool: str, provider: str, repo: str, params: dict) -> None:
        self.tool = tool
        self.provider = provider
        self.repo = repo
        self.params = params
        self.finished = False
        self._effect: dict | None = None
        self._effect_incomplete: list[str] = []
        self._correlation = _correlation.current_correlation()
        self._t0 = time.perf_counter()
        self._span = start_tool_span(tool)
        self._prev = _current_ctx.get()
        _current_ctx.set(self)
        _correlation.register(self)

    def __enter__(self) -> AuditCtx:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.finished:
            if exc is not None:
                self.finish(f"error:{exc_type.__name__}", exc)
            else:
                self.finish("error:unfinished")
        return False

    def effect(self, **fields: Any) -> None:
        """Record what this call changed. Call before finish("ok").

        Counts, ids, SHAs and refs only (effects.py). A value may be a lambda, evaluated here
        with any exception turned into None. A field passed as None could not be
        determined: it is kept as null and named in `effect_incomplete`. The call still
        succeeds — telemetry never fails a tool call.
        """
        if self._effect is None:
            self._effect = {}
        for key, value in fields.items():
            if isinstance(value, types.FunctionType):
                # A lambda defers a read that could itself fail (a lazy PyGithub attribute):
                # a failure is an unknown field, never a failed call.
                try:
                    value = value()
                except Exception:
                    value = None
            self._effect[key] = value
            if value is None:
                if key not in self._effect_incomplete:
                    self._effect_incomplete.append(key)
            elif key in self._effect_incomplete:
                self._effect_incomplete.remove(key)

    def child(self, name: str, **attrs: Any):
        """A child span of this call's span around one git or HTTP operation.

        Attributes are filtered to observability.CHILD_ATTR_KEYS with plain, short values.
        A no-op when OTel is off.
        """
        if self.finished:
            return nullcontext(None)
        return child_span(self._span, name, attrs)

    def finish(self, result: str = "ok", reason: str | BaseException | None = None) -> None:
        """Write the audit record, end the span, and emit the metrics for this call.

        `reason` takes the exception itself wherever one is in scope, not just its
        message: the type is what classify_reason() maps to a bounded reason_class,
        and a string would throw that away. A plain string is accepted for the call
        sites that reject before any exception exists.

        A second finish() is ignored with a warning: one call, one audit entry.
        """
        if self.finished:
            log.warning("audit_ctx_finished_twice", tool=self.tool, result=result)
            return
        self.finished = True
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        exc = reason if isinstance(reason, BaseException) else None
        reason_text = None if reason is None else str(reason)
        succeeded = result.startswith("ok")

        effect = self._effect if succeeded else None
        incomplete = list(self._effect_incomplete) if effect is not None else []
        if succeeded and effect is None and requires_effect(self.tool, self.params):
            _report_missing_effect(self.tool, self.params)
            incomplete = ["effect_not_recorded"]

        trace_id = trace_id_hex(self._span)  # never raises

        write_audit_entry(
            self.tool,
            self.provider,
            self.repo,
            self.params,
            result,
            duration_ms,
            reason=reason_text,
            correlation=self._correlation,
            trace_id=trace_id,
            effect=effect,
            effect_incomplete=incomplete,
        )

        # Any result that is not a success is a denial for counting purposes —
        # `denied:*` policy refusals and `error:*` failures alike. The question the
        # counter answers is "which githost-mcp limits do agents actually hit", and
        # an error the caller cannot get past is a limit regardless of which prefix
        # it carries. "ok:already_absent" and friends are successes.
        reason_class = None if succeeded else classify_reason(result, exc)
        repo_basename = os.path.basename(self.repo.rstrip("/")) if self.repo else ""

        corr = self._correlation
        end_tool_span(
            self._span,
            {
                "githost.tool": self.tool,
                "githost.provider": self.provider,
                "githost.agent_id": _agent_id,
                "githost.repo": repo_basename,
                "githost.result": result,
                "githost.duration_ms": duration_ms,
                "githost.reason_class": reason_class,
                "forge.run_id": corr.run_id,
                "forge.task_id": corr.task_id,
                "forge.session_id": corr.session_id,
            },
            error=result.startswith("error"),
        )

        try:
            emit_tool_event_sync(
                self.tool,
                self.provider,
                repo_basename,
                result,
                duration_ms,
                reason_class,
            )
        except Exception as exc_emit:  # pragma: no cover - defence in depth
            # Telemetry must never fail a tool call that otherwise succeeded.
            log.warning("tool_event_emit_failed", error=str(exc_emit))

        if _current_ctx.get() is self:
            _current_ctx.set(self._prev)


def current_child(name: str, **attrs: Any):
    """A child span under the current call's AuditCtx, or a no-op if there is none."""
    ctx = _current_ctx.get()
    if ctx is None or ctx.finished:
        return nullcontext(None)
    return ctx.child(name, **attrs)


#: Bound on a rejected argument value recorded by audit_rejection(). An enum value an
#: agent guessed is a word or two; anything longer is not a guess at the enum and only
#: needs enough of a prefix to recognise.
MAX_REJECTED_VALUE_CHARS = 64


def bound_rejected_value(value: Any) -> tuple[str | None, bool]:
    """Return ``(text, truncated)`` for a rejected argument value. ``None`` stays ``None``.

    Scrubs BEFORE bounding. Cutting first can split a credential across the boundary
    and leave a prefix that no scrubbing pass recognises any more — a partial secret
    in a durable signed log (part 1 review round 2, agent-build-workflow-2026-09).
    """
    if value is None:
        return None, False
    text = scrub(str(value))
    if len(text) <= MAX_REJECTED_VALUE_CHARS:
        return text, False
    return text[:MAX_REJECTED_VALUE_CHARS], True


def audit_rejection(
    tool: str,
    provider: str,
    repo: str,
    reason_class: str,
    err: dict | str,
    *,
    argument: str | None = None,
    value: Any = None,
) -> dict:
    """Record a pre-flight validation rejection, and return the caller-facing error.

    Tools validate `repo`/`project`/`tag`/enum arguments *before* they build their
    AuditCtx, and until now those guards simply returned an error dict: no audit
    record, no metric, no trace. An agent hitting one of those limits left nothing
    behind at all — which is the single case this telemetry exists to make visible.

    `tool`, `provider` and `repo` are taken from the same expressions the tool's own
    AuditCtx uses, so a rejected call and a successful call of the same tool produce
    comparably-shaped records.

    When `argument` is given, the record also carries `argument` and `rejected_value`
    (null when absent, else scrubbed then bounded — see bound_rejected_value()). Before
    this the record held `repo` alone, so the largest denial group on forge — a wrong
    `method` — said that agents guessed wrong but never what they guessed.

    Returns the error dict so the guard stays a one-liner at the call site.
    """
    message = err["error"] if isinstance(err, dict) else err
    params: dict[str, Any] = {"repo": repo}
    if argument is not None:
        shown, truncated = bound_rejected_value(value)
        params["argument"] = argument
        params["rejected_value"] = shown
        if truncated:
            params["rejected_value_truncated"] = True
    AuditCtx(tool, provider, repo, params).finish(f"denied:{reason_class}", message)
    return {"error": message}


def reject_enum(
    tool: str,
    provider: str,
    repo: str,
    argument: str,
    value: Any,
    valid: set[str] | frozenset[str],
    hint: str | None = None,
) -> dict:
    """Refuse a value outside an enum, saying what was passed and what would work.

    An empty value is `missing_argument`, anything else `invalid_argument`: "the agent
    did not say" and "the agent said something we do not have" call for different fixes.
    `hint` points at the sibling tool when the request belongs somewhere else.
    """
    options = ", ".join(sorted(valid))
    if value is None or value == "":
        reason_class = "missing_argument"
        message = f"{argument} is required; must be one of: {options}"
    else:
        reason_class = "invalid_argument"
        shown, truncated = bound_rejected_value(value)
        ellipsis = "…" if truncated else ""
        message = f"{argument} {shown!r}{ellipsis} is not valid; must be one of: {options}"
    if hint:
        message = f"{message}. {hint}"
    return audit_rejection(
        tool, provider, repo, reason_class, message, argument=argument, value=value
    )

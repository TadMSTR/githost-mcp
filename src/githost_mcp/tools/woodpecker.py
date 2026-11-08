"""Woodpecker CI tools via httpx (8 tools).

Everywhere in this module, `pipeline_id` means the per-repo pipeline **number** — the
value Woodpecker shows in its UI and resolves in every `/repos/{id}/pipelines/{n}` path.
The global id is returned separately as `internal_id` and is never accepted as input.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from datetime import datetime
from typing import Any

import httpx
import structlog

from .. import logview
from ..audit import AuditCtx, audit_rejection
from ..config import get_config
from ..errors import InvalidArgument
from ..httptrace import async_client
from ..security import scrub, validate_ref

# `\Z`, not `$`: `$` also matches before a trailing newline, so `re.match` accepted
# "owner/repo\n" (KB IV-70). The lookaheads refuse a `.` or `..` segment: the segments go
# into URL paths, and httpx resolves dot segments, so "../.." retargeted the request
# (KB IV-23; githost-scoped-2026-11 p1 pre-audit).
_REPO_RE = re.compile(r"\A(?!\.\.?/)[a-zA-Z0-9_.-]+/(?!\.\.?\Z)[a-zA-Z0-9_.-]+\Z")
_REPO_FMT_ERR = "repo must be in 'owner/repo' format (alphanumeric, hyphens, underscores, dots)"

# Woodpecker 3.x enums (model.StatusValue and the webhook event list, doc-cache
# `woodpecker` rest-api-84). Validated here so a typo is refused with the valid set rather
# than a bare 400 from the server.
_STATUSES = (
    "skipped",
    "pending",
    "running",
    "success",
    "failure",
    "killed",
    "error",
    "blocked",
    "declined",
    "created",
)
_EVENTS = (
    "push",
    "pull_request",
    "pull_request_closed",
    "pull_request_metadata",
    "tag",
    "release",
    "deployment",
    "cron",
    "manual",
)
_TERMINAL = frozenset({"success", "failure", "killed", "error", "skipped", "declined"})
_FAILED = frozenset({"failure", "error", "killed"})
# A step in one of these states has produced no output yet, or never will.
_NO_LOG_STATES = frozenset({"pending", "created", "blocked", "skipped"})
_RFC3339_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})\Z")
# Commit messages and step errors are caller-facing text from the forge: bound them.
_TEXT_MAX = 300

log = structlog.get_logger(__name__)


def _woodpecker_headers() -> dict[str, str]:
    config = get_config()
    if not config.woodpecker_token:
        raise ValueError("WOODPECKER_TOKEN is not set")
    return {
        "Authorization": f"Bearer {config.woodpecker_token}",
        "Content-Type": "application/json",
    }


def _woodpecker_base() -> str:
    config = get_config()
    if not config.woodpecker_url:
        raise ValueError("WOODPECKER_URL is not set")
    return config.woodpecker_url.rstrip("/") + "/api"


def _check_response(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise ValueError("Woodpecker authentication failed (check WOODPECKER_TOKEN)")
    if resp.status_code == 403:
        raise ValueError("Woodpecker authorization denied")
    if resp.status_code >= 400:
        # The response body is the only thing that says *why*. Dropping it is what
        # made #269 expensive: woodpecker_trigger returned "API error 400" for its
        # entire life with no hint that the request form was wrong. Bounded and
        # scrubbed — the body can echo back a credential-bearing URL (SC-14).
        detail = scrub(resp.text)[:300].strip()
        raise ValueError(
            f"Woodpecker API error {resp.status_code}" + (f": {detail}" if detail else "")
        )
    content_type = resp.headers.get("content-type", "")
    # Bodyless responses (204 from cancel) carry no content-type worth checking —
    # only reject a body that arrived and isn't JSON.
    if resp.content and "json" not in content_type.lower():
        # A route that doesn't match any Woodpecker API path still gets a 200 — the
        # SPA fallback serves the frontend index. That HTML then fails json() with an
        # opaque JSONDecodeError that reads like a data problem, not a routing one
        # (vikunja #478). Name the actual cause here instead.
        detail = scrub(resp.text)[:300].strip()
        raise ValueError(
            f"Woodpecker returned non-JSON content-type '{content_type or 'unknown'}' for "
            f"{resp.request.method} {resp.request.url} (status {resp.status_code})"
            + (f": {detail}" if detail else "")
        )


async def _get_json_capped(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> Any:
    """GET a JSON body of at most logview.DOWNLOAD_MAX bytes (KB IV-14, IV-72).

    Log and config bodies carry a whole step's output; the read stops at the cap. A cut JSON
    body can't be parsed, so a body past the cap is refused rather than half-read.
    """
    async with client.stream("GET", url, headers=headers) as resp:
        data, cut = await logview.aread_capped(resp.aiter_bytes())
    _check_response(
        httpx.Response(
            resp.status_code,
            headers=logview.decoded_headers(resp.headers),
            content=data[:4096],
            request=resp.request,
        )
    )
    if cut:
        raise ValueError(
            f"Woodpecker response is larger than {logview.DOWNLOAD_MAX // (1024 * 1024)} MiB; "
            "not read"
        )
    return json.loads(data)


async def _woodpecker_repo_id(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    owner: str,
    name: str,
) -> int:
    """Resolve owner/name to a numeric repo ID via the Woodpecker 3.x lookup endpoint."""
    resp = await client.get(
        f"{base}/repos/lookup/{owner}/{name}",
        headers=headers,
    )
    if resp.status_code == 404:
        raise ValueError(f"Repository '{owner}/{name}' not found in Woodpecker (not registered?)")
    _check_response(resp)
    return int(resp.json()["id"])


def _bounded(text: Any) -> str | None:
    if text is None:
        return None
    shown = scrub(str(text))
    return shown if len(shown) <= _TEXT_MAX else shown[:_TEXT_MAX] + "…"


def _validate_rfc3339(name: str, value: str) -> None:
    ok = bool(_RFC3339_RE.match(value))
    if ok:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            ok = False
    if not ok:
        raise InvalidArgument(f"{name} must be an RFC3339 timestamp, e.g. 2026-11-07T00:00:00Z")


def _validate_pipeline_id(pipeline_id: int) -> None:
    if pipeline_id < 1:
        raise InvalidArgument("pipeline_id must be a pipeline number >= 1")


def _steps(pipeline: dict) -> list[dict]:
    """Flatten workflows[].children[] into step views that keep their workflow name."""
    out = []
    for wf in pipeline.get("workflows") or []:
        for s in wf.get("children") or []:
            out.append(
                {
                    "workflow": wf.get("name"),
                    "name": s.get("name"),
                    "type": s.get("type"),
                    "state": s.get("state"),
                    "exit_code": s.get("exit_code"),
                    "error": _bounded(s.get("error")) if s.get("error") else None,
                    "started": s.get("started") or None,
                    "finished": s.get("finished") or None,
                    "uuid": s.get("uuid"),
                    "_id": s.get("id"),
                }
            )
    return out


def _public_step(step: dict) -> dict:
    return {k: v for k, v in step.items() if not k.startswith("_")}


def _stalled_hint(pipeline: dict, steps: list[dict]) -> dict | None:
    """The first step that has not reached a terminal state, and how long it has been so.

    `since` is the step's start, or for a step that never started, the pipeline's start
    (or creation). A pipeline that is itself terminal has no stalled step.
    """
    if pipeline.get("status") in _TERMINAL:
        return None
    for s in steps:
        if s["state"] not in _TERMINAL:
            since = s["started"] or pipeline.get("started") or pipeline.get("created")
            return {
                "workflow": s["workflow"],
                "step": s["name"],
                "state": s["state"],
                "since": since,
                "seconds_in_state": int(time.time() - since) if since else None,
            }
    return None


def _select_step(
    steps: list[dict], step_name: str | None, workflow: str | None
) -> tuple[dict, str]:
    """Pick the step to read. Returns (step, rule that picked it).

    Never silently takes the first of two same-named steps: a name in more than one
    workflow without `workflow` is an error listing the workflows.
    """
    pool = steps
    if workflow is not None:
        pool = [s for s in steps if s["workflow"] == workflow]
        if not pool:
            names = sorted({str(s["workflow"]) for s in steps})
            raise InvalidArgument(f"Workflow {workflow!r} not found. Workflows: {names}")
    if step_name is not None:
        matches = [s for s in pool if s["name"] == step_name]
        if not matches:
            raise InvalidArgument(f"Step {step_name!r} not found")
        if len(matches) > 1:
            names = sorted({str(s["workflow"]) for s in matches})
            raise InvalidArgument(
                f"Step {step_name!r} exists in more than one workflow: {names}. "
                "Pass `workflow` to choose one."
            )
        return matches[0], "named"
    for s in pool:
        if s["state"] in _FAILED:
            return s, "first_failed"
    for s in pool:
        if s["state"] == "running":
            return s, "first_running"
    return pool[0], "first_step"


def _decode_log(log_data: Any) -> list[str]:
    if not isinstance(log_data, list):
        return logview.split_text(str(log_data))
    lines: list[str] = []
    for entry in log_data:
        if not isinstance(entry, dict):
            lines.append(str(entry))
            continue
        # Woodpecker 3.x carries log text as base64 in "data" — "out" was the 1.x field
        # name. A missing "data" key means the response shape isn't what's expected
        # here; raise rather than dumping the raw dict, which is how defect #2 shipped
        # invisibly under #478.
        if "data" not in entry:
            raise ValueError(
                f"Unexpected Woodpecker log entry shape (no 'data' field): {sorted(entry.keys())}"
            )
        # A present-but-null "data" is a real blank output line — confirmed live
        # 2026-08-25, 149 of 2219 entries on one step. Distinct from a missing key: this
        # is documented shape, not a bug signature.
        if entry["data"] is None:
            lines.append("")
            continue
        decoded = base64.b64decode(entry["data"]).decode("utf-8", "replace")
        lines.append(decoded.rstrip("\n"))
    return lines


def _pipeline_row(p: dict) -> dict:
    return {
        "pipeline_id": p.get("number"),
        "internal_id": p.get("id"),
        "status": p.get("status"),
        "event": p.get("event"),
        "branch": p.get("branch"),
        "ref": p.get("ref"),
        "commit": p.get("commit"),
        "message": _bounded(p.get("message")),
        "created": p.get("created"),
        "started": p.get("started"),
        "finished": p.get("finished"),
    }


def _view_args(
    mode: str | None, offset: int, limit: int, grep: str | None, context: int
) -> dict | None:
    """Validate view parameters before any remote call; an error dict, or None."""
    try:
        logview.validate_view(mode, offset, limit, grep, context)
    except InvalidArgument as e:
        return {"error": str(e)}
    return None


def register(mcp) -> None:
    @mcp.tool
    async def woodpecker_trigger(repo: str, branch: str | None = None) -> dict:
        """Trigger a Woodpecker CI pipeline for a repository.

        Creates a new `manual` pipeline. To re-run a failed pipeline with its original
        event (push, pull_request), use woodpecker_restart instead: a manual run does not
        reproduce an event-specific failure.

        Args:
            repo: Repository in 'owner/repo' format.
            branch: Branch to trigger (default: repo default branch).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_trigger", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        ac = AuditCtx("woodpecker_trigger", "woodpecker", repo, {"repo": repo, "branch": branch})
        try:
            owner, name = repo.split("/", 1)
            # Woodpecker 3.x expects a JSON body here, not query params — a
            # query-param POST returns HTTP 400 with an empty body (vikunja #269,
            # id 280). Omit `branch` entirely when unset so the repo default applies
            # server-side; sending null is not the same thing.
            payload: dict[str, str] = {}
            if branch:
                payload["branch"] = branch
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.post(
                    f"{base}/repos/{repo_id}/pipelines",
                    headers=headers,
                    json=payload,
                )
                _check_response(resp)
                data = resp.json()
            # The per-repo number: the handle every other woodpecker tool takes (p1).
            ac.effect(pipeline_id=data.get("number"), branch=data.get("branch"))
            ac.finish("ok")
            return {
                # Woodpecker resolves the {pipeline} path segment in status/logs/cancel
                # as the per-repo pipeline *number*, not the global id — GET
                # /repos/<id>/pipelines/<global-id> 404s. Return the number as the
                # chainable handle so trigger -> status actually works; the global id
                # is kept separately for reference. (vikunja #269, id 280)
                "pipeline_id": data.get("number") or data.get("id"),
                "internal_id": data.get("id"),
                "status": data.get("status"),
                "branch": data.get("branch"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_list_pipelines(
        repo: str,
        limit: int = 10,
        status: str | None = None,
        branch: str | None = None,
        event: str | None = None,
        ref: str | None = None,
        before: str | None = None,
        after: str | None = None,
    ) -> dict:
        """List pipeline runs for a Woodpecker repository, newest first.

        Filters are applied by the Woodpecker server, so a failure older than the most
        recent page is still found. Each row's `pipeline_id` is the per-repo pipeline
        number that woodpecker_status, woodpecker_get_logs and the other tools take;
        `internal_id` is the global id and is not accepted anywhere.

        Args:
            repo: Repository in 'owner/repo' format.
            limit: Max pipelines to return (default 10, max 100).
            status: skipped | pending | running | success | failure | killed | error |
                blocked | declined | created.
            branch: Only pipelines for this branch.
            event: Webhook event, or several comma-separated: push, pull_request,
                pull_request_closed, pull_request_metadata, tag, release, deployment,
                cron, manual.
            ref: Only pipelines whose ref contains this string (e.g. 'refs/pull/12').
            before: Only pipelines created before this RFC3339 time.
            after: Only pipelines created after this RFC3339 time.
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_list_pipelines", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        limit = max(1, min(limit, 100))
        params: dict[str, Any] = {"page": 1, "perPage": limit}
        try:
            if status is not None:
                if status not in _STATUSES:
                    raise InvalidArgument(f"status must be one of {', '.join(_STATUSES)}")
                params["status"] = status
            if event is not None:
                parts = [p.strip() for p in event.split(",")]
                bad = [p for p in parts if p not in _EVENTS]
                if bad:
                    raise InvalidArgument(
                        f"event values {bad} not recognised; use {', '.join(_EVENTS)}"
                    )
                params["event"] = ",".join(parts)
            # The same rule as every other ref argument (#1431 F-08): a value it refuses
            # cannot name a real branch or ref, so the filter used to return an empty list.
            if branch is not None:
                # SECURITY[accepted]: validate_ref is ASCII-only, so a unicode branch name is
                # refused with an explicit error rather than matched; forge branch names are
                # ASCII. Audit: 2026-11-08/githost-scoped-2026-11-p4-githost-correctness (CR-11).
                validate_ref(branch)
                params["branch"] = branch
            if ref is not None:
                # SECURITY[deferred]: whether Woodpecker's list filter matches `ref` exactly or
                # as a substring is unverified. Ticket vikunja#1449. Target: open.
                # Audit: 2026-11-08/githost-scoped-2026-11-p4-githost-correctness (CR-12).
                validate_ref(ref)
                params["ref"] = ref
            if before is not None:
                _validate_rfc3339("before", before)
                params["before"] = before
            if after is not None:
                _validate_rfc3339("after", after)
                params["after"] = after
        except InvalidArgument as e:
            return audit_rejection(
                "woodpecker_list_pipelines", "woodpecker", repo, "invalid_argument", str(e)
            )
        ac = AuditCtx(
            "woodpecker_list_pipelines",
            "woodpecker",
            repo,
            {"repo": repo, **{k: v for k, v in params.items() if k != "page"}},
        )
        try:
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines",
                    headers=headers,
                    params=params,
                )
                _check_response(resp)
                data = resp.json()
            # perPage is the server's own bound; the slice is a guard against a server
            # that ignores it (the bug this replaced sent `limit`, and got 50 back).
            pipelines = [_pipeline_row(p) for p in (data if isinstance(data, list) else [])]
            ac.finish("ok")
            return {"repo": repo, "pipelines": pipelines[:limit]}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_get_logs(
        repo: str,
        pipeline_id: int,
        step_name: str | None = None,
        workflow: str | None = None,
        mode: str | None = None,
        offset: int = 0,
        limit: int = logview.MAX_LINES,
        grep: str | None = None,
        ignore_case: bool = False,
        context: int = 0,
    ) -> dict:
        """Read a bounded, scrubbed view of one step's output from a Woodpecker pipeline.

        With no `step_name`, picks the first failed step, else the first running step,
        else the first step; `selected_by` says which rule applied. The default `mode` is
        `tail` for a failed step (the failure is at the end) and `head` otherwise.

        At most 500 lines and about 64 KB come back per call. `total_lines`,
        `returned_from` and `returned_to` (0-based, `to` exclusive) let you page with
        `mode='range'`. A step that has not started returns its state and no lines.

        Log text is never written to the audit trail; only these parameters are, with
        `grep` recorded as present/absent. Output is scrubbed of githost-mcp's own
        tokens and URL userinfo only; other secrets a pipeline printed may remain.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger / _status /
                _list_pipelines), not the internal id.
            step_name: Step to read (default: chosen as above).
            workflow: Workflow name, required when `step_name` occurs in more than one.
            mode: tail | head | range.
            offset: First line for `range` (0-based).
            limit: Max lines (and, for `range`, the window size). Capped at 500.
            grep: Literal substring (not a regex), at most 200 characters. Returns the
                matching lines as `segments`.
            ignore_case: Case-insensitive `grep`.
            context: Lines of context either side of each `grep` hit (0-20).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_get_logs", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        if err := _view_args(mode, offset, limit, grep, context):
            return audit_rejection(
                "woodpecker_get_logs", "woodpecker", repo, "invalid_argument", err
            )
        # NOTE: Only metadata is logged here — step output may contain secrets from
        # pipeline environment variables and must not appear in the audit trail. The
        # grep string is recorded only as present/absent: it is caller text that
        # describes what the log contains.
        ac = AuditCtx(
            "woodpecker_get_logs",
            "woodpecker",
            repo,
            {
                "repo": repo,
                "pipeline_id": pipeline_id,
                "step_name": step_name,
                "workflow": workflow,
                "mode": mode,
                "grep": grep is not None,
            },
        )
        try:
            _validate_pipeline_id(pipeline_id)
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=30.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                # /repos/{id}/pipelines/{n}/steps also matches no Woodpecker route
                # (confirmed live 2026-08-25 — 200 with the SPA's HTML index). Steps live
                # nested under the pipeline detail response instead, as
                # workflows[].children[].
                pipeline_resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
                )
                _check_response(pipeline_resp)
                steps = _steps(pipeline_resp.json())
                if not steps:
                    ac.finish("error:no_steps", "no steps found for pipeline")
                    return {"error": "No steps found for pipeline"}

                step, rule = _select_step(steps, step_name, workflow)
                head = {
                    "pipeline_id": pipeline_id,
                    "workflow": step["workflow"],
                    "step": step["name"],
                    "state": step["state"],
                    "exit_code": step["exit_code"],
                    "selected_by": rule,
                }
                if step["state"] in _NO_LOG_STATES:
                    ac.finish("ok")
                    return head | {
                        "lines": None,
                        "note": f"Step has not run (state {step['state']}): no output yet.",
                    }

                # /repos/{id}/pipelines/{n}/{step}/logs matches no Woodpecker route —
                # the correct path is /repos/{id}/logs/{n}/{step} (vikunja #478).
                log_data = await _get_json_capped(
                    client,
                    f"{base}/repos/{repo_id}/logs/{pipeline_id}/{int(step['_id'])}",
                    headers,
                )
                lines = _decode_log(log_data)

            if mode is None:
                mode = "tail" if step["state"] in _FAILED else "head"
            result = logview.view(
                lines,
                mode=mode,
                offset=offset,
                limit=limit,
                grep=grep,
                ignore_case=ignore_case,
                context=context,
            )
            ac.finish("ok")
            return head | result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_pipeline_cancel(repo: str, pipeline_id: int) -> dict:
        """Cancel a running Woodpecker pipeline.

        DESTRUCTIVE: Terminates a running CI pipeline. HITL gated in scoped-mcp
        manifests — sysadmin only.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_pipeline_cancel", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        ac = AuditCtx(
            "woodpecker_pipeline_cancel",
            "woodpecker",
            repo,
            {"repo": repo, "pipeline_id": pipeline_id},
        )
        try:
            _validate_pipeline_id(pipeline_id)
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.delete(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
                )
                if resp.status_code == 409:
                    ac.finish(
                        "error:already_finished",
                        "pipeline is already finished and cannot be cancelled",
                    )
                    return {"error": "Pipeline is already finished and cannot be cancelled"}
                _check_response(resp)
            ac.effect(pipeline_id=pipeline_id, action="cancel")
            ac.finish("ok")
            return {"cancelled": True, "id": pipeline_id}
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_status(repo: str, pipeline_id: int) -> dict:
        """Get the status of a Woodpecker pipeline run, with per-step state.

        `pipeline_id` in the result is the per-repo number every other woodpecker tool
        takes; `internal_id` is the global id, for reference only. `steps[]` gives each
        step's state and exit code; `stalled_hint` names the first step that has not
        finished and how long it has been in that state; `errors[]` carries config and
        linter errors (warnings included, flagged `is_warning`).

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number (as returned by woodpecker_trigger).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_status", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        ac = AuditCtx(
            "woodpecker_status", "woodpecker", repo, {"repo": repo, "pipeline_id": pipeline_id}
        )
        try:
            _validate_pipeline_id(pipeline_id)
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.get(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
                )
                _check_response(resp)
                data = resp.json()
            steps = _steps(data)
            cancel = data.get("cancel_info")
            ac.finish("ok")
            return {
                # The number, not data["id"]: status used to return the global id here,
                # and agents fed it back into get_logs/cancel and got 404s or a different
                # pipeline (vikunja#1425).
                "pipeline_id": data.get("number"),
                "internal_id": data.get("id"),
                "status": data.get("status"),
                "event": data.get("event"),
                "branch": data.get("branch"),
                "ref": data.get("ref"),
                "commit": data.get("commit"),
                "message": _bounded(data.get("message")),
                "started_at": data.get("started"),
                "finished_at": data.get("finished"),
                "errors": [
                    {
                        "type": e.get("type"),
                        "message": _bounded(e.get("message")),
                        "is_warning": e.get("is_warning"),
                    }
                    for e in (data.get("errors") or [])
                    if isinstance(e, dict)
                ],
                "cancel_info": (
                    {
                        k: cancel.get(k)
                        for k in ("canceled_by_step", "canceled_by_user", "superseded_by")
                    }
                    if isinstance(cancel, dict)
                    else None
                ),
                "steps": [_public_step(s) for s in steps],
                "stalled_hint": _stalled_hint(data, steps),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_restart(repo: str, pipeline_id: int) -> dict:
        """Restart a Woodpecker pipeline, keeping its original event.

        Unlike woodpecker_trigger, which creates a `manual` run, a restart re-runs the
        pipeline's original event at that pipeline's commit: push, pull_request, and also
        tag, release, deployment and cron. Restarting an old pipeline runs that old
        commit, and restarting a tag, release or deployment pipeline re-runs whatever
        those steps do (a publish, a deploy). No event or deploy-target override is
        exposed: an override could turn a push into a deployment run.

        Creates a new pipeline. Returns its number as `pipeline_id`.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Number of the pipeline to restart.
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_restart", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        ac = AuditCtx(
            "woodpecker_restart", "woodpecker", repo, {"repo": repo, "pipeline_id": pipeline_id}
        )
        try:
            _validate_pipeline_id(pipeline_id)
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                # No params, no body: the endpoint's `event` and `deploy_to` overrides
                # are deliberately never sent.
                resp = await client.post(
                    f"{base}/repos/{repo_id}/pipelines/{pipeline_id}",
                    headers=headers,
                )
                _check_response(resp)
                data = resp.json()
            ac.effect(pipeline_id=data.get("number"), restarted_from=pipeline_id)
            ac.finish("ok")
            return {
                "pipeline_id": data.get("number"),
                "internal_id": data.get("id"),
                "restarted_from": pipeline_id,
                "status": data.get("status"),
                "event": data.get("event"),
                "branch": data.get("branch"),
                "commit": data.get("commit"),
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    async def woodpecker_pipeline_config(
        repo: str,
        pipeline_id: int,
        file: str | None = None,
        mode: str = "head",
        offset: int = 0,
        limit: int = logview.MAX_LINES,
        grep: str | None = None,
        ignore_case: bool = False,
        context: int = 0,
    ) -> dict:
        """Read the compiled config files a Woodpecker pipeline actually ran with.

        Returns the list of config file names and a bounded, scrubbed view of one of them
        (the same reader and caps as woodpecker_get_logs). Config carries `from_secret`
        names, never secret values. Output is scrubbed of githost-mcp's own tokens and
        URL userinfo only; other secrets a pipeline printed may remain.

        Args:
            repo: Repository in 'owner/repo' format.
            pipeline_id: Pipeline number.
            file: Config file name (default: the first). See `files` in the result.
            mode: head | tail | range (default head).
            offset: First line for `range` (0-based).
            limit: Max lines. Capped at 500.
            grep: Literal substring (not a regex), at most 200 characters.
            ignore_case: Case-insensitive `grep`.
            context: Lines of context either side of each `grep` hit (0-20).
        """
        if not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_pipeline_config", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        if err := _view_args(mode, offset, limit, grep, context):
            return audit_rejection(
                "woodpecker_pipeline_config", "woodpecker", repo, "invalid_argument", err
            )
        ac = AuditCtx(
            "woodpecker_pipeline_config",
            "woodpecker",
            repo,
            {
                "repo": repo,
                "pipeline_id": pipeline_id,
                "file": file,
                "mode": mode,
                "grep": grep is not None,
            },
        )
        try:
            _validate_pipeline_id(pipeline_id)
            owner, name = repo.split("/", 1)
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                data = await _get_json_capped(
                    client, f"{base}/repos/{repo_id}/pipelines/{pipeline_id}/config", headers
                )
            configs = [c for c in (data if isinstance(data, list) else []) if isinstance(c, dict)]
            if not configs:
                ac.finish("error:no_config", "no config files for pipeline")
                return {"error": "No config files found for pipeline"}
            names = [str(c.get("name")) for c in configs]
            if file is None:
                chosen = configs[0]
            else:
                chosen = next((c for c in configs if c.get("name") == file), None)
                if chosen is None:
                    raise InvalidArgument(f"Config file {file!r} not found. Files: {names}")
            # Go encodes []byte as base64 in JSON; the doc-cache sample shows an int array
            # for the same field, so accept both.
            raw = chosen.get("data") or ""
            try:
                if isinstance(raw, list):
                    text = bytes(raw).decode("utf-8", "replace")
                else:
                    text = base64.b64decode(raw).decode("utf-8", "replace")
            except (binascii.Error, ValueError, TypeError):
                raise ValueError("Config file data is not in a recognised encoding") from None
            result = logview.view(
                logview.split_text(text),
                mode=mode,
                offset=offset,
                limit=limit,
                grep=grep,
                ignore_case=ignore_case,
                context=context,
            )
            ac.finish("ok")
            return {
                "pipeline_id": pipeline_id,
                "files": names,
                "file": chosen.get("name"),
            } | result
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

    @mcp.tool
    # SECURITY[deferred]: works only because the Woodpecker token's user is a human admin;
    # agent restarts are attributed to that user. Dedicated bot account tracked in
    # vikunja#1428. Audit: 2026-11-08/githost-scoped-2026-11-p1-ci-diagnostics (F-02).
    async def woodpecker_queue_info(repo: str | None = None) -> dict:
        """Show what Woodpecker's queue is running and waiting on.

        Use when a pipeline sits in `pending`: it answers whether the agents are busy and
        with what. Items carry repo id, pipeline number, workflow name, agent name and
        age only — never task labels or environment. Needs an admin Woodpecker token.

        Args:
            repo: Optional 'owner/repo' to show only that repository's items.
        """
        if repo is not None and not _REPO_RE.match(repo):
            return audit_rejection(
                "woodpecker_queue_info", "woodpecker", repo, "bad_repo_format", _REPO_FMT_ERR
            )
        ac = AuditCtx("woodpecker_queue_info", "woodpecker", repo or "", {"repo": repo})
        try:
            base = _woodpecker_base()
            headers = _woodpecker_headers()
            async with async_client("woodpecker", timeout=15.0) as client:
                repo_id = None
                if repo is not None:
                    owner, name = repo.split("/", 1)
                    repo_id = await _woodpecker_repo_id(client, base, headers, owner, name)
                resp = await client.get(f"{base}/queue/info", headers=headers)
                _check_response(resp)
                data = resp.json()
            now = time.time()

            def items(key: str) -> list[dict]:
                out = []
                for t in data.get(key) or []:
                    if not isinstance(t, dict):
                        continue
                    if repo_id is not None and t.get("repo_id") != repo_id:
                        continue
                    created = t.get("created") or None
                    out.append(
                        {
                            "repo_id": t.get("repo_id"),
                            "pipeline_id": t.get("pipeline_number"),
                            "name": t.get("name"),
                            "agent_name": t.get("agent_name") or None,
                            "age_seconds": int(now - created) if created else None,
                        }
                    )
                return out

            stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            ac.finish("ok")
            return {
                "paused": data.get("paused"),
                "running": items("running"),
                "pending": items("pending"),
                "waiting_on_deps": items("waiting_on_deps"),
                "stats": {
                    k: stats.get(k)
                    for k in (
                        "worker_count",
                        "running_count",
                        "pending_count",
                        "waiting_on_deps_count",
                    )
                },
            }
        except Exception as e:
            ac.finish(f"error:{type(e).__name__}", e)
            return {"error": scrub(str(e))}

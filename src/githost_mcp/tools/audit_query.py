"""Query the local JSONL audit log."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import structlog

from ..audit import audit_backup_paths, verify_entry_hmac
from ..config import get_config

log = structlog.get_logger(__name__)

# Read the file back-to-front in blocks. The previous f.readlines() pulled the
# whole log into memory on every call — on a file that never rotated and was
# already approaching a megabyte per agent.
_REVERSE_READ_BLOCK = 64 * 1024


def _reverse_lines(path: str):
    """Yield the file's lines newest-first without loading it all into memory."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        remaining = f.tell()
        tail = b""
        while remaining > 0:
            read_size = min(_REVERSE_READ_BLOCK, remaining)
            remaining -= read_size
            f.seek(remaining)
            block = f.read(read_size) + tail
            lines = block.split(b"\n")
            # The first element may be a partial line continuing into the previous
            # block, so hold it back until that block is read.
            tail = lines.pop(0)
            for line in reversed(lines):
                yield line.decode("utf-8", errors="replace")
        if tail:
            yield tail.decode("utf-8", errors="replace")


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO date/datetime string, assuming UTC when no offset is given.

    Naive datetimes (e.g. '2026-05-20', with no 'Z' or offset) can't be compared
    to timezone-aware ones — this normalizes both sides of the 'since' filter to
    always be timezone-aware so the comparison never raises.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def register(mcp) -> None:
    @mcp.tool
    def audit_log_query(
        agent_id: str | None = None,
        tool: str | None = None,
        repo: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Query the structured JSONL audit log.

        Each returned entry includes a tamper_detected field (True if HMAC verification fails).

        Args:
            agent_id: Filter by agent ID (exact match).
            tool: Filter by tool name (exact match).
            repo: Filter by repo path (substring match).
            since: ISO date string (e.g. '2026-05-20') — return entries on or after this date.
            limit: Max entries to return (default 50, newest first).
        """
        config = get_config()
        audit_path = config.audit_log_file

        if not os.path.exists(audit_path):
            return {"entries": [], "total_matched": 0, "sources_searched": []}

        since_dt: datetime | None = None
        if since:
            try:
                since_dt = _parse_iso_utc(since)
            except ValueError:
                return {
                    "error": (
                        f"Invalid 'since' date format: '{since}'. Use ISO format like '2026-05-20'."
                    )
                }

        # Search the live file first, then rotated backups newest-first. Without
        # this, rotation would silently shrink the queryable window and a 'since'
        # query spanning a rotation would come back short with no indication —
        # the same class of defect rotation is being added to fix.
        sources = [audit_path]
        sources += [p for p in audit_backup_paths(audit_path, config.audit_log_backup_count)]

        entries: list[dict] = []
        searched: list[str] = []
        for source in sources:
            if len(entries) >= limit:
                break
            if not os.path.exists(source):
                continue
            searched.append(os.path.basename(source))
            try:
                for line in _reverse_lines(source):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if agent_id and entry.get("agent_id") != agent_id:
                        continue
                    if tool and entry.get("tool") != tool:
                        continue
                    if repo and repo not in entry.get("repo", ""):
                        continue
                    if since_dt:
                        try:
                            entry_dt = _parse_iso_utc(entry["ts"])
                            if entry_dt < since_dt:
                                continue
                        except (KeyError, ValueError):
                            pass

                    entry["tamper_detected"] = not verify_entry_hmac(entry)
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
            except OSError as e:
                # A backup we cannot read must not hide the results we did get.
                if source == audit_path:
                    return {"error": f"Cannot read audit log: {e}"}
                log.warning("audit_backup_unreadable", path=source, error=str(e))

        return {
            "entries": entries,
            "total_matched": len(entries),
            "sources_searched": searched,
        }

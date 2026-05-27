"""Query the local JSONL audit log."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

import structlog

from ..audit import verify_entry_hmac
from ..config import get_config

log = structlog.get_logger(__name__)


def register(mcp) -> None:
    @mcp.tool
    def audit_log_query(
        agent_id: Optional[str] = None,
        tool: Optional[str] = None,
        repo: Optional[str] = None,
        since: Optional[str] = None,
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
            return {"entries": [], "total_matched": 0}

        since_dt: Optional[datetime] = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return {"error": f"Invalid 'since' date format: '{since}'. Use ISO format like '2026-05-20'."}

        entries = []
        try:
            with open(audit_path) as f:
                lines = f.readlines()
        except OSError as e:
            return {"error": f"Cannot read audit log: {e}"}

        for line in reversed(lines):
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
                    entry_dt = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                    if entry_dt < since_dt:
                        continue
                except (KeyError, ValueError):
                    pass

            entry["tamper_detected"] = not verify_entry_hmac(entry)
            entries.append(entry)
            if len(entries) >= limit:
                break

        return {"entries": entries, "total_matched": len(entries)}

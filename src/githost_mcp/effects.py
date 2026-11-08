"""Which githost tools change something, and how their `effect` record is bounded.

Every registered tool is in exactly one of EFFECT_TOOLS or READ_TOOLS. The closed-world
test (tests/test_effects.py) enumerates the server's tools and fails on one that is in
neither, so a new verb cannot ship unclassified. githost-scoped-2026-11 p3 adds and
regates verbs: each goes in one of these sets in the same change.

An effect tool records, on success, what it changed through AuditCtx.effect(): ids,
SHAs, refs and counts. A tool whose methods or actions are partly reads names the
argument and the values that write in EFFECT_SELECTORS; a call with any other value is
a read and records no effect.

`effect` is a plain dict and stays one: p4 (vikunja#1300) extends it with the commit
parents for git_commit and merges, rather than adding a field beside it.
"""

from __future__ import annotations

from typing import Any

READ_TOOLS = frozenset(
    {
        "audit_log_query",
        "git_diff",
        "git_log",
        "git_show",
        "git_status",
        "gitea_commit_status",
        "gitea_get_release",
        "gitea_issue_read",
        "gitea_list_releases",
        "gitea_pr_get",
        "gitea_pr_list",
        "gitea_repo_get",
        "github_commit_checks",
        "github_get_release",
        "github_issue_read",
        "github_list_releases",
        "github_pr_comments",
        "github_pr_get",
        "github_pr_list",
        "github_repo_get",
        "github_workflow_list",
        "github_workflow_status",
        "gitlab_get_release",
        "gitlab_issue_read",
        "gitlab_list_releases",
        "gitlab_mr_get",
        "gitlab_mr_list",
        "woodpecker_get_logs",
        "woodpecker_list_pipelines",
        "woodpecker_pipeline_config",
        "woodpecker_queue_info",
        "woodpecker_status",
    }
)

EFFECT_TOOLS = frozenset(
    {
        # local git
        "git_add",
        "git_branch",
        "git_branch_delete_remote",
        "git_checkout",
        "git_clone",
        "git_commit",
        "git_pull",
        "git_push",
        "git_push_force_with_lease",
        "git_remote",
        "git_tag",
        "git_worktree",
        # Gitea
        "gitea_actions",
        "gitea_create_release",
        "gitea_issue_write",
        "gitea_pr_comment",
        "gitea_pr_create",
        "gitea_pr_merge",
        "gitea_pr_review",
        "gitea_release_delete",
        "gitea_release_update",
        "gitea_repo_create",
        # GitHub
        "github_actions",
        "github_create_release",
        "github_fork",
        "github_issue_write",
        "github_pr_create",
        "github_pr_merge",
        "github_pr_review",
        "github_pr_update",
        "github_release_delete",
        "github_release_update",
        "github_repo_create",
        # GitLab
        "gitlab_create_release",
        "gitlab_issue_write",
        "gitlab_mr_create",
        "gitlab_mr_merge",
        "gitlab_mr_review",
        "gitlab_pipeline",
        "gitlab_release_delete",
        "gitlab_release_update",
        # release and registries
        "npm_publish",
        "pypi_publish",
        "release",
        # Woodpecker
        "woodpecker_pipeline_cancel",
        "woodpecker_restart",
        "woodpecker_trigger",
    }
)

#: Tools that are an effect only for some values of one argument, keyed by the name the
#: argument has in the tool's audit `params`.
EFFECT_SELECTORS: dict[str, tuple[str, frozenset[Any]]] = {
    "git_branch": ("action", frozenset({"create", "delete"})),
    "git_remote": ("action", frozenset({"add", "remove"})),
    "git_worktree": ("method", frozenset({"add", "remove"})),
    "gitea_actions": ("method", frozenset({"dispatch_workflow", "rerun_run", "rerun_failed_jobs"})),
    "github_actions": (
        "method",
        frozenset({"run_workflow", "rerun_workflow", "rerun_failed_jobs", "cancel_run"}),
    ),
    "gitlab_pipeline": ("method", frozenset({"create", "retry", "cancel"})),
    "gitea_pr_review": ("method", frozenset({"submit_review", "dismiss_review"})),
    "github_pr_review": ("method", frozenset({"submit_review", "dismiss_review"})),
    "gitlab_mr_review": ("method", frozenset({"approve", "unapprove"})),
    "release": ("dry_run", frozenset({False})),
}


def requires_effect(tool: str, params: dict[str, Any]) -> bool:
    """True when a successful call of `tool` with these audit params must record an effect."""
    if tool not in EFFECT_TOOLS:
        return False
    selector = EFFECT_SELECTORS.get(tool)
    if selector is None:
        return True
    argument, values = selector
    return params.get(argument) in values


# ---------------------------------------------------------------------------
# Bounding
#
# Counts and ids only. No commit message, PR or issue body, file name, URL or log text
# ever belongs in an effect: the audit trail is durable and signed, and part 6 ships it
# to Loki. The values that do go in are scrubbed and then bounded by AuditCtx (scrub
# first: cutting first can split a credential so the scrubber no longer sees it).
# ---------------------------------------------------------------------------

#: Strings in an effect are ids, refs, SHAs and short enums. A ref can legitimately be
#: long-ish; 256 is well past any real one and short of anything that is a body.
MAX_EFFECT_STRING_CHARS = 256
#: A list (push flags, per-target release results) holds at most this many items.
MAX_EFFECT_LIST_ITEMS = 16
#: Fields per effect record.
MAX_EFFECT_FIELDS = 16


def _bound_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    if len(text) > MAX_EFFECT_STRING_CHARS:
        return text[:MAX_EFFECT_STRING_CHARS] + "…"
    return text


def _bound_list(value: list | tuple) -> list:
    return [
        "<nested>" if isinstance(v, dict | list | tuple) else _bound_scalar(v)
        for v in list(value)[:MAX_EFFECT_LIST_ITEMS]
    ]


def bound_effect_value(value: Any, nested: bool = False) -> Any:
    """A scalar, a short list of scalars, or (top level only) a dict of those."""
    if isinstance(value, dict):
        if nested:
            return "<nested>"
        items = list(value.items())[:MAX_EFFECT_FIELDS]
        return {str(k)[:64]: bound_effect_value(v, nested=True) for k, v in items}
    if isinstance(value, list | tuple):
        return _bound_list(value)
    return _bound_scalar(value)


def bound_effect(fields: dict[str, Any]) -> dict[str, Any]:
    """Bound a whole effect record: at most MAX_EFFECT_FIELDS fields, each bounded."""
    items = list(fields.items())[:MAX_EFFECT_FIELDS]
    return {str(k)[:64]: bound_effect_value(v) for k, v in items}

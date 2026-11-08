"""Which githost tools change something, and how their `effect` record is bounded.

Every registered tool is in exactly one of EFFECT_TOOLS or READ_TOOLS. The closed-world
test (tests/test_effects.py) enumerates the server's tools and fails on one that is in
neither, so a new verb cannot ship unclassified. HITL_TOOLS is a subset of EFFECT_TOOLS:
the verbs a manifest may grant only behind a HITL approval (githost-scoped-2026-11 p3).

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

#: The effect tools an agent may call only through a HITL approval. host-forge/scripts
#: `grant-lint.py --tools` reads this class from `githost-mcp --list-tools --json`. It
#: refuses a manifest that grants one of these tools unless that tool is in
#: `hitl.approval_required` under `hitl.mode: enforce`. `githost-grant-gate.sh` runs the
#: same check before githost-mcp restarts on a new wheel (githost-scoped-2026-11 p3).
#:
#: A new verb goes in READ_TOOLS or EFFECT_TOOLS. If it goes in EFFECT_TOOLS, its author
#: also decides whether it belongs here, and says why in the group comment.
#:
#: Outside the class, and why (each is an effect tool; UNGATED_EFFECT_TOOLS below pins the
#: set so moving a verb out of the class takes a deliberate edit):
#:
#: - `git_push` pushes one existing local branch to the same-named remote branch, with the
#:   namespace pinned (`refs/heads/<n>:refs/heads/<n>`). It cannot push a tag, delete a ref
#:   or force an update, so it reaches none of the gated effects below.
#: - `git_tag(push=True)`, `*_create_release`, `*_release_update`, and the
#:   `github_actions` `run_workflow`/`rerun_*` methods: NOT reversible in every repo. Where
#:   a pushed `v*` tag, a published release or a dispatched workflow runs a registry
#:   publish, one ungated call publishes a version that can never be reused. There is no
#:   githost-side control on these today: `git_tag` and the release tools are held by the
#:   build agents only, but `github_actions` is also held by agents that read untrusted
#:   content, and nothing here limits which workflow it dispatches. Gate them, or require
#:   a reviewer environment on every publish job: an open decision (vikunja#1440).
#: - `github_actions` and `gitlab_pipeline` dispatch on `method`, and the class is per
#:   tool. Their `cancel_run`/`cancel` methods stop a pipeline exactly as
#:   `woodpecker_pipeline_cancel` does, ungated. Same decision (vikunja#1440).
#: - `woodpecker_restart`, `woodpecker_trigger`: start or re-run whatever pipeline the
#:   repo defines, ungated. Same decision (vikunja#1440).
HITL_TOOLS = frozenset(
    {
        # Merges land on a default branch, which for some repos is what deploys.
        "gitea_pr_merge",
        "github_pr_merge",
        "gitlab_mr_merge",
        # These destroy state that nothing in githost restores: a remote branch, a remote
        # ref rewritten by a force push, a published release.
        "git_branch_delete_remote",
        "git_push_force_with_lease",
        "gitea_release_delete",
        "github_release_delete",
        "gitlab_release_delete",
        # Stops a running pipeline, which may be another agent's deploy.
        "woodpecker_pipeline_cancel",
        # These publish to the world. A registry version can never be reused once
        # uploaded, and `release` tags, publishes and rolls back across several targets.
        "release",
        "npm_publish",
        "pypi_publish",
        # These create a repository under a forge-owned owner, which is a new public surface.
        "github_repo_create",
        "gitea_repo_create",
    }
)

#: The effect tools deliberately left outside HITL_TOOLS (see the comment above it).
#: Pinned by tests/test_effects.py, so a verb leaving the class, or a new effect verb
#: landing outside it, is a reviewed edit to this set rather than a silent default.
UNGATED_EFFECT_TOOLS = EFFECT_TOOLS - HITL_TOOLS

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

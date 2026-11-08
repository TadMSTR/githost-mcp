[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

# githost-mcp

Unified local + multi-provider git MCP server with a per-agent audit trail as a first-class feature.

Every tool call is tagged with the caller agent (`AGENT_ID`), written to a structured JSONL audit log, and write operations emit agent-bus events. Local git operations run entirely through gitpython — no subprocess, no injection risk.

## Architecture

Every tool call dispatches to one provider and writes a signed audit entry before returning; write operations additionally emit an agent-bus event.

```mermaid
flowchart LR
    client[MCP client / agent] -->|tool call + AGENT_ID| tool[githost-mcp tool]
    tool --> dispatch{provider}
    dispatch -->|local| localgit[gitpython]
    dispatch -->|GitHub| gh[PyGithub]
    dispatch -->|Gitea| gitea[httpx]
    dispatch -->|GitLab| glab[python-gitlab]
    dispatch -->|Woodpecker| wp[httpx]
    localgit --> audit[audit.py]
    gh --> audit
    gitea --> audit
    glab --> audit
    wp --> audit
    audit -->|HMAC-signed JSONL| logfile[(audit log)]
    tool -.->|write ops| bus[[agent-bus event]]
```

Path-taking tools resolve separate read and write allowlists before touching the
filesystem: `ALLOWED_REPO_ROOTS` env (break-glass, applies to both lists) →
`/etc/forge/workspace-policy.yml` → the agent manifest → empty, fail closed. First
match wins — the policy file, once it loads successfully, is authoritative for that
agent even if its own grant is empty. A write op additionally passes through a
glob gate when the resolved grant carries `write_globs`/`write_globs_deny`.

```mermaid
flowchart TD
    call[path-taking tool call] --> env{ALLOWED_REPO_ROOTS set?}
    env -->|yes| envroots[env roots — same list for read + write]
    env -->|no| policy{workspace-policy.yml present + parses?}
    policy -->|yes| proots[policy read/write roots + write_globs]
    policy -->|no| manifest{manifest roots available?}
    manifest -->|yes| mroots[manifest-declared roots]
    manifest -->|no| deny[deny — fail closed]
    envroots --> validate{path under the resolved read/write root?}
    proots --> validate
    mroots --> validate
    validate -->|no| deny
    validate -->|yes, read op| allow[proceed]
    validate -->|yes, write op| glob{write_globs/write_globs_deny set?}
    glob -->|no| allow
    glob -->|yes, matches allow and not deny| allow
    glob -->|yes, denied by glob| deny
```

## Why githost-mcp?

| Tool | What it covers | Gap |
|------|----------------|-----|
| `cyanheads/git-mcp-server` (TS) | 28 local git tools | No remote providers, no agent attribution, no audit trail |
| `poly-git-mcp` | GitHub + GitLab + Gitea | Wraps CLI tools — fragile, no audit, no agent ID |
| Official GitHub MCP | GitHub only | No local git, no Gitea/GitLab |
| Official Gitea MCP | Gitea only | No local git, no GitHub/GitLab |
| `j04n-f/woodpecker-mcp` | Woodpecker (6 read-only tools) | No trigger/cancel — githost already exceeds it |

**githost-mcp fills the gap:** local git + multi-provider remote via native APIs + per-agent structured audit trail. As of the Tier-1 parity release it matches the single-provider servers on PR/MR review + diff, CI trigger/rerun/cancel, full release CRUD, and issues — across all three providers behind one audited server.

## Tools (79 total)

Higher-verb-count capabilities (PR/MR review, CI control, issues) are exposed as
**method-dispatch** tools — one tool takes a `method` argument and routes internally —
mirroring how the official GitHub/Gitea servers structure theirs. This adds ~40
operations without the tool count ballooning past what every agent pays for in context.
Each `method` still writes its own per-operation audit entry.

### Local Git (16)
`git_status`, `git_diff`, `git_log`, `git_show`, `git_branch`, `git_branch_delete_remote`, `git_checkout`, `git_add`, `git_commit`, `git_push`, `git_push_force_with_lease`, `git_pull`, `git_tag`, `git_remote` *(list/add/remove)*, `git_worktree` *(add/list/remove)*, `git_clone`

`git_clone(provider, owner, repo, depth=None, branch=None)` (as of 0.16.0) clones **only
repos whose owner you control**:

- **GitHub**: owners listed in `FORGE_OWNED_OWNERS` (default `TadMSTR`). Any other owner
  is refused before any network or filesystem access.
- **Gitea**: **organizations** on the configured `GITEA_URL`, confirmed with one
  `GET /orgs/{owner}` call. A user namespace is refused.

Both refusals are audited as `denied:not_owned`. Forks are deliberately allowed: to work
on an upstream, `github_fork` it and clone the fork. Cloning the upstream itself stays an
operator step.

The tool takes **no URL and no destination**. It builds the scp-form SSH URL from the
arguments (still passed through the same remote-URL validation as `git_remote add`) and
computes the path:

| Provider | Destination |
|---|---|
| `github` | `~/repos/personal/<repo>` |
| `gitea` | `~/repos/gitea/<owner>-<repo>`, or `~/repos/gitea/<repo>` if `<repo>` already starts with `<owner>-` |

`~` is the broker's own home. The path is validated against the write roots. An existing
path is refused, never merged into, and a symlink at the destination is refused, never
followed. Submodules are not cloned, since `.gitmodules` URLs are content the cloned repo
controls. The clone is killed after 120 s, and a failed clone removes only the directory
the call created.

Glob-scoped grants (see [Write glob scoping](#write-glob-scoping)) are handled strictly:

- An agent whose grant carries `write_globs` is refused outright, because a clone writes a
  whole tree.
- An agent whose grant carries `write_globs_deny` (e.g. `manifests/**`) cannot pass
  `branch` at all, not even the default branch's name; omit it to clone the default
  branch. A clone whose tree contains a denied path is removed and refused as
  `denied:write_glob`, the same result `git_add` and `git_commit` write.
- A tracked symlink is refused if it sits above any denied area, at any depth
  (`config -> evil/` under a `config/secrets/*` deny). A file symlink such as
  `AGENTS.md -> CLAUDE.md` is not affected.

So an agent denied `manifests/**` cannot clone a repo that contains manifests.

`git_worktree(repo_path, method, name=None, branch=None, base=None)` (as of 0.15.0) is
the one sanctioned way to get a scratch checkout. The destination is **computed, never
caller-supplied**: `<worktree_root>/<repo>-<name>`, where `worktree_root` is
`GITHOST_WORKTREE_ROOT` (default `~/repos/personal/_worktrees`, `~` expanded at startup).
`add` returns the path; run every later call against it. `list` marks each worktree with
`under_worktree_root`. The root is a location, not a grant — each destination is still
checked against the agent's write roots.
`remove` never forces: a worktree with uncommitted or untracked files is refused with
git's own message. Before this verb, agents guessed `/tmp/…`, `~/scratch/…` and
`~/worktrees/…`, and githost-mcp refused every one.

`git_push_force_with_lease(repo_path, branch, expected_sha, remote="origin")` (as of
0.15.0) updates a rebased branch. It always sends
`--force-with-lease=refs/heads/<branch>:<expected_sha>`, with `expected_sha` required as a
full 40-hex sha — without an explicit sha the lease compares against the remote-tracking
ref, which a `git fetch` silently moves, and protects nothing. `main`, `master` and the
remote's default branch are refused before any push (and the default-branch check fails
closed when the default cannot be determined). It is a separate tool, not a flag on
`git_push`, for the same reason `git_branch_delete_remote` is: HITL gates on tool names.

Paths: `repo_path` may start with `~` (as of 0.15.0). Validation expands and resolves it,
and the git operation runs on that same resolved path — never on the literal string.

`git_branch` takes `force` (as of 0.13.0), which applies to `delete` only. Without it,
deletion uses `git branch -d` semantics, which require the branch be an ancestor of HEAD.
**A squash-merged branch never is** — squash rewrites the work into one new commit and
leaves the original tip unreachable — so an unforced delete fails for essentially every
branch merged the way this fleet merges. The default stays `False`: the caller knows
whether the work landed and the tool will not guess. The result carries `deleted_sha`,
captured before the delete, because after it the tip is only reachable from the reflog.

`git_branch_delete_remote` deletes a branch on the remote by pushing a delete refspec. It
is a separate tool rather than a `remote=` flag on `git_branch`, deliberately: every
scoped-mcp enforcement layer keys on the tool **name** and never on arguments — HITL
matches `tool_name` by fnmatch, the proxy allow/denylist is exact-string set membership,
and `argument_filters` is a single regex over a field list with no conjunction. As a
parameter it would be ungateable, since restricting it would mean denying `git_branch`
outright and losing `list` and `create` with it.

Two behaviours worth knowing before you call it:

- **A ref that is already absent is not reported as a deletion.** Git exits 0 and prints
  `[deleted]` for a branch that never existed, which would make a typo'd branch name
  indistinguishable from a successful cleanup while the real branch survives. The tool
  queries the remote with `ls-remote` first and returns `already_absent` instead —
  carrying neither `deleted` nor `error`, because nothing was destroyed and nothing went
  wrong.
- **A rejected delete surfaces as an error result with no `deleted` key**, the same
  contract `git_push` uses. Note that GitPython cannot report this one through
  `PushInfo` flags: git's porcelain line for a denied deletion carries control character
  `!`, so `PushInfo._from_line` does not take its `flags & DELETED` branch and instead
  calls `Reference.from_path(repo, "")` on the empty source side, raising `ValueError`.
  `_get_push_info` swallows that, is left with no parsed lines, and re-raises the process
  error. The rejection is therefore only ever visible as a `GitCommandError`, which this
  tool catches explicitly and audits as `error:PushRejected`.

Branch names are validated before being interpolated into the refspec. GitPython passes
the refspec as one argv element, so this is not a shell boundary — but a refspec is
`src:dst`, and an embedded colon would silently retarget the deletion at a ref the caller
never named.

The same check covers every caller-supplied name that reaches a push (as of 0.20.0):
`git_push`'s `branch` (and `remote`), `git_tag`'s `tag_name` and `release`'s tag. Anything
that is not a plain name is refused before anything is pushed.

`git_remote` refuses a URL that embeds credentials rather than redacting it — unlike text
on its way out to a caller, a remote URL is written to `.git/config`, where a token would
outlive the call and be reused by every later fetch and push. Only `http(s)://`, `ssh://`,
`git://` and scp-style `user@host:path` are accepted; `ext::`/`fd::` remote helpers are
refused because git runs them as commands on the next fetch. URLs returned by `list` have
any pre-existing userinfo redacted, so a remote added out-of-band cannot leak a token back
through this tool.

`git_push` reports failure explicitly (as of 0.9.0): if any of `ERROR` / `REJECTED` /
`REMOTE_REJECTED` / `REMOTE_FAILURE` is set on the push result — including an empty
ref-update ack — it returns `{"error": ..., "summary": ..., "flags": [...]}` with **no**
`pushed` key. A caller that only checks for `pushed` will no longer silently treat a
rejected push as a success. `flags` are decoded to reason names (e.g. `["REJECTED",
"ERROR"]`), not a raw integer bitmask. `summary` is the remote's human-readable reason,
credential-scrubbed (see Credential isolation below). On success, a missing upstream is
set automatically and reported as `upstream_set: true`.

### GitHub (21)
`github_repo_get`, `github_repo_create`, `github_create_release`, `github_get_release`, `github_list_releases`, `github_release_update`, `github_release_delete`, `github_workflow_list`, `github_workflow_status`, `github_actions` *(run/rerun/rerun_failed/cancel/logs/list_jobs/get_job_log)*, `github_commit_checks`, `github_fork`, `github_pr_list`, `github_pr_comments`, `github_pr_create`, `github_pr_get`, `github_pr_update`, `github_pr_merge`, `github_pr_review` *(get_diff/get_files/get_reviews/submit_review/dismiss_review)*, `github_issue_read` *(get/list/comments)*, `github_issue_write` *(create/update/add_comment/close/reopen)*

`github_pr_update(repo, pr_number, title=None, body=None, base=None,
ready_for_review=False)` (as of 0.15.0) edits title, body or base, and with
`ready_for_review=True` takes a draft out of draft through PyGithub's
`mark_ready_for_review()` — REST cannot change `draft`. The PR number and its review
threads survive, which close-and-recreate did not. It cannot close a PR.

`github_repo_get(repo)` and `gitea_repo_get(repo)` (as of 0.16.0) return a repo's
`full_name`, `visibility` (`public`/`private`/`internal`), `default_branch`, `empty`,
`archived`, `fork`, `parent`, `delete_branch_on_merge`, `ssh_url` and `html_url`. They are
read-only and work for any repo, owned or not. An absent repo, or one the token cannot
see, returns `{"exists": false}` rather than an error. `delete_branch_on_merge` is `null`
when the token is not an admin of the repo, because GitHub only returns that field to
admins: read `null` as "unknown", not "off" (vikunja#1230). On Gitea the field carries the
repo's merge-UI default, `default_delete_branch_after_merge`.

`github_repo_create(name, visibility, description=None)` and
`gitea_repo_create(owner, name, visibility, description=None)` (as of 0.16.0) create an
**empty** repo, with no initial commit, so existing local work can be pushed in
(`git_remote add`, then `git_push`).

- `visibility` (`public`/`private`) is required, with no default.
- GitHub creates under the token's own account. Gitea creates in an **organization
  only**; a user name as `owner` is refused.
- An existing name is refused before the create call.
- The reported `visibility` is read back from the provider, not echoed. If the read-back
  fails or disagrees, the result still says `created: true` and carries a `warning`, and
  the audit record says `ok:visibility_unconfirmed` or `ok:visibility_mismatch` rather
  than plain `ok`.
- `description` refuses control characters and Unicode line breaks, so it cannot print a
  forged `visibility:` line in the HITL notification.

There is deliberately no delete verb and no visibility-change verb; both stay operator
steps. **Gate both create tools with HITL.**

### Gitea (17)
`gitea_repo_get`, `gitea_repo_create`, `gitea_create_release`, `gitea_get_release`, `gitea_list_releases`, `gitea_release_update`, `gitea_release_delete`, `gitea_pr_list`, `gitea_pr_create`, `gitea_pr_get`, `gitea_pr_comment`, `gitea_pr_merge`, `gitea_pr_review` *(get_diff/get_files/submit_review/dismiss_review)*, `gitea_actions` *(list_runs/get_run/list_jobs/get_job_log/dispatch_workflow/rerun_run/rerun_failed_jobs)*, `gitea_commit_status`, `gitea_issue_read` *(get/list/comments)*, `gitea_issue_write` *(create/update/add_comment/close/reopen)*

### GitLab (13)
`gitlab_create_release`, `gitlab_get_release`, `gitlab_list_releases`, `gitlab_release_update`, `gitlab_release_delete`, `gitlab_mr_list`, `gitlab_mr_create`, `gitlab_mr_get`, `gitlab_mr_merge`, `gitlab_mr_review` *(get_diffs/get_changed_files/approve/unapprove/get_approval_state)*, `gitlab_pipeline` *(list/get/create/retry/cancel/get_job_log)*, `gitlab_issue_read` *(get/list/comments)*, `gitlab_issue_write` *(create/update/add_comment/close/reopen)*

### Release Orchestration (1)
`release` — coordinated multi-target release: git tag → GitHub/Gitea/GitLab release → PyPI → npm, with rollback on failure

### Registry (2)
`pypi_publish`, `npm_publish`

### Woodpecker CI (8)
`woodpecker_trigger`, `woodpecker_restart`, `woodpecker_list_pipelines`, `woodpecker_status`, `woodpecker_get_logs`, `woodpecker_pipeline_config`, `woodpecker_queue_info`, `woodpecker_pipeline_cancel`

`pipeline_id` means the per-repo pipeline **number** in every Woodpecker tool, as input and
as output (status, list and restart return it since 0.18.0; trigger since 0.9.0). The global
id comes back separately as `internal_id`, for reference only; no tool accepts it.

- `woodpecker_status` returns the event, ref, commit, `errors[]` (config and linter, with
  `is_warning`), `cancel_info`, `steps[]` (`workflow`, `name`, `type`, `state`,
  `exit_code`, `error`, `started`, `finished`, `uuid`) and a `stalled_hint`: the first
  unfinished step and how long it has been in that state.
- `woodpecker_list_pipelines` sends `perPage`, and passes `status`, `branch`, `event`,
  `ref`, `before` and `after` to the server as filters, so a failure older than the latest
  page is still found. Rows carry `commit` and a bounded `message`.
- `woodpecker_restart` re-runs a pipeline with its original event (push, pull_request), so
  an event-specific failure reproduces. `woodpecker_trigger` creates a `manual` run
  instead. No event or deploy-target override is exposed. It is a write verb: grant it as
  you grant `woodpecker_trigger`.
- `woodpecker_pipeline_config` returns the compiled config files a run used, through the
  same bounded reader as the logs.
- `woodpecker_queue_info` lists running and pending queue items (repo id, pipeline
  number, workflow, agent, age; never labels or environment). It needs an admin token.

### CI log reader

`woodpecker_get_logs`, `woodpecker_pipeline_config`, `gitea_actions get_job_log` and
`github_actions get_job_log` share one bounded, scrubbed reader (`logview.py`, 0.18.0):

- `mode`: `tail`, `head` or `range` (`offset`, `limit`). Line numbers are 0-based;
  `returned_to` is exclusive, so the next page starts at `offset=returned_to`. A `range`
  page that returns nothing (a grep with no hit) still sets `returned_to` to the end of
  the scanned range, so paging advances.
- `grep`: a **literal substring**, not a regex (at most 200 characters, no control
  characters), with `ignore_case` and `context` (0–20 lines). Hits come back as
  `segments`, with overlapping windows merged. Matching runs on the whole scrubbed line,
  before the 2,000-character display cut, and each hit is taken before its context.
- At most 500 lines and about 64 KB per response; `total_lines` and `truncated` are
  present whenever lines come back (a step that has not started returns `lines: null`
  and a `note` instead). Carriage-return progress redraws collapse to the last one.
- Every line goes through `scrub()`, which removes **only githost-mcp's own configured
  tokens and URL userinfo**. Other credentials a pipeline prints (unmasked environment
  values, third-party tokens) pass through, so treat log text as sensitive and don't
  paste it into public PRs or issues. See [Credential isolation](#credential-isolation).
- Log text and the `grep` string are never written to the audit trail or the app log.

`woodpecker_get_logs` with no `step_name` reads the first failed step, else the first
running step, else the first step, and says which in `selected_by`. A failed step defaults
to `tail`, anything else to `head`. A step name used in more than one workflow needs
`workflow`. A step that has not started returns its `state` and `lines: null`.

`github_actions get_job_log` reads one job's plain-text log. GitHub redirects that request
to a signed blob URL; githost-mcp checks the redirect host against an allowlist of Actions
log hosts and fetches it **without** the GitHub token. `github_actions list_jobs` gives the
job ids and their steps.

`gitea_commit_status(repo, ref)` and `github_commit_checks(repo, ref, annotations=False)`
answer "did CI pass on this commit". Woodpecker posts to Gitea as a commit status, so the
Gitea read covers it. With `annotations=True`, the GitHub read returns up to 50 annotations
from failed check runs, which is where GitHub puts readable failure text.

### Audit (1)
`audit_log_query` — query the JSONL audit log by agent, tool, repo, time range, or (0.19.0+)
`run_id`, `task_id` or `trace_id`

## Grant coverage

A new githost verb must not reach an agent by accident, and a destructive one must not
ship without a HITL gate. Three pieces make that checkable by a machine:

- **Every tool is classified.** `effects.py` puts each tool in `READ_TOOLS` or
  `EFFECT_TOOLS`. `HITL_TOOLS` is the subset of effect tools a manifest may grant only
  behind a HITL approval: the three merges, `git_branch_delete_remote`,
  `git_push_force_with_lease`, the three `*_release_delete`, `woodpecker_pipeline_cancel`,
  `release`, `npm_publish`, `pypi_publish` and the two `*_repo_create` (14 tools). A
  closed-world test fails on a registered tool that is unclassified, and on a HITL tool
  that is not an effect tool. A new effect verb's author decides on `HITL_TOOLS` too.
  The rest, `UNGATED_EFFECT_TOOLS`, is pinned by a test as well, so taking a verb out of
  the class is a reviewed edit. Ungated is not the same as harmless: `git_tag` and the
  release create/update tools can trigger an irreversible registry publish in repos that
  publish on a `v*` tag, and `github_actions`/`gitlab_pipeline` can cancel a pipeline.
  `effects.py` gives the reason for each, and their gating is an open decision
  (vikunja#1440).
- **`githost-mcp --list-tools [--json]`** prints every registered tool with its `kind`
  (`read` | `effect`) and `hitl` flag, then exits. It starts no server, needs no env var,
  and writes neither the audit log nor the app log, so it runs against a freshly built
  venv before the service restarts. Exit 0, or 2 on an internal error.

  ```bash
  $ githost-mcp --list-tools --json
  {"server": "githost-mcp", "version": "0.20.0",
   "tools": [{"name": "audit_log_query", "kind": "read", "hitl": false}, ...]}
  ```
- **The consumer is in forge's host-forge/scripts:** `grant-lint.py --tools
  githost-mcp=-` reads that JSON. It refuses a manifest that grants a `hitl: true` tool not
  listed in its `hitl.approval_required`, and names allowlist entries the server no longer
  serves. `githost-grant-gate.sh <venv>` runs it before githost-mcp restarts on a new wheel.

## Audit Architecture

Every tool call writes a JSONL entry before returning:

```json
{
  "ts": "2026-05-27T09:14:23.000Z",
  "agent_id": "sysadmin",
  "tool": "git_push",
  "provider": "local",
  "repo": "/home/user/repos/personal/example-repo",
  "params": {"repo_path": "/home/user/repos/personal/example-repo", "remote": "origin", "branch": null},
  "result": "ok",
  "duration_ms": 312,
  "run_id": "0b6c1f4e-6a8e-4d3e-9b1e-3f2a7c5d9e10",
  "task_id": "eb9be435-48bc-4ded-b05b-1296bcb1cc57",
  "session_id": "6f1d0c2a-7b3e-5c4d-8e9f-0a1b2c3d4e5f",
  "trace_id": "9113fefe1e69c4185a50818bcdaddc17",
  "effect": {
    "remote": "origin",
    "ref": "main",
    "old_sha": "1f0e…",
    "new_sha": "d5ae…",
    "forced": false,
    "lease": false,
    "flags": ["FAST_FORWARD"]
  },
  "hmac": "a3f8..."
}
```

### What a call changed: `effect` (0.19.0+)

A successful write call records what it changed in `effect`: SHAs, the resolved ref, a PR,
issue, review, comment, release, run or pipeline number, and counts. Before 0.19.0 the entry
said only that the call succeeded — `git_commit` recorded `{repo_path, identity}` and
`git_push` recorded `branch: null` whenever the branch defaulted.

| Tool | `effect` |
|---|---|
| `git_commit` | `sha` (full), `parent_sha` (or `root: true`), `files_changed` (count) |
| `git_push`, `git_push_force_with_lease` | `remote`, `ref`, `old_sha` (or `created: true`), `new_sha`, `forced`, `lease`, `flags` |
| `git_pull`, `git_checkout` | `ref`, `old_head`, `new_head` |
| `git_tag`, `git_branch` | `name`, `target_sha`, `action` |
| `git_branch_delete_remote` | `remote`, `ref`, `old_sha`, `action` |
| `git_clone`, `git_worktree` | `path`, `ref`, `head_sha` (clone: the remote `host`, never the URL) |
| `git_add` | `paths_staged` (count) |
| `git_remote` | `name`, `action` (add: the URL's `host`) |
| `*_pr_create` / `gitlab_mr_create` | `pr_number`, `head_sha`, `base` |
| `*_pr_merge` / `gitlab_mr_merge` | `pr_number`, `merge_sha`, `method` |
| `*_pr_review`, `gitea_pr_comment`, `gitlab_mr_review` | `pr_number`, `review_id` or `comment_id`, `state` |
| `github_pr_update` | `pr_number`, `updated_fields` (names), `marked_ready` |
| `*_issue_write` | `issue_number`, `action` (and `comment_id`) |
| `*_create_release`, `*_release_update`, `*_release_delete` | `tag`, `release_id`, `action` |
| `release` | `tag`, `tag_sha`, `targets` (per-target `ok`/`error`) |
| `pypi_publish`, `npm_publish` | `target`, `package`, `version` |
| `woodpecker_trigger`, `woodpecker_restart` | `pipeline_id` (the per-repo number) |
| `woodpecker_pipeline_cancel`, Actions/pipeline write methods | the run or pipeline id, `action` |
| `*_repo_create`, `github_fork` | `repo`/`fork`, `repo_id`, `visibility` |

Rules:

- **Counts and ids only.** No commit message, PR or issue body, file name, URL or log text
  goes into `effect`. Strings are scrubbed (configured tokens, then URL userinfo by shape)
  and then bounded: 256 characters, 16 list items, 16 fields, one level of nesting.
- A field that could not be determined is `null` and named in `effect_incomplete`. The call
  still succeeds — telemetry never fails a tool call.
- Every tool is classified in `effects.py` as `EFFECT_TOOLS` or `READ_TOOLS`, and a test
  fails on an unclassified one. A multi-method tool (`gitea_actions`, `github_pr_review`, …)
  records an effect only for its write methods. A successful write call that recorded no
  effect logs `effect_not_recorded` and is marked `effect_incomplete: ["effect_not_recorded"]`.
- `effect` is a plain dict. New fields extend it.

### Which run, task and trace a call belonged to (0.19.0+)

Every entry carries `run_id`, `task_id`, `session_id` and `trace_id`, `null` when absent:

- `run_id` and `task_id` are the dispatcher run and task-queue task, and `session_id` is
  scoped-mcp's per-client session. scoped-mcp ≥ 1.20.0 forwards them as `X-Forge-Run-Id`,
  `X-Forge-Task-Id` and `X-Forge-Session-Id` when the manifest's githost block sets
  `forward_identity: true`. Run and task ids must be UUIDs and the session id must match
  `[A-Za-z0-9._:-]{1,128}`; anything else is dropped and named in `correlation_invalid`,
  never recorded. githost-mcp's own MCP session id is useless here: scoped-mcp opens a new
  HTTP connection per call.
- `trace_id` is the OTel trace of the `githost.<tool>` span. It joins an entry to SigNoz even
  for an interactive session that has no run id.

These ids are attribution, not authorization — see
[Forwarded correlation ids](#forwarded-correlation-ids).

```python
audit_log_query(task_id="eb9be435-48bc-4ded-b05b-1296bcb1cc57")
```

`effect` and the correlation fields sit inside the HMAC'd entry: altering either makes the
entry verify as `tampered`.

### Spans

The `githost.<tool>` span opens when the call's `AuditCtx` is created and ends when the
entry is written, so its own duration is the call's duration (`githost.duration_ms` is kept
as an attribute). Before 0.19.0 it was opened after the work, 23 µs long. Inside it:

- `git.push`, `git.fetch`, `git.pull`, `git.clone`, `git.commit`, `git.add`, `git.checkout`,
  `git.worktree` and `git.ls_remote` child spans around the git operations;
- `http.gitea`, `http.woodpecker` and `http.github` child spans per request, and
  `http.github` / `http.gitlab` per PyGithub / python-gitlab call.

Child-span attributes are a fixed set: method, host, status code, the remote's name, the API
call's method name, and a **path template** built from a fixed vocabulary of API words
(`/api/v1/repos/{}/{}/pulls`). No URL, query string, token, body or exception message goes on
a span; a failed child records only the exception type. The span also carries `forge.run_id`,
`forge.task_id` and `forge.session_id`. W3C trace propagation is FastMCP's: the tool span is a
child of FastMCP's server span, which continues scoped-mcp's trace.

A call that did not succeed also carries a `reason` — the exception message, scrubbed
through `security.scrub()`:

```json
{
  "tool": "git_add",
  "result": "denied:write_glob",
  "reason": "Write denied by policy write_globs scope for '/srv/repo': ['secrets.env'] (source: policy)",
  "duration_ms": 4
}
```

`reason` exists because `result` carries the exception *type* and not its message, so
allowlist rejection, bad repo format and missing remote were all indistinguishable inside
one `error:ValueError` bucket. It is an audit-record field and deliberately **never** a
metric label — messages contain repo paths and branch names, which is unbounded
cardinality. The bounded counterpart is `reason_class` on the `githost.tool.denied` counter,
drawn from a closed vocabulary in `errors.py` and derived from the exception type rather
than from a regex over its message.

A call refused for a bad argument value (as of 0.15.0, e.g. an unknown `method`) also
records which argument was rejected and what was passed, in `params`:

```json
{
  "tool": "github_issue_write",
  "params": {"repo": "owner/repo", "argument": "method", "rejected_value": "comment"},
  "result": "denied:invalid_argument",
  "reason": "method 'comment' is not valid; must be one of: add_comment, close, create, reopen, update. To read an issue, list issues or read comments, use github_issue_read"
}
```

| Field | Meaning |
|---|---|
| `params.argument` | Name of the rejected argument. |
| `params.rejected_value` | The value passed, `null` when it was absent. Scrubbed through `security.scrub()` first, then cut to 64 characters. |
| `params.rejected_value_truncated` | Present, and `true`, only when the value was cut. |

Scrubbing happens before truncation so a credential is never split into a prefix the
scrubber no longer recognises. The scrub has a known limit; see
[Credential isolation](#credential-isolation). An empty value is audited as
`denied:missing_argument` rather than `denied:invalid_argument`. **Audit consumers that
parse `params` will see these keys.**

Each entry is HMAC-SHA256 signed **when `AUDIT_SIGNING_KEY` is set** — with no key the `hmac`
field is simply absent and the entry carries no tamper evidence at all. `audit_log_query`
classifies every returned entry in an `integrity` field, and reports `integrity_summary` counts
plus `signing_key_configured` alongside the results:

| `integrity` | `tamper_detected` | Meaning |
|---|---|---|
| `verified` | `false` | Signed, and the HMAC matches. |
| `tampered` | `true` | Signed, but the HMAC does not match — the entry was altered. |
| `unsigned` | `null` | No `hmac` field. Written while the agent had no key; nothing can be confirmed. |
| `unverifiable` | `null` | Signed, but this process holds no key to check it against. |

`tamper_detected` is retained for older callers and is `null` — never `false` — whenever
integrity could not be established. An unsigned entry is not a clean bill of health.

Example — what did the sysadmin agent push last week?

```python
audit_log_query(agent_id="sysadmin", tool="git_push", since="2026-05-20")
```

## Security Model

### Repo path allowlist

`Config` carries separate `allowed_read_roots` and `allowed_write_roots`. Read tools
(`git_status`, `git_diff`, `git_log`, `git_show`, `git_branch list`, `git_remote list`,
`git_worktree list`) validate against the read list; write tools (`git_add`, `git_commit`,
`git_push`, `git_push_force_with_lease`, `git_pull`, `git_tag`, `git_checkout`,
`git_branch create/delete`, `git_branch_delete_remote`, `git_remote add/remove`,
`git_worktree add/remove`, `release`) validate against the write list, as does the
destination `git_clone` computes. `allowed_repo_roots` remains
as a deprecated alias of `allowed_write_roots` for any caller not yet migrated.

Resolution order (first match wins, see Architecture diagram above):

1. `ALLOWED_REPO_ROOTS` env — the break-glass override, unchanged: applies the same root
   list to both read and write.
2. `/etc/forge/workspace-policy.yml` (path overridable via `WORKSPACE_POLICY_PATH`) — a
   central grant file keyed by agent ID, giving `read_roots`, `write_roots`, and
   optionally `write_globs`/`write_globs_deny` per agent. Once this file loads
   successfully it is authoritative for the requesting agent — an agent with no entry in
   `agents:`/`explicit_agents:` gets zero roots and does **not** fall through to the
   manifest. A missing, unreadable, or non-mapping file is the only case that falls
   through.
3. The agent manifest's `git_backed: true` `workspace_access` entries at
   `AGENT_MANIFEST_PATH` — now the third fallback rather than the second.
4. Empty — fail closed, all operations disabled.

**Recommended shape.** Declare a small set of *container roots* (e.g. `~/repos/gitea`,
`~/repos/personal`) rather than per-repo paths, so a repo created inside an
already-granted root is covered automatically — per-repo manifest entries leave every new
repo without access until someone adds it. `default_read: all` grants every agent listed in
`agents:` read across every declared root regardless of that agent's own `write_roots`;
`write_roots` and `write_globs`/`write_globs_deny` are then set per agent (e.g. a
docs-writing agent scoped to `docs/**`-style globs within its write roots — see
[Write glob scoping](#write-glob-scoping) below). Agents not listed in `agents:` or
`explicit_agents:` get nothing.

**Behavior change (relevant to any deployment still on manifest-only resolution):** a
manifest `access: readonly` entry now populates `allowed_read_roots` (previously it
granted **no access at all**). `access: readwrite` continues to populate both read and
write lists.

**When no source yields a root for the requested operation, it is denied** — fail closed,
not open. A malformed or unreadable manifest or policy file resolves to zero roots rather
than raising, so it fails closed the same way an unset `ALLOWED_REPO_ROOTS` does.

### Write glob scoping

A `workspace-policy.yml` grant can additionally narrow write access to a glob subset of
`allowed_write_roots` via `write_globs` (allow) and `write_globs_deny` (deny) — e.g.
scoping the writer agent to `docs/**` within a repo it otherwise has full write roots for.
`validate_write_globs()` (`security.py`) enforces this in `git_add` (against the paths
passed in) and again in `git_commit` (against the *full staged set*, since a commit
commits whatever is staged regardless of what a prior `git_add` call itself validated).
The deny list is evaluated after the allow list and wins — an allow pattern like
`**/*.md` can never override a deny entry such as `**/AGENT_WORKSPACE.md`. An agent with
neither `write_globs` nor `write_globs_deny` configured is unrestricted within its
`allowed_write_roots`, matching prior behavior.

Patterns are plain `fnmatch` globs, not path-aware doublestar globs: `**/*.md` requires a
literal `/` before the filename and will not match a bare top-level `README.md` — the
policy schema accounts for this with separate `README*`/`CHANGELOG*`-style entries for
root-level files. Paths are normalized with `os.path.normpath()` before matching, and any
path whose normalized form is absolute or still starts with `..` is denied outright,
independent of glob match — closing a traversal shape (`docs/../src/exploit.py`) that
would otherwise textually match a `docs/**` allow glob. A rejection raises
`WriteGlobDenied`, logged as a distinct `denied:write_glob` audit-trail result rather than
the generic `error:ValueError` other validation failures get.

As a fail-closed backstop, if a resolved grant carries `write_globs`/`write_globs_deny`
but the running code has no enforcement path for it, writes are denied entirely rather
than silently becoming unrestricted across the full `allowed_write_roots`. Enforcement
now exists (`_GLOB_ENFORCEMENT_IMPLEMENTED = True` in `security.py`), so this backstop is
currently dormant — it exists to prevent a future revert of the enforcement code from
silently widening a glob-scoped agent's grant again.

### Per-agent committer identity

`GIT_AGENT_NAME` and `GIT_AGENT_EMAIL` set the git author/committer on commits to repos
you control. Defaults to `{AGENT_ID}-agent` / `{AGENT_ID}@forge` when not explicitly set.
Values are sanitized (newlines and null bytes stripped) to prevent git header injection.
Those commits also append `agent-id: {AGENT_ID}` as a trailer.

### Public identity on third-party repos

An agent identity is useful on repos you control and is a disclosure on ones you don't: it
names your internal agents in permanent public git history, where an external maintainer
has no use for it. `git_commit` therefore picks its identity from the repo's remotes.

A repo is **forge-controlled** when *every* remote is either owned by an account listed in
`FORGE_OWNED_OWNERS` or hosted on the configured `GITEA_URL` host (any org). Those commits
get the agent identity and the `agent-id:` trailer. If **any** remote is third-party, the
commit gets `GIT_PUBLIC_NAME`/`GIT_PUBLIC_EMAIL` and no trailer.

Detection is by remote rather than by a flag the caller passes, because the caller
forgetting the flag is precisely how the existing contamination happened.

It deliberately does not read the owner of `origin` alone. Which remote is `origin` is an
artifact of how the clone was made — for a fork checkout, `origin=upstream, fork=ours` and
`origin=ours, upstream=theirs` are both common — so an origin-only rule answers the same
situation two different ways, and gets the fork-under-your-own-account case wrong in the
leaking direction.

| Situation | Identity |
|---|---|
| No remotes | agent — nothing to publish to |
| All remotes on the Gitea host | agent + `agent-id:` trailer |
| GitHub repo under a forge-owned account, **not** a fork of a repo you don't own | agent + trailer |
| GitHub repo under a forge-owned account that **is** a fork of a repo you don't own | public, no trailer |
| Fork provenance could not be determined | public, no trailer |
| Any third-party remote | public, no trailer |
| A remote URL that cannot be parsed | **refused** |
| A local filesystem path remote | agent — a path on your own disk |

#### Why the remotes alone are not enough

`TadMSTR/githost-mcp` (a project of yours) and `TadMSTR/claudecodeui` (your fork of someone
else's project) are byte-identical from the remotes alone. Clone the fork directly and never
add an upstream remote — a normal thing to do, and a cross-repo PR can be opened from the API
without one — and remote-parsing alone says "forge-controlled", writing the agent identity
into a third-party PR.

So when the remotes say forge-controlled *and* a remote is a GitHub repo under a forge-owned
account, `git_commit` consults GitHub's own record of whether that repo is a fork, and of
what. The result is cached in the repo's `.git/config` under
`githost-mcp.upstream-provenance`, so this costs one API call per repo, not one per commit.
Repos on the Gitea host, and repos already resolved as third-party from their remotes, never
trigger a lookup.

If the lookup cannot be completed — no `GITHUB_TOKEN`, no network, rate limited — the commit
resolves to the **public** identity, and the result is not cached, so a transient failure does
not pin the answer for the repo's lifetime. That direction is deliberate: the cost is a
missing `agent-id:` trailer on an internal repo, against leaking the agent identity into
permanent public history. Since the audit log records the real acting agent in both modes,
what is lost is a convenience, not accountability.

Two refusals, both deliberate. An unparseable remote is refused rather than guessed at:
defaulting to the agent identity leaks it, defaulting to the public identity breaks
attribution on internal repos, and an error the caller can resolve with an explicit
`identity=` argument beats either. A resolved public identity that still looks like an
agent identity (`*@forge`, `*-agent`) is also refused — a repo-local `user.email` is enough
to produce one, and it would be the same leak under a different label.

`GIT_PUBLIC_NAME`/`GIT_PUBLIC_EMAIL` fall back to the repo's own git config
(`user.name`/`user.email`) when unset.

**This changes the commit object only.** Every audit entry records the real acting agent in
both modes — `write_audit_entry` reads the process-wide agent ID and never consults identity
resolution. Inverting that would turn a disclosure fix into an accountability hole.

`git_commit(identity=...)` overrides the detection in either direction: `auto` (default),
`agent`, or `public`.

### Query limits

`git_log` caps the `limit` parameter at 200 entries regardless of the requested value, preventing excessive history traversal.

### No subprocess git

All local git operations use **gitpython** (Python library), not subprocess. This eliminates command injection risk via crafted `repo_path` or `branch` values.

### Credential isolation

Token values never appear in:
- JSONL audit entries (credential filter applied before write)
- structlog output (processor filter bound to logger)
- tool return values (scrubbed before return)
- exception messages (caught at provider layer and re-raised without token value)

As of 0.9.0, every caller-facing error return in the tool modules (`git_local.py`,
`release.py`, `woodpecker.py`, `registry.py`, `gitea.py`/`github.py`/`gitlab.py`) is
scrubbed via **`security.scrub()`** — `redact_url_credentials(mask_credentials(text))` —
rather than `mask_credentials()` alone. `mask_credentials()` only replaces githost-mcp's
own *configured* token values, so a credential a human embedded in a remote by hand
(`https://user:token@host/...`) previously survived it. `redact_url_credentials()` strips
the userinfo component from any scheme-qualified URL by shape, independent of whether
githost-mcp knows the token. scp-style remotes (`git@github.com:owner/repo.git`) have no
scheme and are left readable — that's the form most SSH remotes use. This
closes the gap where `git_push`'s new `summary` field (the remote's raw rejection text)
could otherwise have surfaced a credential verbatim.

**Known limit (vikunja#913, deferred).** `scrub()` removes githost-mcp's own configured
token values and URL userinfo, and nothing else. A credential the process does not hold
in its config, appearing **outside** a URL (e.g. `Authorization: Bearer <token>` in a
remote's error text), passes through both passes intact, into the tool's return value and
into the audit log, including `rejected_value`. A shape-based redactor was deliberately
not added: `scrub()` sits on every return path, and an over-broad one would silently
mangle legitimate error text.

Each provider has its own env vars — a compromised GitHub token does not expose Gitea or GitLab credentials.

### HMAC tamper-evidence

`AUDIT_SIGNING_KEY` is a server-side secret set in the launcher, per agent. When it is set, each
JSONL entry includes `hmac: HMAC-SHA256(canonical_json, key)`. This is symmetric (same key signs
and verifies) — it proves the file wasn't edited after write, not that the agent identity is
genuine. Agent identity proof is the scoped-mcp layer's job.

The key is **not enforced at startup**: an agent launched without one starts normally, logs an
`audit_signing_key_unset` warning naming itself, and writes unsigned entries from then on. Those
entries report as `unsigned` from `audit_log_query` (see [Audit Architecture](#audit-architecture))
rather than as verified, and stay identifiable as unsigned after a key is later added — the
absence of an `hmac` is a property of the entry, not of the current config. Refusing to start
without a key is a deployment policy choice and is deliberately not made here; it would take an
agent offline for a missing secret rather than degrade visibly.

### Forwarded correlation ids

`run_id`, `task_id` and `session_id` are **attribution, not authorization**. githost-mcp trusts
any caller that holds its bearer token, and anyone holding the token can send any value in
the `X-Forge-*` headers. In forge's deployment only scoped-mcp holds it, but a process running
as the same user that reads the token could forge a run id. Nothing in githost-mcp gates on
these ids, and nothing should: they are validated for shape, recorded, and that is all.

### HTTP transport surface

`TRANSPORT=http` (see [Deploy](#deploy)) opens a local network listener where stdio mode has
none. Two controls are mandatory together, not either/or:

- **Loopback-only bind, fail closed.** `main()` refuses to start if `HTTP_HOST` resolves to
  anything other than `127.0.0.1` / `localhost` / `::1`, unless `GITHOST_MCP_ALLOW_NONLOOPBACK=1`
  is set explicitly. There is no default that silently exposes the port beyond the host.
- **Bearer token auth.** When `GITHOST_MCP_AUTH_TOKEN` is set, FastMCP's built-in
  `StaticTokenVerifier` rejects any request without a matching `Authorization: Bearer <token>`
  header (401). scoped-mcp's manifest `headers` block supplies it — see
  [Running as PM2 services](#running-as-pm2-services). The token is included in the credential filter (`audit.py` / `security.py`),
  so it's never written to logs or the audit trail — **provided it's at least 16 characters**;
  the scrub only redacts tokens over 4 characters, so `main()` separately hard-fails on a
  shorter token rather than silently accepting one the filter can't reliably catch.

githost-mcp will not ship `TRANSPORT=http` with a reachable port and no token configured, or with
a token under 16 characters — both are config errors, not a supported deploy shape. In stdio mode
(the default), neither control is relevant: there's no listening port to protect.

## Environment Variables

### Required

```env
AGENT_ID=dev                     # agent attribution — set per launcher
AUDIT_SIGNING_KEY=<32-byte-hex>  # generate: python3 -c "import secrets; print(secrets.token_hex(32))"
ALLOWED_REPO_ROOTS=/home/user/repos/personal,/home/user/repos/work  # enforced on ALL tools (read + write)
```

`ALLOWED_REPO_ROOTS` is not strictly required if `AGENT_MANIFEST_PATH` resolves to a manifest
with `git_backed: true` `workspace_access` entries (see `AGENT_MANIFEST_PATH` below) — but one
of the two must yield at least one root, or every path-taking tool is denied.

```env
AGENT_MANIFEST_PATH=/home/user/.claude/manifests/dev-agent.yml  # optional — allowlist fallback
# Default: ~/.claude/manifests/{AGENT_ID}-agent.yml (only when AGENT_ID is set to a real identity)
# The shipped ecosystem.config.js overrides this per-process to
# /etc/forge/manifests/<agent>-agent.yml — see Deploy > Manifest allowlist path.

WORKSPACE_POLICY_PATH=/etc/forge/workspace-policy.yml  # optional — checked ahead of the manifest
# Default: /etc/forge/workspace-policy.yml. See Security Model > Repo path allowlist for
# the full env > policy > manifest > empty resolution order, and for the container-root
# grant model.
```

### Agent Identity (optional)

```env
GIT_AGENT_NAME=dev-agent         # git author/committer name (default: {AGENT_ID}-agent)
GIT_AGENT_EMAIL=dev@forge        # git author/committer email (default: {AGENT_ID}@forge)

# Identity used instead, on any repo that has a third-party remote. Falls back to the
# repo's own git config user.name/user.email when unset. See Security Model >
# Public identity on third-party repos.
GIT_PUBLIC_NAME=YourAccount
GIT_PUBLIC_EMAIL=12345+YourAccount@users.noreply.github.com

# Accounts/orgs you control, comma-separated (default: TadMSTR). Repos whose remotes
# all sit under one of these — or on the GITEA_URL host — keep the agent identity.
FORGE_OWNED_OWNERS=YourAccount,YourOrg

# GPG key ID to sign commits with (optional; unsigned when unset)
GIT_SIGNING_KEY=
```

`FORGE_OWNED_OWNERS` also bounds `git_clone`: it is the GitHub owner allowlist.

### Worktrees and clones (optional)

```env
# Where git_worktree creates scratch worktrees: <root>/<repo>-<name>.
# Default ~/repos/personal/_worktrees; ~ is expanded at startup.
GITHOST_WORKTREE_ROOT=~/repos/personal/_worktrees
```

The root is a location, not a grant: put it inside the agent's write roots, or every
`git_worktree add` is refused. `git_clone` has no setting; its destinations are fixed (see
[Local Git](#local-git-16)).

### GitHub

```env
GITHUB_TOKEN=<PAT with repo scope>
GITHUB_OWNER=YourOrg
```

### Gitea

```env
GITEA_URL=https://gitea.example.com
GITEA_TOKEN=<PAT>
GITEA_OWNER=youruser
```

### GitLab

```env
GITLAB_URL=https://gitlab.com
GITLAB_TOKEN=<PAT>
```

### Woodpecker CI

```env
WOODPECKER_URL=https://ci.example.com
WOODPECKER_TOKEN=<API token>
```

### Registry

```env
PYPI_TOKEN=<API token>
PYPI_TEST_TOKEN=<API token>      # only for pypi_publish(target="testpypi")
NPM_TOKEN=<automation token>
```

### Logging (always on)

```env
LOG_LEVEL=INFO
LOG_FILE=/opt/appdata/githost-mcp/logs/githost-mcp.log      # default
AUDIT_LOG_FILE=/opt/appdata/githost-mcp/audit/githost.jsonl  # default

# Audit JSONL rotation
AUDIT_LOG_MAX_BYTES=10485760     # default 10 MB; 0 disables rotation
AUDIT_LOG_BACKUP_COUNT=5         # default 5; .jsonl.1 is newest

# Application log rotation — defaults to the AUDIT_LOG_* values above
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
```

Both files rotate by rename (`.1` newest); entries are never truncated or rewritten. Audit
HMACs are per-entry rather than a chain, so a rotated entry verifies exactly as it did before
the rename, and `audit_log_query` searches the rotated backups as well as the live file —
its `sources_searched` field reports which files a given result actually covered.

### Observability (all opt-in)

```env
# OTEL (SigNoz, Honeycomb, Grafana Tempo, Jaeger, Datadog — same env var)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
# Pair the protocol with the port. Default is grpc; :4318 needs http.
OTEL_EXPORTER_OTLP_PROTOCOL=grpc
# Per-process, or every agent collapses into one service in the backend.
OTEL_SERVICE_NAME=githost-mcp-<agent>

```

OTel is the only telemetry export: traces (see [Spans](#spans)) and the `githost.tool.calls`,
`githost.tool.denied`, `githost.tool.duration` and `githost.release.targets` metrics. The audit
JSONL is the durable record.

> **Removed in 0.19.0: `LOKI_URL`, `LOKI_LABELS`, `METRICS_PORT`, `NATS_URL`,
> `NATS_SUBJECT_PREFIX`.** The Loki push and NATS publisher had no caller and the Prometheus
> endpoint had no scraper. A set variable is ignored and logged once, by name, as
> `config_ignored_removed_backend`. The `loki`, `prometheus` and `nats` extras are gone too;
> `observability` remains, as `otel`.

> **`OTEL_EXPORTER_OTLP_PROTOCOL` must agree with the port in the endpoint.** It defaults
> to `grpc`, so setting only `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` points
> the gRPC exporter at an HTTP port, and it fails **silently** — `_init_otel()` swallows
> the error and the process starts normally with no telemetry at all. Use `:4317` with
> `grpc`, or `:4318` with `OTEL_EXPORTER_OTLP_PROTOCOL=http`. `ecosystem.config.js` sets
> both explicitly rather than relying on the default, so the pairing is checkable by
> reading one place. Verify by querying the backend for a `githost.*` series — an
> exporter that initialised proves nothing about whether anything arrived.

> **`OTEL_SERVICE_NAME` defaults to `githost-mcp` for every process.** With several agents
> that is one service with several writers, which makes "which agent hit this limit"
> unanswerable from traces — the question the telemetry exists to answer.
> `ecosystem.config.js` sets `githost-mcp-<agent>`, matching the `scoped-mcp-<agent>`
> convention.

### Transport (optional — default stdio)

```env
TRANSPORT=stdio          # or "http" — see Deploy below
HTTP_HOST=127.0.0.1      # http mode only; must be loopback unless overridden below
HTTP_PORT=<port>         # http mode only; one free loopback port per agent
GITHOST_MCP_ALLOW_NONLOOPBACK=   # set to "1" to bind a non-loopback HTTP_HOST (not recommended)
GITHOST_MCP_AUTH_TOKEN=  # required whenever TRANSPORT=http; must be >= 16 chars
```

## Installation

githost-mcp is not published to PyPI. Install from a checkout of a release tag, from
the committed `uv.lock`, so the dependency set is the one CI tested and audited.

```bash
VERSION=0.20.0
VENV=/opt/venvs/githost-mcp

# 1. The checkout is clean and is exactly the release tag on the remote. A local tag
#    can be moved, and a dirty tree would be installed as it stands.
git fetch --tags origin
test -z "$(git status --porcelain)"
# The peeled line (^{}) is listed last for an annotated tag; a lightweight tag has one line.
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin "refs/tags/v$VERSION" "refs/tags/v$VERSION^{}" | tail -n1 | cut -f1)"

# 2. Build a fresh venv at the path the service runs from. Pin the interpreter: uv
#    otherwise prefers a Python it manages itself, which `uv python` can later remove
#    or replace under a running service. --no-editable installs a copy of the package,
#    so the service does not follow later changes to the checkout.
UV_PROJECT_ENVIRONMENT=$VENV \
  uv sync --frozen --no-editable --extra observability --python /usr/bin/python3.13

# 3. Confirm what was installed.
"$VENV/bin/python3" -c "import importlib.metadata as m, githost_mcp.server; print(m.version('githost-mcp'))"
```

Build a fresh venv rather than installing into an existing one. `pip install .` into an
existing venv keeps whatever older dependency versions it finds there, if they still
satisfy the declared ranges. Build at the final path rather than building elsewhere and
moving it: console-script shebangs embed the venv path. A uv-built venv has no `pip`.

CI audits the locked set on every push and weekly, but nothing reads a scheduled run's
result unless you watch it. Audit the deployed venv where it runs as well (for example
`pip-audit --path <venv>/lib/python3.13/site-packages`).

## Launcher pattern (scoped-mcp subprocess)

```bash
#!/bin/bash
# run-githost-mcp-dev.sh
export AGENT_ID="dev"
export ALLOWED_REPO_ROOTS="/home/user/repos/personal,/home/user/repos/work"
export AUDIT_SIGNING_KEY="$(cat /run/secrets/githost_audit_key)"
export GITHUB_TOKEN="$(cat /run/secrets/github_token)"
export GITEA_TOKEN="$(cat /run/secrets/gitea_token)"
export LOG_FILE="/var/log/githost-mcp/githost-mcp.log"
export AUDIT_LOG_FILE="/var/log/githost-mcp/githost.jsonl"
exec /opt/venvs/githost-mcp/bin/githost-mcp
```

This is `TRANSPORT=stdio` (the default): scoped-mcp spawns a fresh subprocess per call and
tears it down afterward. Simple, but OTel metrics and batched spans rarely survive that short
a lifetime — they reset or drop every call.

## Deploy

Transport is selected by `TRANSPORT` (`stdio` default, or `http`) so both models are supported
by the same codebase — no fork, no rewrite to move between them.

| | `stdio` (default) | `http` |
|---|---|---|
| Process lifetime | One per scoped-mcp call, recycled every turn | Long-lived, one PM2 service per agent |
| Observability | Rotating file log + audit JSONL only — OTel metrics and spans rarely survive teardown | All of it works — metrics accumulate, spans flush |
| Restart | N/A — recycled automatically | `pm2 restart githost-mcp-<agent>`, independent of scoped-mcp |
| Network surface | None | Local HTTP listener — must be loopback-bound + token-authed (see [Security Model](#http-transport-surface)) |

**Per-agent processes, not one shared process.** Each agent gets its own OS process (own
`AGENT_ID`, own tokens, own `ALLOWED_REPO_ROOTS`), so a compromised process can't see another
agent's credentials and `AGENT_ID` can't be spoofed via a request header. This is a deliberate
security tradeoff over a single shared process with per-request identity, which is
deliberately deferred.

### Running as PM2 services

`ecosystem.config.js` in this repo builds one app per agent from a single `AGENT_ID -> {httpPort}`
map, reading a per-agent env file (`githost-mcp-<agent>.env`) and a shared env file
of tokens — the same files the stdio launchers read, so there is no separate secret plumbing to
maintain. Both paths are set near the top of the file; adjust them for your host.

```bash
pm2 start ecosystem.config.js
pm2 save
```

Each service comes up with `TRANSPORT=http`, `HTTP_HOST=127.0.0.1`, its own `HTTP_PORT`, and `GITHOST_MCP_AUTH_TOKEN` sourced from the shared env file. Point
scoped-mcp's manifest at the corresponding `http://127.0.0.1:<port>/mcp/` URL with the token in
an `Authorization: Bearer` header (requires `type: http` on the manifest block — a bare
`{url, headers}` entry is silently skipped).

### Manifest allowlist path

When `ALLOWED_REPO_ROOTS` is unset for an agent, `AGENT_MANIFEST_PATH` (default
`~/.claude/manifests/{AGENT_ID}-agent.yml`) is the allowlist's only other source. The
shipped `ecosystem.config.js` overrides that default to `/etc/forge/manifests/<agent>-agent.yml`
for every agent process — intended to be a root-owned, `0644` copy published from the
manifests repo's `main` by a deploy script, not a symlink into a live git working tree. The
target directory matters as much as the file mode: directory *write* permission governs
`rename`/`unlink` regardless of who owns the file inside it, so a root-owned file under a
directory the service user can write isn't actually protected — anyone who can
write the directory can swap the file out from under its own permissions. `/etc` is root-owned
end to end, which is why the deployed copy lives there instead.

**Deployers must create and populate `/etc/forge/manifests` before unsetting
`ALLOWED_REPO_ROOTS` for any agent.** `config.py` does not fall back further if the target file
is missing or unreadable — the allowlist resolves empty (fail closed), and the agent loses all
repo access, rather than silently reusing the old default path.

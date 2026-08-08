# Changelog

## [Unreleased]

### Added — read/write allowlist split + workspace-policy.yml loader (workspace-policy Phase 1, vikunja #349)

`Config` now has separate `allowed_read_roots` and `allowed_write_roots`, and
`validate_read_path`/`validate_write_path` check the matching list instead of both running
identical logic against one shared list. `allowed_repo_roots` remains as a deprecated alias of
`allowed_write_roots` for any caller not yet migrated.

Resolution order is now `ALLOWED_REPO_ROOTS` env (applies to both lists — unchanged as the
break-glass override) → `/etc/forge/workspace-policy.yml` (new; path overridable via
`WORKSPACE_POLICY_PATH`) → agent manifest `workspace_access` (unchanged, now the third
fallback rather than the second) → empty, fail closed. This build ships with the policy file
**absent** in production — sysadmin deploys it in a follow-on phase — so today's live
behaviour is unchanged: with no policy file, resolution falls straight through to the manifest,
exactly as before this release.

### Security — write denied when a grant carries write_globs but enforcement isn't implemented yet

Audit finding (MEDIUM, `githost-workspace-policy-2026-08`): `_load_policy` loads
`write_globs`/`write_globs_deny` into `Config`, but glob enforcement itself is Phase 3, not yet
built. Without a guard, an agent granted a glob-scoped write (e.g. writer, meant to be limited
to `docs/**`) would silently get **unrestricted** write across its full `allowed_write_roots`
instead — the glob loaded but never checked. `validate_write_path` now fails closed whenever
`write_globs`/`write_globs_deny` is non-empty for the resolved config and
`_GLOB_ENFORCEMENT_IMPLEMENTED` (`security.py`) is `False`. Phase 3 flips that flag when it adds
enforcement. No live effect today — the policy file isn't deployed yet.

### Changed — `access: readonly` in an agent manifest now grants read (behaviour change, not a bugfix)

Previously, `_load_manifest_roots` admitted an entry to the single shared allowlist only when
`access: readwrite`; any other value — including `readonly` — was dropped entirely, so
`access: readonly` granted **no githost-mcp access at all**, not even read. This was documented
and deferred (see the removed docstring at old `config.py:86-90`) and was the root cause behind
repeated per-agent read-grant gaps (vikunja #203/#332/#308).

With the read/write split, `access: readonly` now populates `allowed_read_roots` (still not
`allowed_write_roots`). **This changes what an existing `access: readonly` entry in a deployed
manifest means** — any agent manifest already carrying a `git_backed: true`, `access: readonly`
entry gains read access to that path, which it did not have before. One `access: readonly`
entry exists on forge today (`sysadmin-agent.yml`, `/mnt/atlas/`), but it is not `git_backed:
true` and so remains excluded by that separate, unchanged gate — no live entry is affected by
this deploy. Still a semantics change to a manifest key already in the schema, not merely a fix
to unreleased code, and the next `git_backed: true` + `access: readonly` entry added to any
manifest will grant read the moment this ships, with no code change required.

## [0.10.0] — 2026-08-01

### Fixed — reliability batch 2: unchecked pushes, empty-result crash, audit log rotation (vikunja id 311)

Second batch of the defect class v0.9.0 started and did not finish: **a tool reports success,
or drops the information needed to diagnose failure.**

**Tag pushes were never checked (highest severity).** `git_tag(push=True)` discarded the
`PushInfo` and set `"pushed": True` unconditionally. Worse, `release()` did the same and then
proceeded through every provider step, creating GitHub/Gitea/GitLab releases pointing at a tag
the remote may never have received. Both now decode the push result and fail loudly; `release()`
aborts into `_rollback` before any provider step runs. Two further defects surfaced while
testing that rollback: the local tag was only marked for rollback *after* the push returned
(so a create-succeeds/push-raises path orphaned it), and `_rollback` deleted the **remote** tag
whenever the local one existed — so a rejected push followed by a rollback destroyed a
pre-existing remote tag the release never pushed. Local and remote tag creation are now tracked
separately.

**`github_pr_list` crashed on zero results** (vikunja #178, id 189), returning
`{"error": "list index out of range"}` and making "no open PRs" indistinguishable from a real
API failure. Root cause is PyGithub's `PaginatedList._Slice.__iter__`, so it hit
`github_workflow_list` and `github_issue_read` too — wider than filed. Fixed with
`itertools.islice`. Gitea and GitLab were verified unaffected.

**The audit JSONL never rotated.** `audit_log_max_bytes` / `audit_log_backup_count` were
consumed by the *application* log's handler instead — named for a file they did not govern.
The audit log now rotates as those settings promise, under a lock (the HTTP transport writes
from a threadpool), and `audit_log_query` streams the file in reverse rather than pulling it
entirely into memory, searching rotated backups as well as the live file.

Also: `git_pull` decodes `FetchInfo` flags and surfaces `note` instead of returning a
stringified bitmask; Woodpecker API errors carry a bounded, scrubbed response body;
`git_branch(create)` reports the unchanged `active_branch` and `git_push` reports `pushed_sha`;
the Gitea rollback branch actually deletes the release now that a delete client exists.

#### Result-shape changes

Additive except where noted — no key was removed from a success path.

| Tool | Change |
|---|---|
| `git_tag(push=True)` | success gains `flags`; **failure now returns `error` with no `pushed` key**, plus `local_tag_created` so the caller knows to clean up |
| `git_push` | gains `pushed_sha` (full sha of the pushed ref — compare against local HEAD) |
| `git_pull` | `flags` are now decoded names, not a stringified bitmask; gains `note`; **failure returns `error` with no success-shaped key** |
| `git_branch(action="create")` | gains `active_branch`. `create` still does not check out — it is `git branch`, not `git checkout -b` |
| `release` | tag-push failure returns `error` + `rolled_back` instead of proceeding |
| `audit_log_query` | gains `sources_searched` |

#### New configuration

`LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` split the application log's rotation from the audit log's.
Both default to the `AUDIT_LOG_*` values, so **no deployed env file needs to change.**

#### Operational note

Existing audit files are untouched — rotation applies going forward. Retention is now bounded
where it previously was not: at the deployed defaults (10 MB × 5) that is roughly 11 years at
the observed ~15 KB/day/agent, so it is a ceiling rather than a practical retention limit.

Five tickets closed by this batch (ids 285, 288, 189, 300, 290) plus seven findings with no
prior ticket. Four further tickets (ids 36, 38, 41, 43) were verified already fixed and closed
without work.

#### Security audit

Audited 2026-08-01 — no Critical, one High, one Low, three Info.

The **Low was fixed before merge**: `_rotate_if_needed` suppressed `OSError` at each rename
with no log line, making two failure modes invisible — an inconsistent backup chain, and (the
one that matters) a failed final rename leaving the live file to grow past
`audit_log_max_bytes` indefinitely while every subsequent write re-failed identically. Rotation
failures now log, with the persistent case at `error` rather than `warning`. Failures are still
tolerated, so a rotation problem never costs an audit entry.

The **High is not in this release** — it is a live deployment gap rather than a code defect:
the `writer` agent's audit trail is unsigned, because `~/.secrets/githost-mcp-writer.env` lacks
the `AUDIT_SIGNING_KEY` its five siblings all carry. `verify_entry_hmac` returns `True` when no
key is configured, so `audit_log_query` reports `tamper_detected: false` for entries that were
never signed — tamper detection failing open. Tracked as vikunja #301 (id 312); fix is a
one-line env addition plus a restart, owned by sysadmin.

Confirmed and not overruled: the Phase 4 Woodpecker error widening (`gitea_client.py` was
already the precedent, not the exception). Retention bounding was accepted at the defaults.

### Security — the manifest allowlist is read from a deployed copy, not a git working tree (vikunja #271, id 282)

**Deployment change only — no Python source change.** It was deliberately left untagged at the
time, since the installed package was then byte-identical to 0.9.0; it ships under 0.10.0
because that is the first release cut after it landed. It still needs saying, because it
changes where githost-mcp reads its security configuration from, and a deployment that misses
the new step behaves differently from one that does not.

When `ALLOWED_REPO_ROOTS` is unset, `_resolve_allowed_roots()` falls back to the agent
manifest. `config.py`'s default for that path is `~/.claude/manifests/<agent>-agent.yml`,
which on forge is a symlink into the `host-forge-scripts` git working tree — a tree five
agents hold `readwrite`, `git_backed` access to at the repo root. The effective allowlist was
therefore a function of whichever branch happened to be checked out: a routine `git checkout`
silently re-authorized five agents on their next restart, and any of the five could widen its
own or another agent's allowlist with an uncommitted edit no PR ever saw.

`ecosystem.config.js` now sets `AGENT_MANIFEST_PATH=/etc/forge/manifests/<agent>-agent.yml`
for every agent, pointing at a root-owned `0644` copy published from `origin/main` by
`host-forge-scripts/scripts/agent-manifests-deploy.sh`. The parent directory matters as much
as the file mode — directory write permission governs `rename` and `unlink` regardless of who
owns the file — so the target lives under `/etc` (root-owned) rather than `/opt/appdata`
(`ted`-owned).

No loader change was required: `config.py` already read `AGENT_MANIFEST_PATH` and already
overrode the default. Two regression tests were added because that guarantee rests on a single
`or` short-circuit at `config.py:157`, and the plausible refactor — treating the default as a
fallback when the explicit path is unreadable — would undo the decoupling with no visible
symptom, since the allowlist would still be populated, just from the agent-writable file again.

**Deployers must create and populate `/etc/forge/manifests` before unsetting
`ALLOWED_REPO_ROOTS` for any agent.** If the file is absent the allowlist resolves empty
(fail-closed) rather than falling back, so the agent loses all repo access.

## [0.9.0] — 2026-07-27

### Security — credential scrubbing applied to every caller-facing error return (SC-14, vikunja #36 / id 44)

Audit finding (MEDIUM) on this batch: `git_push`'s new failure return surfaced
`PushInfo.summary`, which is the remote's raw text. If a git remote carried a credential
(`https://user:token@host`), it reached the calling agent verbatim. Neither existing scrub
layer covered it — `audit.py`'s structlog `_credential_filter` and `write_audit_entry`
both operate on log/audit-JSONL output, not on a tool's return dict.

This was the **third** audit to catch this gap class, crossing the recurrence-3 threshold
into a `security-baseline` universal check, so it is closed module-wide here rather than
deferred a fourth time.

**New `security.scrub()`** = `redact_url_credentials(mask_credentials(text))`.

`mask_credentials()` alone was insufficient: it only replaces githost-mcp's *own
configured* token values, so a PAT a human embedded in a remote by hand survived it
entirely. `redact_url_credentials()` strips the userinfo component from any
scheme-qualified URL by shape. scp-style remotes (`git@github.com:owner/repo.git`) have no
scheme and stay readable — that is the form every forge remote actually uses.

Applied to all 27 caller-facing sites: `git_local.py` (12), `release.py` (5),
`woodpecker.py` (5), `registry.py` (2), `gitea.py`/`github.py`/`gitlab.py` (1 each —
upgraded from `mask_credentials` to `scrub`). `release.py`'s four `log.warning(stderr=...)`
sites, which pass twine/npm output through, are scrubbed too. No
`return {"error": str(e)}` remains anywhere in the package.

The end-to-end regression test was proven red before the fix: without the scrub, a
credential in the remote's rejection text reaches both the tool's return value **and** the
warning log — the latter confirming that the structlog filter does not catch an
unconfigured token.

### Fixed — `git_push` reported success on a rejected push (vikunja #265, id 276)

`tools/git_local.py` stringified `PushInfo.flags` (`[str(p.flags) for p in push_info]`)
and returned `{"pushed": <branch>}` unconditionally. Nothing tested the error bits, so a
non-fast-forward rejection came back as `{"pushed": "main", "flags": ["1032"]}` — where
`1032` is `ERROR (1024) | REJECTED (8)`. sysadmin only caught a live instance by
independently running `git rev-list --count @{u}..HEAD`.

**Blast radius: any past build report asserting a push landed is unverified.** This is a
correctness problem in the audit trail, not a cosmetic one.

Now:

- `PushInfo.flags` is checked against `ERROR | REJECTED | REMOTE_REJECTED | REMOTE_FAILURE`.
  Any bit set returns `{"error": ...}` — matching the module's existing failure idiom — and
  the result carries **no** `pushed` key. A result holding both would be the same bug in a
  new shape.
- Flags are decoded to names (`["REJECTED", "ERROR"]`) rather than an opaque integer, so a
  failure is diagnosable from the audit log without a bitmask lookup.
- `PushInfo.summary` — the human-readable reason, previously discarded — is surfaced in
  both `error` and `summary`.
- An empty `push_info` (remote acknowledged no ref updates) is treated as a failure.
- Upstream is set when missing, and reported as `upstream_set`. Without it a genuine
  success left the caller unable to verify: `git rev-list @{u}..HEAD` *errors* rather than
  confirming when there is no tracking branch.

Tested against a real local bare remote — asserting both that a rejection reports failure
and that a genuine push still reports success, since a filter that failed everything would
pass a one-sided test.

### Fixed — `woodpecker_trigger` returned HTTP 400 (vikunja #269, id 280)

`tools/woodpecker.py` sent `branch` as a **query parameter**; Woodpecker 3.x requires a
JSON body. The POST returned HTTP 400 with an empty body, so the tool never worked and
pipelines had to be triggered by hand.

`branch` is now sent as `json={"branch": ...}`, and omitted entirely when unset so the
repo default applies server-side rather than sending `null`. The existing unit test
asserted `request.url.params["branch"]` and had locked the wrong form in; it now asserts
the request body, the absence of the query param, and the content type.

Verified live against Woodpecker 3.x on `127.0.0.1:8100`: HTTP 200, pipeline created,
pipeline ran to `success`.

### Fixed — `woodpecker_trigger` returned an unusable pipeline handle

Found while live-verifying the above, per the plan's "check sibling calls" step.
`woodpecker_trigger` returned the pipeline's **global `id`**, but `woodpecker_status`,
`woodpecker_get_logs` and `woodpecker_pipeline_cancel` all interpolate their
`pipeline_id` argument into a path segment that Woodpecker resolves as the **per-repo
`number`**. `GET /repos/2/pipelines/26` 404s where `.../18` succeeds — so chaining
`trigger` into `status` never worked. The confusion was visible in the code: one docstring
already read "Pipeline ID or number from woodpecker_trigger".

`trigger` now returns the `number` as `pipeline_id` (the chainable handle) and keeps the
global id as `internal_id`. Sibling docstrings say "Pipeline number" explicitly. No
back-compat concern: `trigger` returned HTTP 400 for its entire life, so nothing can have
depended on its output.

### Fixed — Prometheus metrics endpoint bound `0.0.0.0` (vikunja #272, id 283)

`observability.py` called `start_http_server(config.metrics_port)` with no `addr=`.
`prometheus_client` defaults to `0.0.0.0`, making the endpoint LAN-reachable while every
other githost-mcp listener is loopback-only by design.

The 2026-07-16 stopgap was to leave `METRICS_PORT` unset fleet-wide, which left
githost-mcp with **no metrics for 11 days** — the exact capability the HTTP/PM2 migration
existed to enable.

`addr` is now `127.0.0.1`, hardcoded via `observability.METRICS_BIND_ADDR` rather than
made configurable — no deployment wants otherwise, and an env knob is how this regresses.
`ecosystem.config.js` re-enables `METRICS_PORT` from the per-agent `metricsPort` values
already declared in its `AGENTS` map (9620-9625, mirroring the 8620-8625 HTTP block).

Acceptance is `ss -tlnp` showing `127.0.0.1`, not a successful `curl` to localhost — that
confusion is how the `0.0.0.0` bind shipped in the first place.

## [0.8.0] — 2026-07-27

### Fixed — manifest-fallback allowlist ignored `access:` (M-2, vikunja#47)

`config.py:_load_manifest_roots()` selected `workspace_access` entries on
`git_backed: true` alone and never read the `access:` field, so a `readonly` entry
would have granted **full write**. Recorded as accepted risk M-2 in
`host-forge/security/accepted-risks.md` and dormant only because every agent still
has an explicit `ALLOWED_REPO_ROOTS` env var set, which takes precedence.

An entry is now included only when it is `git_backed: true` **and**
`access: readwrite`. Any other value — including a missing `access:` key, or a
near-miss like `read-only`, `rw`, or `READWRITE` — is treated as not readwrite and
fails closed, consistent with the rest of the module.

**Behavioural change, hence the minor bump:** an agent relying on the manifest
fallback with entries that lack `access: readwrite` will see a narrower allowlist
than under 0.7.0. No production agent is affected today (all six are on the env
path), but reconcile manifests before removing any `ALLOWED_REPO_ROOTS`.

`readonly` means **no githost-mcp access at all**, not read-only access:
`allowed_repo_roots` is a single list consulted by both `validate_read_path()` and
`validate_write_path()`. Splitting it into separate read and write allowlists is a
larger change that needs its own security review, and is deliberately not done here.

Skipped entries emit a `manifest_allowlist_entry_skipped` warning (path, entry path,
access value, reason) so a narrowed allowlist is diagnosable rather than mysterious.

Tests cover all four `access:`/`git_backed:` combinations, near-miss `access:` values,
mixed manifests, and — the case M-2 describes — that a `readonly` + `git_backed: true`
root is rejected by `validate_write_path()` and `validate_read_path()` both when it is
the only entry and when other readwrite roots are present.

## [0.7.0] — 2026-07-21

### Added — Tier 1 parity (GHOST-12, vikunja#40)

Closes the four Tier-1 capability gaps vs the official single-provider MCP servers via
consolidated **method-dispatch** tools (one tool + a `method` argument, per-operation
audit). Tool count 45 → 63; ~40 new operations. No existing tool was renamed.

- **PR/MR review + diff:**
  - `github_pr_review` — `get_diff` (raw unified diff via the diff media type),
    `get_files`, `get_reviews`, `submit_review` (APPROVE/REQUEST_CHANGES/COMMENT),
    `dismiss_review`. Unblocks the CodeRabbit `/pr-review` pipeline.
  - `gitea_pr_review` — `get_diff`, `get_files`, `submit_review` (APPROVE → Gitea
    APPROVED), `dismiss_review`.
  - `gitlab_mr_review` — `get_diffs`, `get_changed_files`, `approve`, `unapprove`,
    `get_approval_state`.
- **CI control:**
  - `github_actions` — `run_workflow`, `rerun_workflow`, `rerun_failed_jobs`,
    `cancel_run`, `get_run_logs` (job breakdown; GitHub raw logs are a zip archive).
  - `gitea_actions` — `list_runs`, `get_run`, `list_jobs`, `get_job_log`,
    `dispatch_workflow`, `rerun_run`, `rerun_failed_jobs`. Gitea 1.26 exposes no
    cancel-run API (only rerun/delete), so cancel is intentionally not offered.
  - `gitlab_pipeline` — `list`, `get`, `create`, `retry`, `cancel`, `get_job_log`.
  - Read-only workflow listing/status stays in `github_workflow_list`/
    `github_workflow_status`. CI secrets/variables endpoints deliberately excluded (risk).
- **Release CRUD completion:** `github_release_update`/`github_release_delete`,
  `gitea_release_update`/`gitea_release_delete`, `gitlab_release_update`/
  `gitlab_release_delete` (previously create/get/list only). update tools are partial —
  omitted fields keep current values.
- **Issues:** `{github,gitea,gitlab}_issue_read` (`get`/`list`/`comments`) and
  `{github,gitea,gitlab}_issue_write` (`create`/`update`/`add_comment`/`close`/`reopen`).
  Discrete label add/remove methods omitted — label identifiers diverge per provider
  (GitHub/GitLab names vs Gitea numeric IDs); labels are settable at create time only.
  Milestones, time-tracking, and issue-links stay out of scope (Tier 3).

### HITL gating required (companion scoped-mcp manifest change — NOT automatic)

The following (tool, method) pairs are state-changing and must be gated at the method
level in the per-agent scoped-mcp manifests, same treatment as `*_pr_merge` in v0.5.0.
Method-level gating is tracked by the companion plan
`scoped-mcp-hitl-method-aware-gating-2026-07`; until it lands, gate the whole tool.

| Tool | Destructive methods |
|------|---------------------|
| `github_pr_review` / `gitea_pr_review` | `submit_review` (event=APPROVE or REQUEST_CHANGES), `dismiss_review` |
| `gitlab_mr_review` | `approve`, `unapprove` |
| `github_actions` | `run_workflow`, `rerun_workflow`, `rerun_failed_jobs`, `cancel_run` |
| `gitea_actions` | `dispatch_workflow`, `rerun_run`, `rerun_failed_jobs` |
| `gitlab_pipeline` | `create`, `retry`, `cancel` |
| `github_release_delete` / `gitea_release_delete` / `gitlab_release_delete` | (whole tool) |
| `github_issue_write` / `gitea_issue_write` / `gitlab_issue_write` | `close` (and `create`/`update`/`add_comment` at operator discretion) |

### Security

- Audit `githost-mcp-tier1-parity-2026-07` (1 Medium, 1 Low, 3 Info; full report:
  `host-forge/build-reports/githost-mcp-tier1-parity-2026-07/audit.md`). Both findings
  remediated before merge:
  - **Medium (SC-14 / GHOST-8):** `gitea.py` now routes every exception return through a
    `mask_credentials` `_err()` helper, matching `github.py`/`gitlab.py`. Closes the
    long-open GHOST-8 for all Gitea tools (the audit log was already scrubbed
    independently; this covers the direct tool-return value). Restores the build plan's
    credential-isolation invariant across all three providers.
  - **Low:** added the missing unsafe-tag rejection test for `gitea_release_update`.
- Pre-audit baseline also fixed IV-01 (unvalidated `tag`/`workflow` interpolated into raw
  Gitea httpx paths — now guarded by `_bad_tag`/`_WORKFLOW_RE`).

### Fixed

- `__init__.py` `__version__` was stuck at `0.5.0` while `pyproject.toml` had moved to
  `0.6.0`; both now agree at `0.7.0`.

### Notes

- No Woodpecker work — githost already exceeds the only real Woodpecker MCP (6 read-only
  tools) with its existing trigger + cancel.
- No new required env vars. Existing per-provider tokens must carry the added scopes:
  `GITHUB_TOKEN` (repo + workflow), `GITEA_TOKEN` (write:repository, write:issue),
  `GITLAB_TOKEN` (api).
- GitLab tools are unit-tested with mocks but not live-smoke-tested (forge runs no GitLab).

## [0.6.0] — 2026-07-16

### Added
- Env-selectable transport: `TRANSPORT=stdio|http` (default `stdio`, unchanged behavior). `http`
  mode runs `mcp.run(transport="http", host=HTTP_HOST, port=HTTP_PORT)` as a long-lived process —
  fixes Prometheus/OTEL/Loki/NATS observability, which rarely survived the per-turn stdio recycle
  scoped-mcp does today — and lets githost-mcp restart independently of scoped-mcp.
  (GHOST-13, Phase 1 of 2; Phase 2 — sysadmin PM2 cutover — is a separate follow-up build.)
- HTTP transport hard requirements, both fail-closed in `server.py::main()`, not just documented:
  - Refuses to bind any `HTTP_HOST` other than `127.0.0.1`/`localhost`/`::1` unless
    `GITHOST_MCP_ALLOW_NONLOOPBACK=1` is set explicitly.
  - Refuses to start `TRANSPORT=http` at all unless `GITHOST_MCP_AUTH_TOKEN` is set — FastMCP's
    built-in `StaticTokenVerifier` then rejects any request missing a matching
    `Authorization: Bearer <token>` header (401). The token is included in the existing
    credential filter (audit JSONL + structlog), so it is never written to logs.
- Real per-agent `ecosystem.config.js`: one PM2 app per agent (developer, sysadmin, security,
  writer, research, harlock — 6, corrected from the original build plan's list of 5 during
  `shared-build-review`), generated from a single `AGENT_ID -> {httpPort, metricsPort}` map and
  reusing the same per-agent secrets files the stdio launchers already read.
- README "Deploy" section (stdio vs http tradeoffs, PM2 usage) and a new Security Model
  subsection on the HTTP transport surface; `AGENTS.md` and `.env.example` updated to match.

### Security
- Audit `githost-mcp-http-pm2-migration-2026-07` (2 Low, 2 Info; full report:
  `host-forge/build-reports/githost-mcp-http-pm2-migration-2026-07/audit.md`). Both Low findings
  remediated before merge:
  - `main()` now also refuses to start `TRANSPORT=http` if `GITHOST_MCP_AUTH_TOKEN` is shorter
    than 16 characters — the credential filter only redacts tokens over 4 characters, so a
    shorter token could have appeared in cleartext in logs/audit trail despite the README's
    claim otherwise.
  - Bumped `starlette` (>=1.3.1, fixes PYSEC-2026-248/249), `mcp` (>=1.28.1, fixes
    CVE-2026-59950), and `cryptography` (>=48.0.1, fixes GHSA-537c-gmf6-5ccf) — none of these
    were reachable through githost-mcp's actual routes, but this is the first build where the
    dependency chain backs a real network listener instead of stdio.

## [0.5.1] — 2026-07-16

### Fixed
- `security.clean_env()`: new shared helper strips `NODE_CHANNEL_FD`, `NODE_CHANNEL_SERIALIZATION_MODE`,
  and `NODE_UNIQUE_ID` from subprocess environments before spawning `npm`, `twine`, or `python -m build`
  children. PM2 injects these for its own IPC channel; leaking them into a spawned Node.js child causes
  a SIGABRT during teardown. Applied to all 4 env-construction sites (`registry.py` twine/npm upload envs,
  `release.py` twine/npm upload envs) and the 2 bare `subprocess.run` calls that previously passed no
  `env=` at all (`registry.py` build and twine-check). (GHOST-11, sibling to HLOPS-1)

## [0.5.0] — 2026-07-13

### Added
- `LICENSE` (MIT) and the repo-standards Baseline README badges (Built with Claude Code, License: MIT).
- GitHub Actions CI (`.github/workflows/ci.yml`): ruff lint + format check, pytest with
  coverage, and `pip-audit --strict`, across Python 3.11/3.12/3.13, with every action pinned
  to a commit SHA. Replaces the non-functional Woodpecker config, which was never wired to the
  GitHub repo (GHOST-10).
- `[tool.ruff]` / `[tool.ruff.lint]` and `[tool.coverage]` configuration in `pyproject.toml`;
  `ruff` and `pip-audit` added to the `dev` extra.
- Two Mermaid architecture diagrams in the README (provider-dispatch → audit-log flow, and the
  `ALLOWED_REPO_ROOTS` env-vs-manifest resolution flow).
- GitHub and GitLab PR/MR write tooling, for provider parity with Gitea (39 → 45 tools):
  `github_pr_create`, `github_pr_get`, `github_pr_merge`, `gitlab_mr_create`, `gitlab_mr_get`,
  `gitlab_mr_merge`. Each mirrors the existing `gitea_pr_*` shape, records an `AuditCtx` entry,
  and routes errors through the `mask_credentials` wrapper. This closes the asymmetry that
  forced the prior build to route around githost-mcp when opening its own GitHub PR.
  `github_pr_merge` and `gitlab_mr_merge` are DESTRUCTIVE and need HITL gating in scoped-mcp
  manifests (same treatment as `gitea_pr_merge`) — a companion manifest change tracked
  separately, not automatic on tool registration.

### Fixed
- `pypi_publish` / `npm_publish`: a failing `twine check` was captured but its result never
  inspected, so a distribution that failed metadata/render validation could still proceed to
  upload. The check now aborts the publish on non-zero exit, matching the adjacent build and
  upload checks.

### Changed
- Applied `ruff format` across the codebase and resolved all `ruff check` findings (import
  ordering, PEP 604 optionals, exception chaining, `contextlib.suppress`, unused bindings).
- Corrected the README tool inventory to 39 tools (was mislabeled 32); Gitea (4→8) and
  Woodpecker (2→5) counts were stale.
- Test coverage raised 64% → 84%; the repo-standards 80% floor is now enforced via
  `[tool.coverage.report] fail_under = 80` and the CI pytest step. Newly covered:
  `tools/github.py` 41→100%, `tools/gitlab.py` 48→100%, both provider clients →100%,
  `tools/woodpecker.py` 65→96%, `server.py` 0→97%, `observability.py` 20→74% (OTEL-endpoint
  init path deferred to the `[otel]` extra's own suite).

### Removed
- `.woodpecker.yml` — non-functional CI config, superseded by GitHub Actions.

### Security
- Audited 2026-07-13 (`githost-mcp-baseline-and-ci-2026-07`): 1 high, 1 low, 3 info. Repo-level
  checks (credential masking, audit logging, baseline/CI compliance) all passed; the new
  GitHub/GitLab tools mask credentials on every error path and each records an audit entry.
- IV-01 (low, fixed): `repo` (GitHub) and `project` (GitLab) arguments are now validated before
  reaching the client library, across all 17 tools in `tools/github.py` and `tools/gitlab.py` —
  matching the guard `tools/gitea.py`/`tools/woodpecker.py` already apply. GitHub uses the
  strict `owner/repo` grammar; GitLab accepts nested group paths and bare numeric project IDs.
- The high finding is a scoped-mcp manifest gap, out of this repo's scope: the new destructive
  merge tools (`github_pr_merge`, `gitlab_mr_merge`) were live and ungated in all six agent
  manifests (denylist-based access model), not "inert until granted." Remediated via manifest
  change, tracked in Plane (SMCP) and an urgent sysadmin task — no githost-mcp code change.

## [0.4.0] — 2026-07-13

### Added
- Manifest-aware fallback for `ALLOWED_REPO_ROOTS`: when the env var is unset or empty,
  the allowlist now resolves from the requesting agent's manifest
  (`AGENT_MANIFEST_PATH`, default `~/.claude/manifests/{AGENT_ID}-agent.yml`), using
  `git_backed: true` entries from `workspace_access`. Explicit `ALLOWED_REPO_ROOTS`
  always wins when set — no behavior change for any existing deployment. Root-cause fix
  for GHOST-7, GHOST-5, GHOST-2 (allowlist drift).
- `pytest-cov` wired into CI (`.woodpecker.yml`) with a `--cov-fail-under=60` gate.

### Fixed
- `audit_log_query(since=...)` raised an uncaught `TypeError` (naive vs. aware datetime
  comparison) whenever `since` was passed without a timezone suffix — exactly the format
  shown in its own docstring example (`'2026-05-20'`).

### Changed
- Test coverage: `tools/registry.py` 0% → 96%, `tools/audit_query.py` 0% → 97%,
  `tools/release.py` 22% → 69%. Overall coverage 49% → 64%.

### Docs
- `AGENTS.md`: fixed `ALLOWED_REPO_ROOTS` colon/comma mismatch; documented the new
  `AGENT_MANIFEST_PATH` fallback.

### Security
- Audited 2026-07-13 (`githost-mcp-allowlist-and-coverage-2026-07`): 0 critical/high, 2
  medium, 1 low, 2 info. M-2 (manifest fallback doesn't honor `access: readonly`, dormant
  until manifest reconciliation) accepted as known risk — see
  `host-forge/security/accepted-risks.md`. M-1 (credential-masking asymmetry, pre-existing)
  and L-1 (transitive dependency CVEs) filed as follow-up tickets (GHOST-8, GHOST-9).

## [0.3.0] — 2026-06-01

### Added
- Per-agent git committer identity: `GIT_AGENT_NAME` and `GIT_AGENT_EMAIL` env vars set the
  git author and committer fields on every `git_commit` call. When unset, defaults are derived
  from `AGENT_ID` (`{id}-agent` / `{id}@forge`). Enables `git log --author` filtering by agent.
- `validate_read_path` in `security.py`: read tools (`git_status`, `git_diff`, `git_log`,
  `git_show`, `git_branch list`) now enforce the same `ALLOWED_REPO_ROOTS` allowlist as write
  tools, preventing unrestricted filesystem reads.
- `git_log` limit capped at 200 (consistent with existing list tool caps).

### Security
- Read tools previously bypassed `ALLOWED_REPO_ROOTS` entirely; an agent could call them on
  any path on the filesystem. All five read tools now call `validate_read_path` (same allowlist
  logic as write path, distinct error message).

## [0.2.1] — 2026-05-31

### Fixed
- All 5 Woodpecker tools now resolve `owner/name` to a numeric repo ID via
  `GET /api/repos/lookup/{owner}/{name}` before constructing any pipeline URL.
  Woodpecker 3.x removed name-based repo routing; all pipeline endpoints now
  require a numeric ID. Previously, name-based paths returned the SPA frontend
  (HTTP 200, HTML body), causing a JSON parse error in every Woodpecker tool.
- 404 from the lookup endpoint returns a clear "not found in Woodpecker" error
  rather than an uncaught exception.

## [0.2.0] — 2026-05-31

### Added
- `woodpecker_list_pipelines` — list recent pipeline runs with optional status filter; limit capped at 100
- `woodpecker_get_logs` — fetch step output by name or first step; truncated at 500 lines; log content excluded from audit trail
- `woodpecker_pipeline_cancel` — cancel a running pipeline; HITL gated in scoped-mcp manifests
- `gitea_pr_create` — open a pull request from a feature branch
- `gitea_pr_get` — get PR details including mergeable status and labels
- `gitea_pr_comment` — post a comment on a PR (via Gitea issues endpoint)
- `gitea_pr_merge` — merge a PR with style (merge/squash/rebase); HITL gated; validates merge_style enum
- `gitea_post_void` internal helper for 204 No Content responses (Gitea merge endpoint)
- `.woodpecker.yml` CI pipeline: test + pip-audit steps

### Security
- `repo` arg now validated against `^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$` in all 9 Woodpecker/Gitea tools (IV-01)
- `limit` param capped at 100 in all list tools to prevent unbounded memory allocation (I-03)
- `step_id` cast to `int()` before URL construction in `woodpecker_get_logs` (I-04)
- `gitea_pr_merge` and `woodpecker_pipeline_cancel` HITL gated in sysadmin and developer scoped-mcp manifests
- Research agent manifest denylists both destructive tools

## [0.1.0] — 2026-05-27

### Added
- 32 tools across 8 categories: local git (11), GitHub (7), Gitea (4), GitLab (4),
  release orchestration (1), registry (2), Woodpecker CI (2), audit query (1)
- Per-agent JSONL audit trail with HMAC-SHA256 tamper-evidence for every tool call
- `ALLOWED_REPO_ROOTS` allowlist enforced on all write operations; fails closed when unset
- Credential filter processor via structlog — tokens scrubbed from all log output, JSONL
  entries, OTEL span attributes, and tool return values
- 401/403 sanitization in all three provider clients (GitHub, Gitea, GitLab)
- Coordinated multi-target release tool (`release`) with rollback: git tag → GitHub/Gitea/
  GitLab release → PyPI → npm; rollback deletes remote releases on downstream failure
- `pypi_publish` and `npm_publish` — tokens injected via `env=` kwarg, never in CLI args
- OTEL traces, Prometheus metrics, Loki push, NATS publisher — all opt-in via env vars
- FastMCP stdio transport; runs as a subprocess via scoped-mcp on forge

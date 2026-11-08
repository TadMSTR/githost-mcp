// PM2 ecosystem — one long-lived HTTP-transport githost-mcp process per agent.
//
// Replaces the per-turn-recycled stdio launchers (run-githost-mcp-<agent>.py)
// with real PM2 services so OTel exporters survive across calls and
// `pm2 restart githost-mcp-<agent>` applies a code change without bouncing
// scoped-mcp. See README.md "Deploy" for the full stdio-vs-http cutover.
//
// Single source of truth is the AGENTS map below (AGENT_ID -> port) — add a
// row here to onboard a new agent, no separate generator script needed since
// PM2 ecosystem files are plain Node and this one builds its own `apps` array.
//
// Secrets are NOT hardcoded here: each app reuses the same two files the
// stdio launchers already read — ~/.secrets/forge.env (shared GITHUB_TOKEN /
// GITEA_TOKEN / GITLAB_TOKEN / GITHOST_MCP_AUTH_TOKEN) and
// ~/.secrets/githost-mcp-<agent>.env (GITEA_URL / GITHUB_URL, GIT_AGENT_NAME /
// GIT_AGENT_EMAIL, AUDIT_SIGNING_KEY, LOG_FILE / AUDIT_LOG_FILE /
// AUDIT_LOG_MAX_BYTES / AUDIT_LOG_BACKUP_COUNT / LOG_LEVEL, WOODPECKER_URL;
// ALLOWED_REPO_ROOTS only where an agent still overrides the manifest
// allowlist).
//
// GITHOST_MCP_AUTH_TOKEN is fleet-wide, not per-agent: buildApp() below reads
// it from the per-agent file first but then applies the shared forge.env value
// unconditionally, overwriting anything set there. A per-agent value in
// githost-mcp-<agent>.env is read and silently discarded — see vikunja #408
// for the tracked fix (would need new per-agent secrets, `headers:` blocks in
// six manifests, and a coordinated restart of the githost-mcp and scoped-mcp
// processes).
//
// AGENT_ID does NOT come from that file and never has — buildApp() below injects
// it, along with TRANSPORT, HTTP_HOST, HTTP_PORT and AGENT_MANIFEST_PATH (and, before
// 0.19.0, METRICS_PORT). Taking the old wording at face value cost a cutover: an
// audit-LOW sanity check in agent-allowlist-cutover.sh asserted the rewritten
// env file still carried AGENT_ID, an invariant that had never been true, and
// the cutover failed closed for every agent on its first real run (fixed in
// host-forge/scripts 01ae354; vikunja #279, id 290).
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function parseEnvFile(filePath) {
  const env = {};
  if (!fs.existsSync(filePath)) return env;
  for (const line of fs.readFileSync(filePath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const idx = trimmed.indexOf("=");
    env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
  }
  return env;
}

const HOME = os.homedir();
const SHARED_TOKEN_KEYS = ["GITHUB_TOKEN", "GITEA_TOKEN", "GITLAB_TOKEN"];
const sharedEnv = parseEnvFile(path.join(HOME, ".secrets", "forge.env"));

// AGENT_ID -> per-agent HTTP port. Contiguous range per the build plan
// (githost-mcp-http-pm2-migration-2026-07); keep in sync with
// host-forge/services.md when adding or renumbering agents.
// 8625 and 8627 stay reserved: they were harlock's and steward's, both decommissioned
// (vikunja#367). Do not reuse them for a new agent; a stale PM2 entry or firewall note
// may still name them. 8626 is backrest-mcp.
const AGENTS = {
  developer: { httpPort: 8620 },
  sysadmin: { httpPort: 8621 },
  security: { httpPort: 8622 },
  writer: { httpPort: 8623 },
  research: { httpPort: 8624 },
  fleet: { httpPort: 8628 },
};
function buildApp(agentId, ports) {
  const env = parseEnvFile(path.join(HOME, ".secrets", `githost-mcp-${agentId}.env`));

  for (const key of SHARED_TOKEN_KEYS) {
    if (sharedEnv[key]) env[key] = sharedEnv[key];
  }
  if (sharedEnv.GITHOST_MCP_AUTH_TOKEN) {
    env.GITHOST_MCP_AUTH_TOKEN = sharedEnv.GITHOST_MCP_AUTH_TOKEN;
  }

  env.AGENT_ID = agentId;
  env.TRANSPORT = "http";
  env.HTTP_HOST = "127.0.0.1";
  env.HTTP_PORT = String(ports.httpPort);

  // Read the manifest allowlist from the deployed, root-owned copy rather than
  // config.py's default of ~/.claude/manifests/<agent>-agent.yml — which is a
  // symlink into the host-forge-scripts *working tree* that five agents hold
  // readwrite, git_backed access to. Without this, a routine `git checkout` in
  // that shared tree silently changes five agents' effective allowlist on their
  // next restart, and an uncommitted edit bypasses PR review entirely.
  //
  // Published by host-forge-scripts/scripts/agent-manifests-deploy.sh, which
  // reads origin/main as git objects and installs root:root 0644 under /etc —
  // root-owned and not ted-writable, so the file cannot be renamed out from
  // under us the way it could beneath /opt/appdata.
  //
  // Set for every agent, so no process retains the working-tree fallback path. This
  // does NOT cut an agent over on its own: ALLOWED_REPO_ROOTS still wins wherever it is
  // set (_resolve_allowed_roots returns early on an explicit value). If the file is
  // missing or unparseable the allowlist resolves empty — fail closed, not fall back.
  //
  // vikunja#271; closes vikunja#47 and accepted risk M-2.
  env.AGENT_MANIFEST_PATH = `/etc/forge/manifests/${agentId}-agent.yml`;
  // No METRICS_PORT: the Prometheus endpoint was removed in 0.19.0. No job scraped
  // 9620-9628, and the OTel metrics below already reach SigNoz (githost-scoped-2026-11
  // p2). A running process started from the old file keeps METRICS_PORT in its PM2 env
  // across restarts; 0.19.0 ignores it and logs config_ignored_removed_backend once.

  // OTLP export to the SigNoz collector (signoz-otel-collector, 4317 gRPC /
  // 4318 HTTP). Until this was set, githost-mcp's telemetry reached nothing:
  // _init_otel() returns immediately when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
  // This is now githost-mcp's only telemetry export.
  //
  // The protocol is set EXPLICITLY even though "grpc" is already config.py's
  // default. An exporter that is correct only because a variable is unset is the
  // vikunja#865 failure mode: pair :4318 with the grpc default and the gRPC
  // exporter talks to an HTTP port, fails, and says nothing at all. Naming both
  // halves here makes the pairing checkable by reading one place, and survives a
  // future change to that default.
  env.OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4317";
  env.OTEL_EXPORTER_OTLP_PROTOCOL = "grpc";
  // Per-agent, or every process registers as one service named "githost-mcp"
  // (config.py's default) and collapse into a single row in SigNoz — which would
  // make "which agent hit this limit" unanswerable from traces, the one question
  // this telemetry exists to answer. Matches the scoped-mcp-<agent> convention
  // already visible in list_services.
  env.OTEL_SERVICE_NAME = `githost-mcp-${agentId}`;

  return {
    name: `githost-mcp-${agentId}`,
    script: "/opt/venvs/githost-mcp/bin/python3",
    args: "-m githost_mcp.server",
    interpreter: "none",
    env,
  };
}

module.exports = {
  apps: Object.entries(AGENTS).map(([agentId, ports]) => buildApp(agentId, ports)),
};

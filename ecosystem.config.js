// PM2 ecosystem — one long-lived HTTP-transport githost-mcp process per agent.
//
// Replaces the per-turn-recycled stdio launchers (run-githost-mcp-<agent>.py)
// with real PM2 services so Prometheus/OTEL/Loki/NATS survive across calls and
// `pm2 restart githost-mcp-<agent>` applies a code change without bouncing
// scoped-mcp. See README.md "Deploy" for the full stdio-vs-http cutover.
//
// Single source of truth is the AGENTS map below (AGENT_ID -> ports) — add a
// row here to onboard a new agent, no separate generator script needed since
// PM2 ecosystem files are plain Node and this one builds its own `apps` array.
//
// Secrets are NOT hardcoded here: each app reuses the same two files the
// stdio launchers already read — ~/.secrets/forge.env (shared GITHUB_TOKEN /
// GITEA_TOKEN / GITLAB_TOKEN / GITHOST_MCP_AUTH_TOKEN) and
// ~/.secrets/githost-mcp-<agent>.env (AGENT_ID, ALLOWED_REPO_ROOTS, and any
// agent-specific token overrides).
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

// AGENT_ID -> per-agent port block. Contiguous ranges per the build plan
// (githost-mcp-http-pm2-migration-2026-07); keep in sync with
// host-forge/services.md when adding or renumbering agents.
const AGENTS = {
  developer: { httpPort: 8620, metricsPort: 9620 },
  sysadmin: { httpPort: 8621, metricsPort: 9621 },
  security: { httpPort: 8622, metricsPort: 9622 },
  writer: { httpPort: 8623, metricsPort: 9623 },
  research: { httpPort: 8624, metricsPort: 9624 },
  harlock: { httpPort: 8625, metricsPort: 9625 },
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
  // Set for every agent, including harlock, so no process retains the
  // working-tree fallback path. This does NOT cut an agent over on its own:
  // ALLOWED_REPO_ROOTS still wins wherever it is set (_resolve_allowed_roots
  // returns early on an explicit value), and harlock deliberately keeps its env
  // because its manifest has zero git_backed readwrite entries (vikunja #270,
  // id 281). If the file is missing or unparseable the allowlist resolves empty
  // — fail closed, not fall back.
  //
  // vikunja #271 (id 282); closes #47 (id 55) and accepted risk M-2.
  env.AGENT_MANIFEST_PATH = `/etc/forge/manifests/${agentId}-agent.yml`;
  // Re-enabled 2026-07-27 (vikunja #272, id 283). The 2026-07-16 stopgap left this
  // unset because observability.py called start_http_server() without addr= and so
  // bound 0.0.0.0; it now passes addr="127.0.0.1" unconditionally. Verify with
  // `ss -tlnp` — a metrics endpoint answering on localhost proves nothing about
  // which interface it is on, and that confusion is how the 0.0.0.0 bind shipped.
  env.METRICS_PORT = String(ports.metricsPort);

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

// PM2 config for reference — githost-mcp is deployed as a subprocess, not a PM2 service.
// Each agent launcher (run-githost-mcp.sh) starts this directly via scoped-mcp.
// See .env.example for required environment variables.
module.exports = {
  apps: [
    {
      name: "githost-mcp",
      script: "/opt/agents/dev/venv/bin/python3",
      args: "-m githost_mcp.server",
      interpreter: "none",
      env: {
        AGENT_ID: "dev",
        LOG_LEVEL: "INFO",
        LOG_FILE: "/opt/appdata/githost-mcp/logs/githost-mcp.log",
        AUDIT_LOG_FILE: "/opt/appdata/githost-mcp/audit/githost.jsonl",
      },
    },
  ],
};

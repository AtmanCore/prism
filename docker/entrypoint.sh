#!/usr/bin/env bash
# Container start: check credentials, point git at the GitHub token, then run the service.
set -euo pipefail
cd /app

if [[ -d config.json ]]; then
  echo "config.json is a folder: Docker created it because the file didn't exist on the host." >&2
  echo "Fix: on the host, run 'rmdir config.json && cp config.example.json config.json', then start again." >&2
  exit 1
fi

# GitHub: the service and gh read GITHUB_TOKEN (gh also accepts GH_TOKEN).
export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [[ -z "$GH_TOKEN" ]]; then
  echo "WARNING: GITHUB_TOKEN is empty in .env. The container can't use your host's 'gh auth' login," >&2
  echo "         so syncing PRs will fail. Run ./setup.sh github on the host (or see docs/setup.md)." >&2
else
  # Clone over HTTPS with the token instead of SSH keys (git@github.com:x -> https://github.com/x).
  git config --global url."https://github.com/".insteadOf "git@github.com:"
  gh auth setup-git >/dev/null 2>&1 || echo "WARNING: 'gh auth setup-git' failed; cloning may fail." >&2
fi

# Claude: your subscription via a token from 'claude setup-token', or an API key.
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" && ! -f "$HOME/.claude/.credentials.json" ]]; then
  echo "WARNING: no Claude credentials. On your host run ./setup.sh claude (or 'claude setup-token' and put" >&2
  echo "         the token in .env as CLAUDE_CODE_OAUTH_TOKEN=...). Reviews fail until then." >&2
fi

# Jira / Confluence: each is enabled only when its URL, username and token are all set. The bundled
# MCP server runs read-only with just the tools reviews need. Complete credentials aren't passed with
# -e: the server inherits them from the container environment (.env), so they aren't written to disk.
# With neither configured, reviews use the PR description.
complete() { [[ -n "${!1:-}" && -n "${!2:-}" && -n "${!3:-}" ]]; }
partial()  { [[ -n "${!1:-}${!2:-}${!3:-}" ]]; }
TOOLS=() MCP_ENV=(-e READ_ONLY_MODE=true)
if complete JIRA_URL JIRA_USERNAME JIRA_API_TOKEN; then
  TOOLS+=(jira_get_issue jira_search)
else
  partial JIRA_URL JIRA_USERNAME JIRA_API_TOKEN && echo "WARNING: Jira is half configured (need JIRA_URL, JIRA_USERNAME and JIRA_API_TOKEN). Skipping Jira." >&2
  MCP_ENV+=(-e JIRA_URL= -e JIRA_USERNAME= -e JIRA_API_TOKEN=)
fi
if complete CONFLUENCE_URL CONFLUENCE_USERNAME CONFLUENCE_API_TOKEN; then
  TOOLS+=(confluence_get_page confluence_search confluence_get_page_children)
else
  partial CONFLUENCE_URL CONFLUENCE_USERNAME CONFLUENCE_API_TOKEN && echo "WARNING: Confluence is half configured (need CONFLUENCE_URL, CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN). Skipping Confluence." >&2
  MCP_ENV+=(-e CONFLUENCE_URL= -e CONFLUENCE_USERNAME= -e CONFLUENCE_API_TOKEN=)
fi
claude mcp remove --scope user atlassian >/dev/null 2>&1 || true
if (( ${#TOOLS[@]} )); then
  if [[ -x /opt/mcp-atlassian/bin/mcp-atlassian ]]; then
    ENABLED=$(IFS=,; echo "${TOOLS[*]}")
    claude mcp add --scope user atlassian "${MCP_ENV[@]}" -e ENABLED_TOOLS="$ENABLED" \
      -- /opt/mcp-atlassian/bin/mcp-atlassian >/dev/null
    export PRISM_EXTRA_READ_TOOLS
    PRISM_EXTRA_READ_TOOLS=$(printf 'mcp__atlassian__%s,' "${TOOLS[@]}"); PRISM_EXTRA_READ_TOOLS=${PRISM_EXTRA_READ_TOOLS%,}
    echo "Jira/Confluence: read-only tools enabled ($ENABLED)."
  else
    echo "WARNING: Jira/Confluence settings found, but the image was built without mcp-atlassian. Skipping." >&2
  fi
else
  echo "Jira/Confluence: not configured. Reviews use the PR title and description as the spec."
fi

exec python3 server.py

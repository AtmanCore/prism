# PRism in a container: Python (the service), git + GitHub CLI (PRs), Node + Claude Code (reviews).
# Build: docker compose build     Run: docker compose up -d     UI: http://localhost:6161
FROM node:22-bookworm-slim

# Pin Claude Code for reproducible builds, e.g. --build-arg CLAUDE_CODE_VERSION=2.1.0
ARG CLAUDE_CODE_VERSION=latest
# Lint tools your repos use, so reviews can run them, e.g. --build-arg EXTRA_APT_PACKAGES="shellcheck"
ARG EXTRA_APT_PACKAGES=""
# Jira + Confluence for reviews (community server github.com/sooperset/mcp-atlassian), run read-only.
# Set to "" to leave it out.
ARG MCP_ATLASSIAN_VERSION=0.23.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv git ca-certificates curl gnupg make ripgrep jq openssh-client tini ${EXTRA_APT_PACKAGES} \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && if [ -n "${MCP_ATLASSIAN_VERSION}" ]; then \
      python3 -m venv /opt/mcp-atlassian \
      && /opt/mcp-atlassian/bin/pip install --no-cache-dir "mcp-atlassian==${MCP_ATLASSIAN_VERSION}"; \
    fi

# Run as an unprivileged "prism" user (the base image's uid 1000 user, renamed).
RUN groupmod -n prism node && usermod -l prism -d /home/prism -m node \
 && mkdir -p /app/data /app/workspace /home/prism/.claude /home/prism/kb \
 && chown -R prism:prism /app /home/prism

WORKDIR /app
COPY --chown=prism:prism . /app
RUN chmod +x /app/docker/entrypoint.sh

ENV HOME=/home/prism \
    PRISM_BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1

USER prism
EXPOSE 6161
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/api/state' % (os.environ.get('PR_REVIEW_PORT') or 6161), timeout=4)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

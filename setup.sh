#!/usr/bin/env bash
# Get the GitHub, Claude, Jira and Confluence credentials into .env (gitignored), with browser help.
#   ./setup.sh            everything      ./setup.sh github|claude|atlassian|kb    one part
#   ./setup.sh --check    test what's already in .env, change nothing
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 is needed for setup (macOS and most Linux have it)."; exit 1; }
exec python3 tools/setup.py "$@"

#!/usr/bin/env bash
# Start the PRism service (idempotent) and open the UI. Uses launchd if autostart is installed.
set -euo pipefail
cd "$(dirname "$0")"
LABEL=local.pr-review
PORT=$(grep -E '^PR_REVIEW_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d "'\"" || true)
PORT=${PORT:-6161}
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ -f service.pid ]] && kill -0 "$(cat service.pid)" 2>/dev/null; then
  echo "Already running (pid $(cat service.pid)) at http://localhost:$PORT"
elif [[ -f "$PLIST" ]]; then
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl kickstart "gui/$(id -u)/$LABEL"
  sleep 2
  echo "Started via launchd (pid $(cat service.pid 2>/dev/null || echo '?')) at http://localhost:$PORT, logs in service.log"
else
  nohup ./run.sh >> service.log 2>&1 &
  sleep 1
  if kill -0 "$(cat service.pid)" 2>/dev/null; then
    echo "Started (pid $(cat service.pid)) at http://localhost:$PORT, logs in service.log"
  else
    echo "Failed to start. Last log lines:"; tail -20 service.log; exit 1
  fi
fi
URL="http://localhost:$PORT"
if [[ "${1:-}" != "--no-open" ]]; then
  if command -v open >/dev/null 2>&1 && [[ "$(uname)" == "Darwin" ]]; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
  else echo "Open $URL in your browser."
  fi
fi

#!/usr/bin/env bash
# Restart without losing work: stop starting new reviews, wait for running ones to finish, then restart.
set -euo pipefail
cd "$(dirname "$0")"
PORT=$(grep -E '^PR_REVIEW_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d "'\"" || true)
PORT=${PORT:-6161}
if ! { [[ -f service.pid ]] && kill -0 "$(cat service.pid)" 2>/dev/null; }; then
  exec ./start.sh --no-open
fi
touch service.drain
echo "Draining: no new reviews will start."
busy() {
  curl -fsS --max-time 30 "127.0.0.1:$PORT/api/state" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(p.get('review_status') in ('preparing','kb-refresh','reviewing') for p in d['prs']) + d['meta'].get('busy_jobs', 0))"
}
fails=0
while true; do
  if ! n=$(busy 2>/dev/null); then
    fails=$((fails + 1))
    if (( fails >= 6 )); then
      echo "Couldn't ask the service how many reviews are running (6 tries). Not restarting; run ./restart.sh again." >&2
      rm -f service.drain; exit 1
    fi
    sleep 5; continue
  fi
  fails=0
  [[ "$n" -gt 0 ]] || break
  echo "  $n review(s) or other background job(s) still running…"; sleep 20
done
if [[ "${1:-}" == "--stop-only" ]]; then
  ./stop.sh; rm -f service.drain; exit 0
fi
LABEL=local.pr-review
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # the drain flag is cleared by the server itself on startup
  launchctl kickstart -k "gui/$(id -u)/$LABEL" && sleep 2 && echo "Restarted via launchd (pid $(cat service.pid))."
else
  ./stop.sh
  rm -f service.drain
  ./start.sh --no-open
fi

#!/usr/bin/env bash
# Stop the service (in-flight reviews are re-queued on the next start).
# With autostart installed this unloads it until ./start.sh or the next login.
cd "$(dirname "$0")"
LABEL=local.pr-review
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" && rm -f service.pid && echo "Stopped (launchd)."
elif [[ -f service.pid ]] && kill -0 "$(cat service.pid)" 2>/dev/null; then
  kill "$(cat service.pid)" && rm -f service.pid && echo "Stopped."
else
  rm -f service.pid; echo "Not running."
fi

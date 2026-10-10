#!/usr/bin/env bash
# Remove the LaunchAgent (stops the service). ./start.sh still works by hand afterwards.
cd "$(dirname "$0")"
LABEL=local.pr-review
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Stopped and unloaded." || echo "Not loaded."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" service.pid

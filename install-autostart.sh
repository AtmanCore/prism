#!/usr/bin/env bash
# Install a launchd LaunchAgent: start the service at login and restart it if it crashes.
set -euo pipefail
cd "$(dirname "$0")"
DIR=$(pwd)
LABEL=local.pr-review
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY=$(python3 -c 'import sys; print(sys.executable)')
# launchd starts with a bare PATH; give it everything the reviews shell out to.
PATH_VAL="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$HOME/.pyenv/shims:$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Drain and stop a copy started by hand (nohup) so the two don't fight over the port.
if [[ -f service.pid ]] && kill -0 "$(cat service.pid)" 2>/dev/null && ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  ./restart.sh --stop-only
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/run.sh</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH_VAL</string>
    <key>PR_REVIEW_PYTHON</key><string>$PY</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR/service.log</string>
  <key>StandardErrorPath</key><string>$DIR/service.log</string>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $PLIST. The service now starts at login and restarts if it crashes."

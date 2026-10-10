#!/usr/bin/env bash
cd "$(dirname "$0")"
if [[ -f service.pid ]] && kill -0 "$(cat service.pid)" 2>/dev/null; then
  echo "Running (pid $(cat service.pid))"; tail -5 service.log
else
  echo "Not running. Start with ./start.sh"
fi

#!/usr/bin/env bash
# Foreground entry point (used by launchd). Records the pid so status.sh works either way.
cd "$(dirname "$0")"
echo $$ > service.pid
exec "${PR_REVIEW_PYTHON:-python3}" server.py

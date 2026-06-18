#!/bin/sh
set -eu

DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
STATE_FILE="${KIJITOMON_STATE_FILE:-$HOME/.cache/kijito-monitor/hive.json}"

exec python3 -u "$DIR/kijito_monitor.py" --state-file "$STATE_FILE" "$@"

#!/bin/sh
set -eu

DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
STATE_FILE="${KIJITOMON_STATE_FILE:-$HOME/.cache/kijito-inbox-monitor/hive.json}"

# Default to stdout (interactive). For a SUPERVISED producer, set KIJITOMON_EVENTS_FILE to an owned,
# size-rotated events log that survives rotation (see --events-file). Do not redirect stdout to the log.
set -- --state-file "$STATE_FILE" "$@"
[ -n "${KIJITOMON_EVENTS_FILE_TEMPLATE:-}" ] && set -- --events-file-template "$KIJITOMON_EVENTS_FILE_TEMPLATE" "$@"
[ -n "${KIJITOMON_EVENTS_FILE:-}" ] && set -- --events-file "$KIJITOMON_EVENTS_FILE" "$@"

exec python3 -u "$DIR/kijito_inbox_monitor.py" "$@"

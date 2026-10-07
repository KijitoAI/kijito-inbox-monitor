#!/bin/sh
# Render a supervisor definition for the producer from the shipped template, to stdout.
#
#   scripts/render-service.sh launchd --program PATH [--python PATH] [--home DIR] [--api-base URL]
#   scripts/render-service.sh systemd [--api-base URL]
#
#   launchd   com.kijito.inbox-monitor.plist.template with __PYTHON__ (default: `command -v python3`),
#             __PROGRAM__ (required) and __HOME__ (default: $HOME) filled in - the same substitution the
#             template's header documents. Write it to ~/Library/LaunchAgents/com.kijito.inbox-monitor.plist.
#   systemd   kijito-inbox-monitor@.service.template. Write it to ~/.config/systemd/user/kijito-inbox-monitor@.service.
#
# THE API BASE (row M486). A supervisor does not run your shell, so an exported $KIJITO_BASE never reaches the
# service. This script resolves the base the way the producer does (--api-base, else $KIJITO_BASE, else
# https://api.kijito.ai), validates it WITH the producer (`--print-api-base`, the one place the rule lives),
# and writes `--api-base URL` into the definition's command line so the running service uses it.
# For the default base nothing is added: the output is byte-for-byte what the template rendered before this
# option existed, so re-rendering an existing install changes nothing.
set -eu

DIR=$(CDPATH='' cd "$(dirname "$0")/.." && pwd)
PRODUCER="$DIR/kijito_inbox_monitor.py"

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; }
die() { printf 'render-service: %s\n' "$*" >&2; exit 2; }

[ $# -ge 1 ] || { usage >&2; exit 2; }
KIND=$1; shift
case "$KIND" in
  launchd|systemd) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; die "unknown kind: $KIND (launchd or systemd)" ;;
esac

PYTHON=""; PROGRAM=""; HOMEDIR=$HOME; HAVE_BASE=0; BASE_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --python) [ $# -ge 2 ] || die "--python needs a value"; PYTHON=$2; shift 2 ;;
    --program) [ $# -ge 2 ] || die "--program needs a value"; PROGRAM=$2; shift 2 ;;
    --home) [ $# -ge 2 ] || die "--home needs a value"; HOMEDIR=$2; shift 2 ;;
    --api-base) [ $# -ge 2 ] || die "--api-base needs a value"; HAVE_BASE=1; BASE_ARG=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

# The producer validates and normalises; this script never re-implements the rule.
if [ "$HAVE_BASE" = 1 ]; then
  BASE=$(python3 "$PRODUCER" --print-api-base --api-base "$BASE_ARG") || exit 2
else
  BASE=$(python3 "$PRODUCER" --print-api-base) || exit 2
fi
DEFAULT=$(KIJITO_BASE='' python3 "$PRODUCER" --print-api-base) || exit 2

if [ "$KIND" = launchd ]; then
  [ -n "$PROGRAM" ] || die "launchd needs --program PATH (the kijito_inbox_monitor.py the job runs)"
  [ -n "$PYTHON" ] || PYTHON=$(command -v python3) || die "no python3 on PATH; pass --python"
  for v in "$PYTHON" "$PROGRAM" "$HOMEDIR"; do
    case "$v" in
      /*) ;;
      *) die "launchd paths must be absolute (launchd does not search PATH): $v" ;;
    esac
    case "$v" in
      *'<'*|*'>'*|*'&'*|*'|'*|*"\\"*) die "refusing a path the plist cannot carry verbatim: $v" ;;
    esac
  done
  out=$(sed -e 's|__PYTHON__|'"$PYTHON"'|' \
            -e 's|__PROGRAM__|'"$PROGRAM"'|' \
            -e 's|__HOME__|'"$HOMEDIR"'|' \
            "$DIR/com.kijito.inbox-monitor.plist.template"; echo x)
  out=${out%x}
  if [ "$BASE" = "$DEFAULT" ]; then
    printf '%s' "$out"
    exit 0
  fi
  printf '%s' "$out" | awk -v base="$BASE" '
    { print }
    /^[ \t]*<string>--no-content<\/string>[ \t]*$/ {
      n++
      match($0, /^[ \t]*/); ind = substr($0, 1, RLENGTH)
      print ind "<string>--api-base</string>"
      print ind "<string>" base "</string>"
    }
    END { if (n != 1) { print "render-service: template drift: expected one --no-content argument, found " n > "/dev/stderr"; exit 3 } }'
else
  if [ "$BASE" = "$DEFAULT" ]; then
    cat "$DIR/kijito-inbox-monitor@.service.template"
    exit 0
  fi
  awk -v base="$BASE" '
    /^  --no-content \\$/ { n++; print "  --api-base " base " \\" }
    { print }
    END { if (n != 1) { print "render-service: template drift: expected one --no-content line in ExecStart, found " n > "/dev/stderr"; exit 3 } }' \
    "$DIR/kijito-inbox-monitor@.service.template"
fi

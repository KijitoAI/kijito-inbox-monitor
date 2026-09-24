#!/usr/bin/env bash
# Migrate a DEPLOYED kijito-inbox-monitor systemd user unit to producer-derived file names (row M313).
#
#   scripts/migrate-systemd-unit.sh            # DRY RUN: says what it would change, changes nothing
#   scripts/migrate-systemd-unit.sh --apply    # do it
#   options: --unit NAME (default kijito-inbox-monitor)   --bin PATH (default ~/.local/bin/kijito-inbox-monitor)
#            --unit-dir DIR (default ~/.config/systemd/user)
#
# WHAT CHANGES. Older units spelled the persona into paths with systemd's `%i` - the ESCAPED instance name -
# so the unit, not the producer, decided file names, and a persona like 'Loom' or 'name (purpose)' got a
# different file than the producer (and the launchd plist) would give it. The rewritten unit passes
# `--persona %I` and turns every `--state-file/--events-file/--token-file ...%i...` into the matching
# `-template ...{persona}...` flag, so the producer names every file with the rule `--safe-persona` prints.
#
# WHAT MOVES. For a persona that is already a safe component (lowercase ASCII - every persona on a normal
# fleet) the new paths are BYTE-IDENTICAL to the old ones: nothing moves and no consumer has to re-arm.
# Only an instance whose old and new paths differ is touched, and then (instance stopped first):
#   state   copied to the new name (the old file is kept)          - the cursor survives
#   token   copied to the new name, mode kept (the old file is kept)
#   events  HARD-LINKED under the new name - the old name is never removed, so a consumer running
#           `tail -F <old path>` notices nothing and keeps receiving every event the producer now appends
#           under the new name (same file). It stays that way until the producer next ROTATES the stream
#           (default 5 MB), after which only the new name moves on: re-arm consumers on the new path.
#           (Measured, GNU tail 9.4: a SYMLINK at the old path is refused as "untailable", and a rename
#           followed by a relink makes tail re-read the whole file - a replay of every old event.)
#
# SAFE TO RE-RUN. A unit with no `%i` left in ExecStart is reported as already migrated; files are only
# copied/moved when the new one does not exist yet. The old unit file is kept as <unit>.pre-m313.<time>.
# REFUSES to rewrite the unit while the installed producer lacks --state-file-template (it would not start).
# ANNOUNCE TO YOUR FLEET BEFORE --apply on a shared seat: instances restart, for a few seconds each.
set -eu
UNIT=kijito-inbox-monitor
BIN="$HOME/.local/bin/kijito-inbox-monitor"
UNIT_DIR="$HOME/.config/systemd/user"
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --unit) UNIT=$2; shift 2 ;;
    --bin) BIN=$2; shift 2 ;;
    --unit-dir) UNIT_DIR=$2; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
FILE="$UNIT_DIR/$UNIT@.service"
[ -f "$FILE" ] || { echo "no unit file at $FILE - nothing to migrate"; exit 0; }
say() { printf '%s\n' "$*"; }
[ "$APPLY" = 1 ] && say "== APPLY: $FILE ==" || say "== DRY RUN (pass --apply to act): $FILE =="

# The rewrite, as a pure text transform of the unit file (also used to compute the new paths).
rewrite() {
  sed -E \
    -e '/^ExecStart=/,/[^\\]$/ s/--persona %i/--persona %I/' \
    -e '/^ExecStart=/,/[^\\]$/ s/--(state-file|events-file|token-file)([ =])([^ ]*)%i([^ ]*)/--\1-template\2\3{persona}\4/g' \
    -e 's/^(Description=.*)%i/\1%I/' "$1"
}
exec_line() { tr -d '\r' < "$1" | sed -e ':a' -e '/\\$/N; s/\\\n//; ta' | grep '^ExecStart='; }
flag_value() { printf '%s\n' "$1" | tr ' ' '\n' | awk -v f="$2" 'p{print; exit} $0==f{p=1}'; }

old_exec=$(exec_line "$FILE")
case "$old_exec" in
  *%i*) ;;
  *) say "already migrated: ExecStart spells no %i (the producer names every file)"; exit 0 ;;
esac
tmp_unit=$(mktemp); trap 'rm -f "$tmp_unit"' EXIT
rewrite "$FILE" > "$tmp_unit"
new_exec=$(exec_line "$tmp_unit")
case "$new_exec" in *%i*) say "REFUSING: could not rewrite every %i in ExecStart:"; say "  $new_exec"; exit 1 ;; esac
say "ExecStart now:  ${old_exec#ExecStart=}"
say "ExecStart after: ${new_exec#ExecStart=}"

have_flags=0
"$BIN" --help 2>/dev/null | grep -q -- '--state-file-template' && have_flags=1
if [ "$have_flags" = 0 ]; then
  say "REFUSING to rewrite: $BIN does not support --state-file-template (needs kijito-inbox-monitor >= 0.5.4)."
  say "Upgrade the producer first, then re-run. (For persona names that are already lowercase ASCII the"
  say "old unit is correct as it is: this migration changes no file for them.)"
  [ "$APPLY" = 1 ] && exit 1
fi

H=$HOME
changed=(); to_start=()
# Every instance: loaded ones, plus enabled ones that are not loaded right now (a stopped template
# instance drops out of list-units, and its files must migrate too).
instances=$( { systemctl --user list-units --all --plain --no-legend "$UNIT@*.service" 2>/dev/null | awk '{print $1}'
               for w in "$UNIT_DIR"/*.wants/"$UNIT"@*.service; do [ -e "$w" ] || [ -L "$w" ] && basename "$w"; done
             } | sort -u)
while IFS= read -r inst; do
  [ -n "$inst" ] || continue
  esc=${inst#"$UNIT@"}; esc=${esc%.service}
  raw=$(systemd-escape --unescape -- "$esc")
  if [ "$have_flags" = 1 ]; then
    safe=$("$BIN" --safe-persona "$raw")
  else
    # Without an upgraded producer there is no --safe-persona to ask, and this script must not guess the
    # rule (row M290). A name that is already lowercase [a-z0-9._-] is its own component; say so for it,
    # and say plainly that anything else cannot be judged yet.
    case "$raw" in
      *[!a-z0-9._-]*) say "  $raw: cannot tell yet which files would move - needs the upgraded producer's --safe-persona"; continue ;;
    esac
    safe=$raw
  fi
  moves=""
  for f in --state-file --events-file --token-file; do
    o=$(flag_value "${old_exec#ExecStart=}" "$f"); [ -n "$o" ] || continue
    case "$o" in *%i*) ;; *) continue ;; esac
    # Parameter expansion, NOT sed: an escaped instance name carries backslashes ('name\x20\x28dev\x29')
    # and GNU sed would decode them in a replacement - the old path would then name a file that never
    # existed. (Caught by this script's own test against real systemd.)
    op=${o//%h/$H}; op=${op//%i/$esc}
    np=${o//%h/$H}; np=${np//%i/$safe}
    [ "$op" = "$np" ] || moves="$moves$f|$op|$np"$'\n'
  done
  if [ -z "$moves" ]; then
    say "  $raw: paths unchanged (already a safe component) - nothing moves, consumers need not re-arm"
    continue
  fi
  changed+=("$inst")
  while IFS='|' read -r f op np; do
    [ -n "$f" ] && say "  $raw: ${f#--} $op -> $np"
  done <<EOF
$moves
EOF
  [ "$APPLY" = 1 ] && [ "$have_flags" = 1 ] || continue
  was_active=0; systemctl --user is-active --quiet "$inst" && was_active=1
  systemctl --user stop "$inst"
  while IFS='|' read -r f op np; do
    [ -n "$f" ] && [ -e "$op" ] || continue
    [ -e "$np" ] && { say "    ${f#--}: $np already exists - left as is"; continue; }
    mkdir -p "$(dirname "$np")"
    case "$f" in
      --events-file) ln "$op" "$np" && say "    events: $np hard-linked to the live stream; $op still works until the next rotation" ;;
      *) cp -p "$op" "$np" && say "    ${f#--} copied (old file kept)" ;;
    esac
  done <<EOF
$moves
EOF
  [ "$was_active" = 1 ] && to_start+=("$inst")
done <<EOF
$instances
EOF

[ "$APPLY" = 1 ] || { say "(dry run: nothing changed)"; exit 0; }
cp -p "$FILE" "$FILE.pre-m313.$(date +%Y%m%dT%H%M%S)"
cp "$tmp_unit" "$FILE"
systemctl --user daemon-reload
# The instances this script stopped are started again BY NAME (they are no longer listed once stopped);
# every other running instance is restarted so it picks up the rewritten ExecStart (same paths).
for inst in "${to_start[@]+"${to_start[@]}"}"; do systemctl --user start "$inst"; done
while IFS= read -r inst; do
  [ -n "$inst" ] || continue
  case " ${to_start[*]+"${to_start[*]}"} " in *" $inst "*) continue ;; esac
  systemctl --user is-active --quiet "$inst" && systemctl --user restart "$inst"
done <<EOF
$instances
EOF
say "migrated: unit rewritten (old copy kept next to it); ${#changed[@]} instance(s) had files moved"

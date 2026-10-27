#!/bin/sh
# Pre-publish gates for a release. Run from anywhere in the repo; see RELEASING.md step 3.
#
#   ./scripts/prepublish-gate.sh            # canary, then the gates over the public surface
#   ./scripts/prepublish-gate.sh --canary   # prove the gate can still fail, then exit
#
# Six checks over the PUBLIC surface (if you add a check, the loops below say it too):
#   1. typography     - em/en dashes, curly quotes, ellipsis
#   2. memory-ids     - wikilink-style numeric ids (a number in double square brackets)
#   3. path-escapes   - references to paths ABOVE the repository root (../)
#   4. private-detail - an absolute home-directory path, or a private current-state sentinel name
#   5. work-items     - internal tracker and review references (the WORK_ITEMS pattern below)
#   6. names          - internal names, matched by sha256 of each lowercase word token (the list holds
#                       digests only, so this file never spells the names it bans)
#
# Why check 3 exists: a public document that points at `../something` describes a file the clone does
# not contain, and the path resolves to whatever happens to sit above the checkout. It bans `../` across
# the whole surface, code included. If a legitimate use ever arises, allow it here deliberately rather
# than loosening the pattern.
#
# Properties this script guarantees:
#
#   * THE FILE LIST IS RE-DERIVED from git, never hardcoded, so a new file is never silently omitted.
#     pyproject.toml and package.json are included deliberately: they carry the PyPI/npm descriptions,
#     which are immutable per version and so cannot be fixed after publishing.
#
#   * PATHS ARE NUL-DELIMITED into `xargs -0`. Writing this inline as
#         FILES=$(git ls-files); grep -nE ... $FILES || echo clean
#     does not word-split in zsh: grep gets one nonexistent filename, exits non-zero, and the
#     `|| echo clean` prints success having inspected nothing.
#
#   * IT NAMES ITS SPECIMEN AND REFUSES A FOREIGN ONE. `git rev-parse --show-toplevel` resolves to
#     whatever repository you are standing in, so this script checks that it is gating this project
#     before it reports anything.
#
#   * THE CANARY RUNS IN BOTH DIRECTIONS, ONE SHAPE AT A TIME. Each bad shape is checked alone (a
#     combined fixture proves only that SOME line matched), and every pattern must stay silent on a
#     good fixture that deliberately contains the near misses it must ignore (a detector that flags
#     everything passes any bad-input-only canary).
set -eu

usage() {
    cat <<'USAGE'
usage: prepublish-gate.sh [--canary]

  (no args)   run the canary, then the gates over the public surface
  --canary    run the canary only (prove the gate can still fail), then exit
  -h, --help  this text

exit: 0 clean | 1 a gate FAILED | 2 the gate could not be trusted (bad args, wrong repo, broken canary)
USAGE
}

# Unknown arguments are rejected rather than ignored: a tool that ignores an argument you meant lets you
# believe you ran something you did not.
canary_only=0
for arg in "$@"; do
    case "$arg" in
        --canary) canary_only=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ABORT: unknown argument: %s\n\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done

# The pipe warning goes to stderr at run time, so it reaches the terminal even when stdout is piped.
if [ ! -t 1 ]; then
    printf '%s\n' "note: stdout is not a terminal. If you piped this, \$? is the LAST pipeline stage's status, NOT this gate's answer. Use \${PIPESTATUS[0]} (bash) / \${pipestatus[1]} (zsh), or run it unpiped." >&2
fi

command -v python3 >/dev/null 2>&1 || { echo "ABORT: python3 is required for the names check." >&2; exit 2; }

TYPOGRAPHY='—|–|[“”‘’]|…'
# MEMORY IDS - A DECLARED NARROWING. Only the unambiguous double-bracket form is matched. The bare
# single-bracket form is indistinguishable from a Python integer-list literal (`self.assertEqual(em.new_ids, [1200])` in
# the test suite), so matching it would force either false positives or skipping whole published files.
# Narrowing the pattern keeps every file on the surface inspected.
MEMORY_IDS='\[\[[0-9]+\]\]'
PATH_ESCAPES='\.\./'
# PRIVATE DETAIL: an absolute home directory, or a private current-state sentinel name. Matched by
# content, never by filename: a filename rule only covers the files you already thought of.
PRIVATE_DETAIL='/(Users|home)/[A-Za-z0-9._-]+|_CURRENT_STATE_POINTER'
# WORK ITEMS: internal tracker references have no meaning to a reader of the published files.
WORK_ITEMS='\bM[0-9]{2,4}\b|\b(row|finding|item)s? N[0-9]{1,3}\b|re-(audit)\b|\bhive [0-9]{3,}\b|\bruling [0-9]'

# NAMES: sha256 hex digests of lowercase internal names. A token is a run of [a-z0-9] with an optional
# apostrophe suffix kept attached ("don't" is one token, so an English contraction never collides).
# Hyphenated compounds are also hashed whole, as every contiguous run of their parts, so a listed
# compound name matches without listing either of its words (which may be product names).
# Two words that are legitimate on this surface (a product name, and the author contact in
# pyproject.toml) are deliberately not in this list; the test suite applies its own rules to them.
NAME_DIGESTS='
5f5a8ed8f139be6df7cf8f4a91d3e4649961a98c82fd521c18d6f292b031a319
444b759c5264422ea582403ae2083d2447fd226a2e40795968dd740e9202cb97
2fc2a6dd013e7ab41c5a54ab8a254031ff329aa42c5815ac420f1facfe2542d1
1c85bf7d3c260817db543a95c0e52d02563b5dd4dbd8bd656de82c8a0c0610b2
9b2153df65c7cba8a514938e2d3254a32cc0e75f6d032572e45f2b6bb124c970
4f6b9e1c24d93d9aeb4607b9d21de1f9345b735ec1b786dd663b5e9fe5171091
0b2319dc1ccf78038936020d8eeef539ed8a6485fc6b9eb7826225a5276393b2
401cb0a3c9577a6aede136e6d5d4c9b87b955ef3c98010ff91390acd06d4b2aa
784a2ddf5d4d61b6ae157ca800ce8ab7694b7141dc6f652a2600323d706a3d86
e9087d0b20d80d3e12bc8530d883d7ad9c1eb3ebc5cb61824a2b460816503797
37a783c007a47569686de2be3085d1aa163cbfb873a2d06d4d62d110cc2cbb5e
8a62e967fcd6dfa5d75308c37808b4668a7faf1cdb06e09ac0a7161827603887
233e1cded61ace811d7a930947b06acb18cda1dc7b67e215941e1830fdec3b7c
e63b07c6a52c4208fe0579f52bc213fef890e17c2b0b44e71f77385fd9b48f62
c5924fae93560d14aac075b10d3439131ad462e5fa6ef949ba4d24de4d63b4d2
340d6bb972c2bc1eb7e627ff505ae4fa90c8de05ea0c9903da7c161942b13cec
162094c4d87101ff5aca2e655c39a40744052b4e1deb3f100391d45946db0bec
16f56c70f255525be5573faa19738ec1ad5badbf4a3eefaa7d380f18964aae1c
386de061f90d8440f2c3d090e15bcb582741809594e55c99869f7bacc0c71e77
d8872aff63c047bd83219ac743325b3a100d23e95cad298f14a10c01bae2dec4
4a2326c64ab2287ead87a51d07628f24c245cbf1147a2065707456d732594cba
4c5af973f604c186dcbe090a746d4d0248ae40fed496b48510104bb4b732ea51
8bdb247a2a76e166450e193f185f0deeceedee6550589de2cd964e7ce36c9de6
ebbdd141134c93b91745fce8998ab8dd25ac7e715bd7fe85a7fac1a6ac9d3492
6aa37ea8a9fbf00af3b59939c0009b6c30398d95094e7b04e51ee3ec75ce7169
b000761cf2296663cb0321db6e772a2e4ac75e35d3bee9ce207c148ba2f280f9
06a86719d2030817190714299b9b76f7b0c79531e925c394d9e6e884ac881fca
'
# Canary-only digests of the dummy token "zzcanaryname" and the dummy compound "zzq-canary". They are
# added to the list ONLY while the canary runs, so the canary can plant a name without this file
# spelling a real one.
CANARY_DIGEST="ceaf135cba85f647993405891d48fd2a6dabfe710a2a0c8e35395fcce01cd803 6c9b89c6154f0734bc64cc173400bd0c7b3571e1cd494c29ff752f5fc9755d02"

# name_hits DIGESTS: reads NUL-delimited paths on stdin, prints path:line: text for every line holding a
# token whose sha256 is in DIGESTS.
name_hits() {
    python3 -c '
import hashlib, re, sys
digests = set(sys.argv[1].split())
tok = re.compile(r"[a-z0-9]+(?:\x27[a-z]+)?")
comp = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
def tokens(low):
    for t in tok.findall(low):
        yield t
    for c in comp.findall(low):
        parts = c.split("-")
        for i in range(len(parts)):
            for j in range(i + 2, len(parts) + 1):
                yield "-".join(parts[i:j])
for path in sys.stdin.buffer.read().split(b"\0"):
    if not path:
        continue
    p = path.decode()
    try:
        text = open(p, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print("%s: unreadable: %s" % (p, e))
        continue
    for n, line in enumerate(text.splitlines(), 1):
        if any(hashlib.sha256(t.encode()).hexdigest() in digests for t in tokens(line.lower())):
            print("%s:%d: %s" % (p, n, line.strip()[:160]))
' "$1"
}

# THE SCOPE. Everything tracked or untracked-but-not-ignored is inspected, minus exactly one file:
#   scripts/prepublish-gate.sh - THIS FILE. It necessarily contains the literals it bans, because they are
#                                its patterns. Any other way of sparing them would weaken the patterns.
# Test files and CI configuration are published to every clone, so they are inspected too.
EXEMPT='^(scripts/prepublish-gate\.sh$)'

fixture=$(mktemp)
trap 'rm -f "$fixture"' EXIT

# ---- canary, direction 1: every bad SHAPE must fire, each checked ALONE ---------------------------
bad_shape() {   # label, pattern, shape
    printf '%s\n' "$3" > "$fixture"
    if ! grep -qE "$2" "$fixture"; then
        printf 'ABORT: %s does NOT fire on a known-bad shape, so a clean result from it means nothing:\n  %s\n' "$1" "$3" >&2
        exit 2
    fi
}
bad_shape typography   "$TYPOGRAPHY"   'an em — dash'
bad_shape typography   "$TYPOGRAPHY"   'an en – dash'
bad_shape typography   "$TYPOGRAPHY"   'curly “double” quotes'
bad_shape typography   "$TYPOGRAPHY"   'curly ‘single’ quotes'
bad_shape typography   "$TYPOGRAPHY"   'an ellipsis… character'
bad_shape memory-ids   "$MEMORY_IDS"   'a wikilink [[123''45]] id'
bad_shape memory-ids   "$MEMORY_IDS"   'see [[987''65]] for details'
bad_shape path-escapes "$PATH_ESCAPES" '    ../bin/some-helper.sh'
bad_shape path-escapes "$PATH_ESCAPES" 'require("../lib/thing")'
bad_shape path-escapes "$PATH_ESCAPES" '../../etc/somewhere'
bad_shape path-escapes "$PATH_ESCAPES" 'see ../docs/guide.md'
bad_shape private-detail "$PRIVATE_DETAIL" '    <string>/Users/someone/Code/thing/x.py</string>'
bad_shape private-detail "$PRIVATE_DETAIL" 'a linux home /home/someone/.config/x'
bad_shape private-detail "$PRIVATE_DETAIL" 'see the pointer, sentinel SOMEONE_CURRENT_STATE_POINTER_V1'
bad_shape work-items   "$WORK_ITEMS"   '# the unit never spells a persona (M3''13)'
bad_shape work-items   "$WORK_ITEMS"   'fixed in row N''42'
bad_shape work-items   "$WORK_ITEMS"   '# (re-''audit 7, HI''GH 1)'
bad_shape work-items   "$WORK_ITEMS"   'per hive 78''19'
bad_shape work-items   "$WORK_ITEMS"   'see rul''ing 5612'
printf '%s\n' '# reviewed by zzcanaryname last week' > "$fixture"
for planted in '# reviewed by zzcanaryname last week' 'a state file named hive.zzq-canary.json'; do
    printf '%s\n' "$planted" > "$fixture"
    if [ -z "$(printf '%s\0' "$fixture" | name_hits "$NAME_DIGESTS $CANARY_DIGEST")" ]; then
        printf 'ABORT: names does NOT fire on a planted name, so a clean result from it means nothing:\n  %s\n' "$planted" >&2
        exit 2
    fi
done

# ---- canary, direction 2: the GOOD fixture CONTAINS what each pattern must ignore -----------------
cat > "$fixture" <<'GOOD'
an ascii - hyphen, "straight" double and 'straight' single quotes, and three dots...
short ids [12] [123], a longer one [123456], plus [[notdigits]] and [abc]
the DECLARED NARROWING, pinned: bare [1234] and [12345] and self.assertEqual(em.new_ids, [1200])
./relative/path, file..txt, three...dots, /absolute/path, parent/child
~/.config/kijito-inbox-monitor/token and $HOME/.cache/x and __HOME__/.cache/y
/usr/local/bin, /opt/homebrew/bin/python3, /etc/hosts, /var/log/x
the operator current state pointer, written as lower-case prose
a mode of 0600, error -32600, readline(65536), M1 and M4 sizes
Don't, zzcanarynames, canary, alice, bob, carol, 2026-07-24T23:24:43Z
zzq alone, canary alone, zzq_canary, and Claude Code as a product name
GOOD
# Near misses that contain a trigger: a part number, an English word that starts with one, and the
# product's own hive file names. Assembled from parts so this file does not spell them.
printf '%s\n' "an N""42 part number, re-""auditing, hi""ve.json and hi""ve 12 messages" >> "$fixture"
for label_pattern in "typography:$TYPOGRAPHY" "memory-ids:$MEMORY_IDS" "path-escapes:$PATH_ESCAPES" \
                     "private-detail:$PRIVATE_DETAIL" "work-items:$WORK_ITEMS"; do
    label=${label_pattern%%:*}
    pattern=${label_pattern#*:}
    if grep -qE "$pattern" "$fixture"; then
        printf 'ABORT: %s fires on known-GOOD input, so it would condemn a release regardless of content:\n' "$label" >&2
        grep -nE "$pattern" "$fixture" >&2
        exit 2
    fi
done
if [ -n "$(printf '%s\0' "$fixture" | name_hits "$NAME_DIGESTS $CANARY_DIGEST")" ]; then
    echo "ABORT: names fires on known-GOOD input, so it would condemn a release regardless of content." >&2
    exit 2
fi

# ---- canary, direction 3: the SCOPE, not the patterns ---------------------------------------------
# A pattern that works proves nothing about a file the gate never opens. Assert that a leak planted in
# each kind of path WOULD be inspected, and that the one file spared is spared by a declared exemption.
for planted in "scripts/leak-canary.md" "scripts/new-helper.py" "docs/guide.md" "README.md" \
               "test_kijito_monitor.py" "tests/test_something.py" ".github/workflows/publish-npm.yml"; do
    if printf '%s\n' "$planted" | grep -qE "$EXEMPT"; then
        printf 'ABORT: %s would be EXEMPT from every check, so a leak planted there is invisible.\n' "$planted" >&2
        exit 2
    fi
done
if ! printf '%s\n' "scripts/prepublish-gate.sh" | grep -qE "$EXEMPT"; then
    printf 'ABORT: the gate no longer exempts ITSELF, so it will condemn its own pattern literals.\n' >&2
    exit 2
fi
echo "canary: every pattern fires on each known-bad shape and stays silent on known-good input"
echo "canary: scope holds - a leak planted under scripts/, tests/, test_* or .github/ is inspected;"
echo "        only scripts/prepublish-gate.sh is exempt, and it is declared"

if [ "$canary_only" -eq 1 ]; then
    echo "CANARY CLEAN - the gate can still tell good from bad. (No surface inspected: --canary.)"
    exit 0
fi

# ---- name the specimen, and refuse a foreign one --------------------------------------------------
root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "ABORT: not inside a git repository, so there is no surface to gate." >&2; exit 2; }
cd "$root"
if [ ! -f kijito_inbox_monitor.py ] || [ ! -f RELEASING.md ]; then
    printf 'ABORT: %s is not the kijito-inbox-monitor repository (no kijito_inbox_monitor.py + RELEASING.md).\n' "$root" >&2
    printf '       Refusing to report a clean gate over a repository this gate was not written for.\n' >&2
    exit 2
fi
echo "specimen: $root"

# --cached AND --others: a brand-new, not-yet-added file is the likeliest one to carry a leak, because
# nobody has reviewed it. --exclude-standard keeps .gitignore'd build artifacts out.
tracked=$(git ls-files -z --cached --others --exclude-standard | tr '\0' '\n' | sort -u)
files=$(printf '%s\n' "$tracked" | grep -vE "$EXEMPT" || true)
excluded=$(printf '%s\n' "$tracked" | grep -E "$EXEMPT" || true)
if [ -z "$files" ]; then
    echo "ABORT: re-derived file list is EMPTY - refusing to report a clean gate over nothing." >&2
    exit 2
fi
count=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
echo "surface: $count file(s)"
# Declare the exemptions and the narrowing, so a reader can tell "clean" from "not looked at".
if [ -n "$excluded" ]; then
    printf 'exempt: %s file(s) NOT inspected -' "$(printf '%s\n' "$excluded" | wc -l | tr -d ' ')"
    printf ' %s' $(printf '%s\n' "$excluded")
    printf '\n'
else
    echo "exempt: none"
fi
echo "narrowed: memory-ids matches the unambiguous [[nnnnn]] form only - the bare [nnnnn] form is"
echo "          indistinguishable from an integer list literal (e.g. new_ids, [1200]) and is NOT checked"

status=0
for label_pattern in "typography:$TYPOGRAPHY" "memory-ids:$MEMORY_IDS" "path-escapes:$PATH_ESCAPES" \
                     "private-detail:$PRIVATE_DETAIL" "work-items:$WORK_ITEMS"; do
    label=${label_pattern%%:*}
    pattern=${label_pattern#*:}
    hits=$(printf '%s\n' "$files" | tr '\n' '\0' | xargs -0 grep -nE "$pattern" || true)
    if [ -n "$hits" ]; then
        echo "FAIL $label:"
        printf '%s\n' "$hits"
        status=1
    else
        echo "pass $label"
    fi
done
hits=$(printf '%s\n' "$files" | tr '\n' '\0' | name_hits "$NAME_DIGESTS")
if [ -n "$hits" ]; then
    echo "FAIL names:"
    printf '%s\n' "$hits"
    status=1
else
    echo "pass names"
fi

[ "$status" -eq 0 ] && echo "GATES CLEAN over $count file(s) in $root" || echo "GATES FAILED - do not tag"
exit "$status"

#!/bin/sh
# Pre-publish gates for a release. Run from anywhere in the repo; see RELEASING.md step 3.
#
#   ./scripts/prepublish-gate.sh            # canary, then the gates over the public surface
#   ./scripts/prepublish-gate.sh --canary   # prove the gate can still fail, then exit
#
# Four checks over the PUBLIC surface (this count is the exact thing that drifts - if you add a check,
# the loops below and RELEASING.md step 3 say it too):
#   1. typography   - em/en dashes, curly quotes, ellipsis
#   2. memory-ids   - internal Kijito memory ids ([[12345]] / [12345])
#   3. path-escapes - references to paths ABOVE the repository root (../)
#   4. private-detail - an operator absolute home path, or a private handoff sentinel
#
# Why check 3 exists (added 2026-07-29, after finding two live instances): RELEASING.md instructed the
# reader to run `../bin/producer-health.sh`, a helper that lives in the private workspace ALONGSIDE this
# repo and is not part of the package. A clone therefore could not run the gate its own release document
# mandated, and `../bin/...` would resolve to whatever happened to sit above the checkout - so the
# instruction was not merely broken, it was ambiguous in a way that depends on the reader's directory
# layout. The second instance was a stale "still open" note in docs/DESIGN.md pointing at a repo-external
# copy of itself. Both are the same class: THE PUBLIC SURFACE DESCRIBING THINGS THAT ARE NOT IN IT.
# NOTE ON SCOPE: this bans `../` across the whole surface, code included. There is no legitimate use
# today. If one ever arises - a JS require, say - that should be a deliberate, visible decision at this
# gate, not a quietly loosened regex.
#
# Properties this script exists to guarantee, each of which has failed in practice:
#
#   * THE FILE LIST IS RE-DERIVED from `git ls-files`, never hardcoded. A hardcoded list has been
#     wrong twice, and it silently omits exactly the files added since someone last edited it.
#     pyproject.toml and package.json are included deliberately: they carry the PyPI/npm
#     descriptions, which are IMMUTABLE per version and so cannot be fixed after publishing.
#
#   * PATHS ARE NUL-DELIMITED into `xargs -0`. Writing this inline as
#         FILES=$(git ls-files); grep -nE ... $FILES || echo clean
#     does not word-split in zsh: grep gets one nonexistent filename, exits non-zero, and the
#     `|| echo clean` prints success having inspected NOTHING. That false clean was observed here.
#
#   * IT NAMES ITS SPECIMEN AND REFUSES A FOREIGN ONE (assay review, 2026-07-30). `git rev-parse
#     --show-toplevel` resolves to whatever repository you happen to be standing in, so run from
#     somewhere else this script cheerfully gated ANOTHER project and printed GATES CLEAN. A pass that
#     does not say what it inspected is not evidence, and a gate that can pass over the wrong subject
#     is an instance of the very wrong-specimen class it was written to catch.
#
#   * THE CANARY RUNS IN BOTH DIRECTIONS, ONE SHAPE AT A TIME (assay review, 2026-07-30). A gate is
#     only evidence if it can still fail. Two refinements over the original, both learned the hard way:
#     (1) each bad SHAPE is checked ALONE, because a combined fixture proves only that SOME line
#     matched - a pattern narrowed to the one historical string (`\.\./bin/`) would still have passed
#     while missing every other escape; (2) the patterns must also stay SILENT on a good fixture that
#     deliberately CONTAINS the near-miss shapes they must ignore, because a detector sabotaged to flag
#     EVERYTHING passes any bad-input-only canary. A canary can only see the variable it moves.
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

# REAL ARGUMENT HANDLING (assay review, 2026-07-30). This used to accept and silently IGNORE anything,
# so `--canary` - the flag every other check in this fleet has - ran the FULL gate and reported clean.
# A tool that ignores an argument you meant is worse than one that rejects it: you believe you ran
# something you did not.
canary_only=0
for arg in "$@"; do
    case "$arg" in
        --canary) canary_only=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ABORT: unknown argument: %s\n\n' "$arg" >&2; usage >&2; exit 2 ;;
    esac
done

# THE PIPE WARNING, AT RUN TIME, ON STDERR (the 3b339ce fleet standard). A header comment is read by
# whoever EDITS the script, not by whoever RUNS it - and running it is when the mistake happens. stderr
# does not travel down the pipe, so this still lands on the terminal in the exact case being warned about.
if [ ! -t 1 ]; then
    printf '%s\n' "note: stdout is not a terminal. If you piped this, \$? is the LAST pipeline stage's status, NOT this gate's answer. Use \${PIPESTATUS[0]} (bash) / \${pipestatus[1]} (zsh), or run it unpiped." >&2
fi

TYPOGRAPHY='—|–|[“”‘’]|…'
# MEMORY IDS — DECLARED NARROWING, not an exemption. Read the distinction, it is the whole point:
# an EXEMPTION skips a SURFACE entirely (nothing on it is checked, by anything); a NARROWING drops one
# ambiguous PATTERN CLASS while every other check still runs over that same surface.
#
# ⚠️ THIS USED TO ALSO MATCH THE BARE `[12345]` FORM, and that is why `test_*` and `.github/` were
# EXEMPT: `test_kijito_monitor.py` contains `self.assertEqual(em.new_ids, [1200])` and friends, which
# are Python integer-list literals and not memory ids. The bare form is indistinguishable from a list
# literal ANYWHERE, so the old shape forced a choice between false positives and skipping whole
# published files — and it chose the second, on a stated reason that turned out to be false (see the
# SCOPE block below). Narrowing the PATTERN removes the need to skip the SURFACE.
# ⇒ COST, STATED RATHER THAN BURIED: a bare `[12345]` memory id written in prose is no longer caught
# here. Every real leak this row was opened for used the `[[...]]` form (`[[22206]]` in
# scripts/mutation-check.py), and the private-detail check independently catches the sentinel.
#
# 🛑 THE SAME NARROWING IS DECLARED IN River's `gates/public-remote-leak-gate.sh`, AND THE DUPLICATION
# IS DELIBERATE. Sharing one literal between them would make that gate's INSTRUMENT depend on this
# repo — "a gate may reach outside its repo for its SUBJECT, never for its INSTRUMENT" — so a single
# edit here would silently redefine what the external check is able to see. Two independent copies
# that can drift are strictly safer than one copy that can be moved from the subject side.
MEMORY_IDS='\[\[[0-9]+\]\]'
PATH_ESCAPES='\.\./'
# PRIVATE DETAIL (M185): an operator's absolute home directory, or a private Kijito handoff sentinel,
# published to a public remote. Both were live here: the tracked LaunchAgent baked ONE operator's home
# into seven paths, and RELEASING.md named a private current-state sentinel.
# ★ MATCHED BY CONTENT, NEVER BY FILENAME - the M160 lesson. A filename-based rule ("check the plist")
# is a rule about the files you already thought of; the leak arrives in the file you did not.
PRIVATE_DETAIL='/(Users|home)/[A-Za-z0-9._-]+|_CURRENT_STATE_POINTER'

# THE SCOPE. EVERYTHING TRACKED IS INSPECTED, MINUS EXACTLY ONE ENUMERATED FILE:
#   scripts/prepublish-gate.sh - THIS FILE, and the only one that HAS to be exempt: it necessarily
#                       CONTAINS the literals it bans, because they are its patterns and its own
#                       documentation. Any other way of sparing them would mean weakening the patterns.
#
# ⚠️ IT USED TO EXEMPT THE WHOLE `scripts/` PREFIX, and that was a silent scope hole (assay's review,
# 2026-07-30 - M184). Narrowing it to the one file that needs it immediately surfaced TWO REAL LEAKS
# that had been invisible the whole time: internal Kijito memory ids in `scripts/mutation-check.py`,
# published to a public remote. A prefix is a cheap way to SPELL an exemption and an expensive way to
# MEAN one - it silently covers every file added under that prefix later, forever.
#
# 🛑🛑 AND IT ALSO EXEMPTED `test_`, `tests/` AND `.github/` UNTIL 2026-08-01, ON A STATED REASON THAT
# WAS SIMPLY FALSE. The comment read "the suite is not published surface" and "CI configuration is not
# published surface". Measured on a fresh bare clone of the PUBLIC remote:
#     .github/workflows/publish-npm.yml
#     .github/workflows/publish-pypi.yml
#     test_kijito_monitor.py
# All three are tracked; every `git clone` hands a stranger all three; all three were skipped by ALL
# FOUR checks. ⇒ ★ AN EXEMPTION CAN BE WRONG IN ITS PREMISE RATHER THAN ITS FORM. Enumerating those
# files by name and canarying every direction would have left a well-formed exemption resting on a
# claim that was not true. CHECK WHETHER THE STATED REASON IS TRUE BEFORE ARGUING ABOUT THE SHAPE.
#
# ⚖️ WHY THEY WERE DROPPED RATHER THAN RE-JUSTIFIED (assay's ruling, 2026-08-01). The tempting fix was
# to keep them with a TRUE reason - "inspected by River's gate_M185 instead", which is factually
# correct, since that checker covers the whole published surface minus one enumerated path. It was
# REJECTED on a property neither option named: TIMING. This gate is PREVENTIVE - it runs before
# content reaches the remote. gate_M185 is DETECTIVE - it measures the remote after the fact. A true
# justification that routes a surface to the detective control silently converts prevention into
# post-publish detection: a leak in tests or CI would publish FIRST and be found SECOND, which is the
# wrong order for the only control class whose entire value is running first.
# ⇒ The false-positive problem that motivated the original exemption is solved by the DECLARED
# NARROWING on MEMORY_IDS above, not by skipping files: typography, path-escapes and private-detail
# now run UNNARROWED over tests and CI, because nothing in those classes collides there.
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
bad_shape memory-ids   "$MEMORY_IDS"   'a wikilink [[12345]] id'
bad_shape memory-ids   "$MEMORY_IDS"   'the real historical leak [[22206]] shape'
bad_shape path-escapes "$PATH_ESCAPES" '    ../bin/some-helper.sh'
bad_shape path-escapes "$PATH_ESCAPES" 'require("../lib/thing")'
bad_shape path-escapes "$PATH_ESCAPES" '../../etc/somewhere'
bad_shape path-escapes "$PATH_ESCAPES" 'see ../docs/DESIGN.md'
bad_shape private-detail "$PRIVATE_DETAIL" '    <string>/Users/someone/Code/thing/x.py</string>'
bad_shape private-detail "$PRIVATE_DETAIL" 'a linux home /home/someone/.config/x'
bad_shape private-detail "$PRIVATE_DETAIL" 'see the pointer, sentinel SOMEONE_CURRENT_STATE_POINTER_V1'

# ---- canary, direction 2: the GOOD fixture CONTAINS what each pattern must ignore -----------------
# It is not enough for the good fixture to merely OMIT the bad shapes - a detector sabotaged to flag
# everything passes that. Every line below is a near miss the gate must stay silent about.
cat > "$fixture" <<'GOOD'
an ascii - hyphen, "straight" double and 'straight' single quotes, and three dots...
short ids [12] [123], a longer one [123456], plus [[notdigits]] and [abc]
the DECLARED NARROWING, pinned: bare [1234] and [12345] and self.assertEqual(em.new_ids, [1200])
./relative/path, file..txt, three...dots, /absolute/path, parent/child
~/.config/kijito-inbox-monitor/token and $HOME/.cache/x and __HOME__/.cache/y
/usr/local/bin, /opt/homebrew/bin/python3, /etc/hosts, /var/log/x
the operator current state pointer, written as lower-case prose
GOOD
for label_pattern in "typography:$TYPOGRAPHY" "memory-ids:$MEMORY_IDS" "path-escapes:$PATH_ESCAPES" "private-detail:$PRIVATE_DETAIL"; do
    label=${label_pattern%%:*}
    pattern=${label_pattern#*:}
    if grep -qE "$pattern" "$fixture"; then
        printf 'ABORT: %s fires on known-GOOD input, so it would condemn a release regardless of content:\n' "$label" >&2
        grep -nE "$pattern" "$fixture" >&2
        exit 2
    fi
done
# ---- canary, direction 3: the SCOPE, not the patterns (M184) --------------------------------------
# A pattern that works proves nothing about a file the gate never opens. Two of the three checks above
# were passing for years while `scripts/` was wholly exempt and leaking. So: assert that a leak planted
# under scripts/ WOULD be inspected, and that the one file spared is spared by an exemption this script
# DECLARES. Silence about a skipped path is the failure, not the skipping.
# ⚠️ THE FORMERLY-EXEMPT PREFIXES ARE PLANTED HERE DELIBERATELY. Until 2026-08-01 not one direction
# in this loop touched `test_`, `tests/` or `.github/` - so the three prefixes whose justification was
# false were also the three no control ever exercised. A canary that never touches the contested
# surface is the defect this whole finding started from, so each of them now has a planted path.
for planted in "scripts/leak-canary.md" "scripts/mutation-check.py" "docs/DESIGN.md" "README.md" \
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

# --cached AND --others, because `git ls-files` alone reproduces one level up the very defect the
# comment at the top of this file warns about. That comment says a HARDCODED list "silently omits
# exactly the files added since someone last edited it", and re-deriving from git fixed that - for
# TRACKED files. An untracked new file is still omitted, and the gate still printed "GATES CLEAN".
# ⇒ MEASURED 2026-08-06: a new, unstaged kijito-inbox-monitor@.service.template gave
#   "surface: 17 file(s) -> GATES CLEAN" while that file was never opened; `git add` made it 18.
# ★ A BRAND-NEW FILE IS THE LIKELIEST ONE TO CARRY A LEAK - nobody has ever reviewed it - and it was
#   precisely the one the gate could not see. Silence read as approval.
# --exclude-standard keeps .gitignore'd build artifacts (build/, *.egg-info/, __pycache__/) out, so
# this widens the surface to unreviewed SOURCE only, never to generated noise.
tracked=$(git ls-files -z --cached --others --exclude-standard | tr '\0' '\n' | sort -u)
files=$(printf '%s\n' "$tracked" | grep -vE "$EXEMPT" || true)
excluded=$(printf '%s\n' "$tracked" | grep -E "$EXEMPT" || true)
if [ -z "$files" ]; then
    echo "ABORT: re-derived file list is EMPTY - refusing to report a clean gate over nothing." >&2
    exit 2
fi
count=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
echo "surface: $count file(s)"
# DECLARE THE EXEMPTIONS. Silence about what was skipped is the failure mode; a reader must be able to
# tell "clean" from "not looked at" without reading this script.
if [ -n "$excluded" ]; then
    printf 'exempt: %s file(s) NOT inspected -' "$(printf '%s\n' "$excluded" | wc -l | tr -d ' ')"
    printf ' %s' $(printf '%s\n' "$excluded")
    printf '\n'
else
    echo "exempt: none"
fi
# DECLARE THE NARROWING AT RUN TIME, beside the exemptions. It is a real reduction in what this gate
# can see, and a reduction that lives only in a source comment is one the reader never learns about
# at the moment they are deciding whether to trust a clean result.
echo "narrowed: memory-ids matches the unambiguous [[nnnnn]] form only - the bare [nnnnn] form is"
echo "          indistinguishable from an integer list literal (e.g. new_ids, [1200]) and is NOT checked"

status=0
for label_pattern in "typography:$TYPOGRAPHY" "memory-ids:$MEMORY_IDS" "path-escapes:$PATH_ESCAPES" "private-detail:$PRIVATE_DETAIL"; do
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

[ "$status" -eq 0 ] && echo "GATES CLEAN over $count file(s) in $root" || echo "GATES FAILED - do not tag"
exit "$status"

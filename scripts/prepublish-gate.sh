#!/bin/sh
# Pre-publish gates for a release. Run from anywhere in the repo; see RELEASING.md step 3.
#
# Two checks over the PUBLIC surface:
#   1. typography  - em/en dashes, curly quotes, ellipsis
#   2. leakage     - internal Kijito memory ids ([[12345]] / [12345])
#
# Three properties this script exists to guarantee, each of which has failed in practice:
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
#   * A CANARY RUNS FIRST. A gate is only evidence if it can still fail, so the script feeds itself
#     a known-bad line and aborts if the pattern does not match it. Without that, a broken regex, a
#     locale change or a grep replacement turns the gate into a rubber stamp that certifies
#     everything - worse than having no gate, because it is trusted.
set -eu

cd "$(git rev-parse --show-toplevel)"

TYPOGRAPHY='—|–|[“”‘’]|…'
MEMORY_IDS='\[\[[0-9]+\]\]|\[[0-9]{4,5}\]'

canary=$(mktemp)
trap 'rm -f "$canary"' EXIT
printf 'a — b “c” d…\n[[12345]] [1234]\n' > "$canary"

for pattern in "$TYPOGRAPHY" "$MEMORY_IDS"; do
    if ! grep -qE "$pattern" "$canary"; then
        echo "ABORT: the gate cannot detect a known-bad line. Pattern is broken, so a clean result"
        echo "       from it would mean nothing. Fix the pattern before releasing."
        exit 2
    fi
done
echo "canary: both patterns fire on known-bad input"

# The public surface: everything tracked except the test suite and CI workflows.
files=$(git ls-files -z | tr '\0' '\n' | grep -vE '^(test_|tests/|scripts/|\.github/)' || true)
if [ -z "$files" ]; then
    echo "ABORT: re-derived file list is EMPTY - refusing to report a clean gate over nothing."
    exit 2
fi
count=$(printf '%s\n' "$files" | wc -l | tr -d ' ')
echo "surface: $count file(s)"

status=0
for label_pattern in "typography:$TYPOGRAPHY" "memory-ids:$MEMORY_IDS"; do
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

[ "$status" -eq 0 ] && echo "GATES CLEAN over $count file(s)" || echo "GATES FAILED - do not tag"
exit "$status"

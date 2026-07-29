# Releasing

Releases are automated with GitHub Actions Trusted Publishing (OIDC). No API tokens are stored
anywhere. Pushing a version tag publishes to both PyPI and npm, with provenance attached.

## Cut a release

1. Bump the version to the same value in all THREE places:
   - `pyproject.toml` -> `[project] version`
   - `package.json` -> `"version"`
   - `kijito_inbox_monitor.py` -> `__version__`
   The third is easy to miss and this file used to omit it. It is not cosmetic: `__version__` builds
   the `User-Agent` the watcher sends, so leaving it behind makes every request report the previous
   release, and server-side logs then attribute traffic to a version that is not running.
   Verify with: `grep -n '^version\|"version"\|^__version__' pyproject.toml package.json kijito_inbox_monitor.py`
2. Add a section for the new version to `CHANGELOG.md`.
3. Run the pre-publish gates. BOTH must report clean, and the canary must prove the gate can still
   fire - a gate that cannot fail is worse than no gate, because it certifies:
   ```sh
   ./scripts/prepublish-gate.sh
   ```
   It re-derives the file list from `git ls-files` on purpose. A hardcoded list has been wrong twice,
   and PyPI/npm metadata is immutable per version, so the descriptions in `pyproject.toml` and
   `package.json` are exactly the text you cannot fix later.
4. Commit, tag, and push:
   ```sh
   git commit -am "release: vX.Y.Z"
   git tag -a vX.Y.Z -m "kijito-inbox-monitor vX.Y.Z"
   git push origin main --follow-tags
   ```
5. The tag triggers `.github/workflows/publish-pypi.yml` and `publish-npm.yml`. Both publish
   over OIDC, no tokens.
6. Confirm BOTH registries independently - never trust the workflow's own report, because a
   half-failure (one registry published, the other not) is the case that actually happens and a
   version can never be re-uploaded:
   ```sh
   gh run watch
   npm view kijito-inbox-monitor version
   curl -s https://pypi.org/pypi/kijito-inbox-monitor/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
   ```
7. Create the GitHub Release for the tag:
   ```sh
   gh release create vX.Y.Z --title vX.Y.Z --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
   ```
   That `sed` range is INCLUSIVE, so it trails the next version's heading into the notes. Strip the
   last line, or check the rendered release before you walk away.
8. ⚠️ **PUBLISHING DOES NOT UPDATE THE FLEET, AND AFTER STEP 2 A RESTART ALONE DOES NOTHING EITHER.**
   ⚠️ "STEP 2" HERE MEANS THE RECOVERY PLAN'S STEP 2 (repoint the launchd plist at a pinned artifact),
   NOT step 2 of this file - see the current-state pointer, sentinel ARGUS_CURRENT_STATE_POINTER_V1.
   Once the plist points at a pinned artifact, the producer no longer reads the working tree, so a green PyPI/npm publish AND a restart both leave it on the OLD bytes. You must REBUILD /
   REINSTALL the pinned artifact at the new version, repoint the plist, restart, and RE-VERIFY the health
   block (exactly one process, `launchctl list` status 0, no PINNED/CORRUPT state files, zero
   `bounded-window` alerts, heartbeat within ~2 min). `launchctl bootstrap` is not atomic - never assume
   it came back.
   ★ **THIS IS DONE-WHEN #7 AND THE RELEASE IS NOT COMPLETE WITHOUT IT.** Items 1-6 can all pass while the
   fleet's only mail producer still runs the PRE-RELEASE artifact.

## The producer runs a PINNED ARTIFACT - a restart deploys NOTHING

✔ DONE 2026-07-27. `com.kijito.inbox-monitor` executes a read-only artifact under
`~/.local/share/kijito-inbox-monitor/versions/<sha>/`, extracted with `git show <sha>:...` and checksum-
asserted equal to the commit; both `ProgramArguments[2]` and `WorkingDirectory` point there. Confirm with
`plutil -p ~/Library/LaunchAgents/com.kijito.inbox-monitor.plist`.
⚠️⚠️ **THE CONSEQUENCE INVERTS THE OLD RULE, AND THIS IS THE DANGEROUS PART.** Editing the working tree,
switching branches or committing changes NOTHING about what the fleet runs, and neither does a restart.
**Anyone acting on the old "a restart IS the deploy" rule will deploy nothing and believe they deployed** -
a no-op deploy and a successful one produce identical evidence (process up, status 0, heartbeat fresh,
mail flowing), because those are properties of whatever is running, not of what you intended to run.
★ TO DEPLOY: rebuild the artifact at the NEW sha, repoint BOTH plist paths, `bootout` -> wait for the pid
to VANISH (~50s) -> `bootstrap`, then **assert the running process is that sha**. State the requirement as a
PROPERTY, because the operator's own tooling is not part of this package:
    THE RUNNING ARGV MUST CARRY THE EXPECTED SHA. Nothing else settles it.
A self-contained check, which needs only a shell and a running producer:
```sh
sha=<new-sha>
procs=$(pgrep -f 'kijito_inbox_monitor\.py' || true)
n=$(printf '%s' "$procs" | grep -c . || true)
[ "$n" -eq 1 ] || { echo "FAIL: expected exactly 1 producer, found $n"; exit 1; }
ps -o command= -p "$procs" | grep -q "/versions/$sha/" \
  && echo "ok: the single running producer carries $sha" \
  || { echo "FAIL: the running argv does not carry $sha"; exit 1; }
```
⚠️ The process COUNT is asserted first and is not decoration: with an old and a new producer both alive, a
bare match on the expected sha SUCCEEDS while the fleet is still partly serving the old bytes. Verified in
all three directions (right sha, wrong sha, two processes) before being written down.
Health alone cannot tell a successful deploy from a no-op one; only naming the expected sha can.
⚠️ This file previously named a helper script by a RELATIVE PATH that pointed OUTSIDE the repository, so a
clone could not run the gate this document mandates - and the path would silently resolve to whatever
happened to sit above the checkout. The fleet operator's richer health tool lives in the private workspace
alongside this repo, deliberately outside it; it is not required to perform a release, and no public
instruction may depend on a path a clone does not contain.
Before touching launchctl: `bootout` + `bootstrap` (never `kickstart`), never back-to-back (they race ->
"Bootstrap failed: 5" leaving NO service), SIGTERM takes ~50s, copy the current plist aside as a rollback
FIRST, and announce the restart to the hive - this is the fleet's only mail producer.
⚠️ "Released artifact" does NOT mean a PyPI release. Reading it that way made the plan circular (repoint
needs a release -> release needs a GREEN audit -> the audit was meant to follow the repoint). Install the
audited SHA as a LOCAL versioned artifact: a built wheel, or a read-only checkout at the tag.
(This section previously said the producer "does not run this package (yet)" and "currently executes the
WORKING TREE directly", four lines after item 8 said the opposite. It was stale from the moment step 2
landed, and the stale half was the one that would cause a silent no-op deploy - re-audit 11, F4.)

## One-time setup (already done for 0.1.0)

- PyPI: a Trusted Publisher is configured for the project (this repo + `publish-pypi.yml` + the
  `pypi` environment).
- npm: a Trusted Publisher is configured for the package.
- GitHub: a `pypi` environment exists in repository settings.

## Notes

- A published version can never be re-uploaded. To fix a mistake, bump to the next patch version.
- npm cannot use OIDC for the very first publish of a brand-new package, so that one is manual;
  every version after it publishes over OIDC.
- Keep public-facing text free of em-dashes and internal references before tagging. That includes
  the README, the design doc, the script docstring and `--help` text, and the PyPI/npm
  descriptions, not just Markdown. Step 3 enforces this; the prose here is the rationale, not the
  check. A gate that lives only in prose does not run.
- Beware the shell when writing any gate by hand. `FILES=$(git ls-files); grep -nE ... $FILES` does
  NOT word-split in zsh: grep receives one nonexistent filename, exits non-zero, and an
  `|| echo clean` reports success while having inspected nothing. That exact false clean was
  observed in this repo. The script pipes NUL-delimited paths into `xargs -0` for this reason.

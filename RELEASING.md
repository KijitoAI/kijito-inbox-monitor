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

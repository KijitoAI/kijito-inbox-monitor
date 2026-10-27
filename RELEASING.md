# Releasing

Releases are published by GitHub Actions using Trusted Publishing (OIDC). No registry API tokens are
stored anywhere. Pushing a version tag publishes to both PyPI and npm, with provenance attached.

## Checklist

1. Bump the version to the same value in all three places:
   - `pyproject.toml` -> `[project] version`
   - `package.json` -> `"version"`
   - `kijito_inbox_monitor.py` -> `__version__` (it also builds the `User-Agent` the watcher sends)

   Verify with:
   ```sh
   grep -n '^version\|"version"\|^__version__' pyproject.toml package.json kijito_inbox_monitor.py
   ```
2. Add a section for the new version to `CHANGELOG.md`, and run the tests:
   ```sh
   python3 -m unittest test_kijito_monitor
   ```
3. Run the pre-publish gate. Every check must report `pass`, and the canary lines must show the gate can
   still fail:
   ```sh
   ./scripts/prepublish-gate.sh
   ```
   It inspects every tracked and untracked (non-ignored) file, including `pyproject.toml` and
   `package.json`, whose descriptions become immutable registry metadata once a version is published.
4. Commit, tag, and push:
   ```sh
   git commit -am "release: vX.Y.Z"
   git tag -a vX.Y.Z -m "kijito-inbox-monitor vX.Y.Z"
   git push origin main --follow-tags
   ```
5. The tag triggers `.github/workflows/publish-pypi.yml` and `.github/workflows/publish-npm.yml`.
6. Confirm both registries independently, since one can succeed while the other fails:
   ```sh
   npm view kijito-inbox-monitor version
   curl -s https://pypi.org/pypi/kijito-inbox-monitor/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
   ```
7. Create the GitHub Release for the tag, using the version's `CHANGELOG.md` section as the notes.

## Trusted publishing setup (one time)

- **PyPI:** on pypi.org, the project's Publishing settings name this repository, the workflow file
  `publish-pypi.yml`, and the environment `pypi`. The repository has a `pypi` environment in its
  GitHub settings.
- **npm:** on npmjs.com, the package's Trusted Publisher settings name the organization `KijitoAI`, the
  repository `kijito-inbox-monitor`, and the workflow filename `publish-npm.yml` (no environment), with
  `npm publish` allowed. These fields are matched exactly; a mismatch fails the publish with E404.
- npm cannot use OIDC for the very first publish of a new package, so that one is done by hand; every
  later version publishes over OIDC.

## Notes

- A published version can never be re-uploaded. To fix a mistake, release the next patch version.
- Keep public-facing text free of em-dashes and curly quotes, including `--help` text and the registry
  descriptions. The gate in step 3 enforces it.

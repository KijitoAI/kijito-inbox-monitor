# Releasing

Releases are automated with GitHub Actions Trusted Publishing (OIDC). No API tokens are stored
anywhere. Pushing a version tag publishes to both PyPI and npm, with provenance attached.

## Cut a release

1. Bump the version to the same value in both files:
   - `pyproject.toml` -> `[project] version`
   - `package.json` -> `"version"`
2. Add a section for the new version to `CHANGELOG.md`.
3. Commit, tag, and push:
   ```sh
   git commit -am "release: vX.Y.Z"
   git tag -a vX.Y.Z -m "kijito-inbox-monitor vX.Y.Z"
   git push origin main --follow-tags
   ```
4. The tag triggers `.github/workflows/publish-pypi.yml` and `publish-npm.yml`. Both publish
   over OIDC, no tokens.
5. Create the GitHub Release for the tag:
   ```sh
   gh release create vX.Y.Z --title vX.Y.Z --notes-file <(sed -n '/## \[X.Y.Z\]/,/## \[/p' CHANGELOG.md)
   ```

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
  descriptions, not just Markdown.

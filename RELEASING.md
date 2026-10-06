# Releasing

The `ci.yml` workflow runs tests and a Docker build without publishing on PRs
targeting `main` and pushes to `main`. The separate `release.yml` workflow runs
only on `v*` tags. To release, push a new semantic-version tag with a lowercase `v`
prefix, for example:

```sh
git tag v1.0.4
git push origin v1.0.4
```

The tag must include the workflow and `scripts/release_version.py`. The workflow
runs tests, publishes `ghcr.io/hacstream/stremio-addon:1.0.4`, and creates the GitHub
Release `v1.0.4` if it does not already exist. Images are published to GHCR only;
no Docker tar archive is exported or uploaded. Existing release notes are preserved,
and notes are generated only when creating a new release. It then updates the top-level
`version` in `addon/config.yaml` on `main` to `"1.0.4"`. The Docker build version
label also uses `1.0.4`. No `main` or `latest` Docker tag is published.

Both Dockerfiles persist `BUILD_VERSION` as the runtime `APP_VERSION` environment
variable. Stremio's manifest reads that value, so a `v1.0.4` release also reports
`"version": "1.0.4"` in the manifest. No manual Python version edit is needed.
Local runs and untagged builds report `0.0.0-dev` unless a valid semantic version
is provided. For a manual build, pass `--build-arg BUILD_VERSION=1.0.4`.

After release creation, the updater reads that GitHub Release's notes and adds
them under the unprefixed version heading in `addon/CHANGELOG.md` (creating the
file if needed). The config and changelog changes land atomically in one commit.
Older changelog entries and unrelated files are preserved. Re-running the release
updates its existing entry instead of duplicating it, including when the config
already has that version. An empty release body produces a version heading with
no invented notes. This synchronization runs in the tag release workflow; editing
release notes later requires re-running its publish job.

The automated config/changelog commit uses a dedicated GitHub App installation
token and includes `[skip ci]`, so it does not trigger CI. Other configuration or code edits still
receive normal CI. The release tag remains on its original commit.

The updater preserves unrelated configuration, retries concurrent edits, and
never downgrades the version advertised by Home Assistant. If the current
version is newer, it skips the older release. Re-running the release is safe for
both files.

Configure the release App before publishing:

- Store its client ID in the Actions variable `HAC_RELEASE_CI_APP_CLIENT_ID`.
- Store its entire private key in the Actions secret `HAC_RELEASE_CI_APP_PRIVATE_KEY`.
- Install the App on this repository with **Contents: Read and write** permission.
- Add the App to every applicable `main` ruleset bypass list with **Always allow**,
  so the updater can commit directly without a pull request. Any additional classic
  branch protections must also permit the App's update.

The App token is generated immediately before the version/changelog update and
is scoped to this repository by the token action's defaults. GHCR publishing and
GitHub Release creation continue to use the workflow's built-in `GITHUB_TOKEN`.
Rerunning a failed release uses the workflow from the original tag, so a tag
created before this change will not pick up the new authentication automatically.

Tags such as `v1.1.0-rc.1` are supported and map to `1.1.0-rc.1`. Build metadata
such as `+build.1` is rejected because `+` is not valid in Docker tags. Tags
without the `v` prefix do not start the release workflow.

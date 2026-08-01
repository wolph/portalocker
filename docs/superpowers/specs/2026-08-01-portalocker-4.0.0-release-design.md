# Portalocker 4.0.0 Release Design

## Goal

Publish portalocker 4.0.0 from the current `develop` branch through the
repository's signed release workflow without disturbing the dirty
`feature/modernize-4.0.0` checkout.

## Release source

The release source is the latest remote `develop`, which contains the 4.0.0
modernization and the subsequently merged fixes from pull requests #119 through
#125 and #130. The version in `pyproject.toml` is already `4.0.0`, and PyPI and
GitHub do not yet contain a `v4.0.0` release.

`master` has diverged from `develop`. The release helper merges its current
branch into `master`, so those branches must first be integrated and verified.
Release work happens in `.worktrees/release-4.0.0` on
`release/4.0.0-integration`; the primary checkout remains untouched.

## Integration design

Merge current `origin/master` into the integration branch based on current
`origin/develop`. Resolve all conflicts to the 4.0 `develop` versions:

- Keep `.github/workflows/lint.yml` and `.github/workflows/main.yml` deleted;
  their responsibilities moved to the consolidated 4.0 workflows.
- Keep the 4.0 AppVeyor configuration, including the Python 3.10-3.14 matrix and
  managed 64-bit uv interpreters.
- Keep the 4.0 exception behavior and modern type syntax.
- Keep the 4.0 `pyproject.toml` metadata and repository-review configuration.
- Keep the 4.0 tox environment layout and basedpyright checks.

Non-conflicting master-only updates remain in the merge result. The merge is
published as a pull request to `develop`, and it is merged only after GitHub CI,
CodeQL, and AppVeyor pass. This makes `master` an ancestor of `develop`, so the
release helper's later signed `--no-ff` merge is conflict-free.

## Release execution

Immediately before release, refresh and verify the remote refs, confirm that
`v4.0.0` is absent from Git, GitHub Releases, and PyPI, verify the release
worktree is clean, and verify the configured GPG secret key and GitHub
authentication.

Use the complete `4.0.0` section of `CHANGELOG.rst` as one multiline release
message. Invoke `/Users/rick/bin/build_and_upload_release` from updated
`develop` with that message and no version-bump argument. The helper runs local
gates, creates the signed release merge and signed tag, fast-forwards `develop`
back to the release commit, builds and signs artifacts, pushes both branches and
the tag, triggers Trusted Publishing, and creates the GitHub Release.

The helper does not wait for Trusted Publishing. Completion requires monitoring
the tag-triggered workflow and independently verifying GitHub, Git, GPG, and
PyPI state.

## Failure handling

- Before any push, stop on failure and preserve the clean worktree for
  diagnosis. No public release state exists yet.
- After the tag push, do not delete, move, or recreate the public tag or release.
  Diagnose the publishing run first and choose recovery based on whether PyPI
  accepted any files.
- Do not fall back to an unverified local PyPI upload. Trusted Publishing is the
  expected publication path.
- Remove the temporary worktree and merged integration branch only after all
  release verification succeeds.

## Verification

Before the integration pull request, run the repository's static-analysis,
documentation, and test environments, build both package artifacts and the
combined module, validate distribution metadata, and smoke-test an isolated
wheel installation. Require all pull-request checks and AppVeyor to pass.

After the helper runs, verify that remote `master` and `develop` converge on the
signed release commit, annotated `v4.0.0` peels to that commit, GitHub Release
assets include the wheel, sdist, combined module, and wheel/sdist signatures,
both signatures verify, PyPI exposes 4.0.0, and a clean installation imports as
version 4.0.0.

## Public interfaces

This release operation introduces no additional public API or schema changes.
It publishes the API and behavioral changes already documented in the existing
4.0.0 changelog.

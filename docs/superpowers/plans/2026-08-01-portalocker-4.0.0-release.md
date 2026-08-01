# Portalocker 4.0.0 Release Implementation Plan

**For agentic workers:** REQUIRED SUB-SKILL: Use
`superpowers:executing-plans` to execute this plan task-by-task.

**Goal:** Integrate `master` into `develop`, publish signed portalocker 4.0.0
with `build_and_upload_release`, and verify GitHub and PyPI.

**Architecture:** Work only in the isolated
`.worktrees/release-4.0.0` checkout. First make `master` an ancestor of
`develop` through a checked integration pull request. Then run the existing
release helper from updated `develop`; treat its exit as the start of remote
publication verification, not completion.

**Tech Stack:** Git, GitHub CLI, uv, tox, pytest, GnuPG, GitHub Actions,
AppVeyor, PyPI Trusted Publishing.

---

### Task 1: Integrate master into the release branch

**Files:**

- Keep deleted: `.github/workflows/lint.yml`
- Keep deleted: `.github/workflows/main.yml`
- Resolve to develop: `appveyor.yml`
- Resolve to develop: `portalocker/exceptions.py`
- Resolve to develop: `pyproject.toml`
- Resolve to develop: `tox.toml`
- Preserve: `docs/superpowers/specs/2026-08-01-portalocker-4.0.0-release-design.md`
- Preserve: `docs/superpowers/plans/2026-08-01-portalocker-4.0.0-release.md`

- [ ] **Step 1: Refresh and validate source refs**

Run:

```bash
git fetch --prune origin master develop
git status --short --branch
git rev-parse origin/master origin/develop
git ls-remote --heads --tags origin master develop \
  'release/4.0.0-integration' 'v4.0.0'
```

Expected: the worktree is clean, the remote integration branch and tag are
absent before publication, and fetched refs match `git ls-remote`.

- [ ] **Step 2: Merge master and confirm the expected conflicts**

Run:

```bash
git merge --no-ff -S origin/master \
  -m 'Merge master into develop before v4.0.0'
git diff --name-only --diff-filter=U
```

Expected: merge stops with conflicts only in the two deleted legacy workflows,
`appveyor.yml`, `portalocker/exceptions.py`, `pyproject.toml`, and `tox.toml`.

- [ ] **Step 3: Apply the approved conflict policy**

Run:

```bash
git rm .github/workflows/lint.yml .github/workflows/main.yml
git checkout --ours -- appveyor.yml portalocker/exceptions.py \
  pyproject.toml tox.toml
git add appveyor.yml portalocker/exceptions.py pyproject.toml tox.toml
git diff --name-only --diff-filter=U
git diff --cached --check
git status --short
```

Expected: no unmerged paths remain; the 4.0 versions are staged while
non-conflicting master updates remain included.

- [ ] **Step 4: Commit the signed integration merge**

Run:

```bash
git commit -S -m 'Merge master into develop before v4.0.0'
git verify-commit HEAD
git log -1 --show-signature --oneline
git merge-base --is-ancestor origin/master HEAD
```

Expected: signed merge commit verifies and contains `origin/master` as an
ancestor.

### Task 2: Verify the integrated release candidate locally

**Files:**

- Verify: `pyproject.toml`
- Verify: `tox.toml`
- Verify: `CHANGELOG.rst`
- Generate ignored artifacts: `build/`, `dist/`, `.venv/`, `.tox/`

- [ ] **Step 1: Run test and static-analysis gates**

Run:

```bash
uv sync --all-extras
uv run pytest
uv run tox -e ruff,mypy,basedpyright,pyrefly,ty,codespell,repo-review,docs
uv run pyright
```

Expected: pytest reports 100% coverage; every requested tox environment and the
helper's standalone Pyright gate pass.

- [ ] **Step 2: Build and validate release artifacts**

Run:

```bash
rm -rf build dist
uv build
uv run build_extra.py
uvx twine check dist/portalocker-4.0.0-py3-none-any.whl \
  dist/portalocker-4.0.0.tar.gz
ls -l dist/portalocker-4.0.0-py3-none-any.whl \
  dist/portalocker-4.0.0.tar.gz dist/portalocker-4.0.0.py
```

Expected: wheel, sdist, and combined module exist; both package artifacts pass
Twine metadata checks.

- [ ] **Step 3: Smoke-test an isolated wheel installation**

Run:

```bash
smoke_dir=$(mktemp -d)
uv venv "$smoke_dir"
uv pip install --python "$smoke_dir/bin/python" \
  dist/portalocker-4.0.0-py3-none-any.whl
"$smoke_dir/bin/python" -c \
  "import portalocker; assert portalocker.__version__ == '4.0.0'"
python dist/portalocker-4.0.0.py --help
```

Expected: installed package reports 4.0.0 and the combined module starts
successfully.

- [ ] **Step 4: Confirm only intended tracked changes exist**

Run:

```bash
git status --short --branch
git diff --check origin/develop...HEAD
git diff --stat origin/develop...HEAD
```

Expected: only the two documentation files, signed merge, and automatically
merged master changes differ from `origin/develop`; build outputs remain
ignored.

### Task 3: Publish and merge the integration pull request

**Files:** No additional repository files.

- [ ] **Step 1: Push the integration branch explicitly**

Run:

```bash
git push -u origin release/4.0.0-integration
git ls-remote --heads origin release/4.0.0-integration
```

Expected: remote branch SHA equals local `HEAD`.

- [ ] **Step 2: Open the integration pull request**

Run:

```bash
gh pr create --repo wolph/portalocker \
  --base develop \
  --head release/4.0.0-integration \
  --title 'Integrate master for portalocker v4.0.0' \
  --body $'## Summary\n\n- integrate current master into develop before v4.0.0\n- retain the 4.0 workflow, AppVeyor, typing, metadata, and tox designs\n- add the reviewed release design and execution plan\n\n## Verification\n\n- uv run pytest\n- uv run tox -e ruff,mypy,basedpyright,pyrefly,ty,codespell,repo-review,docs\n- uv run pyright\n- uv build and twine check\n- isolated wheel and combined-module smoke tests'
```

Expected: a non-draft pull request targeting `develop` is created.

- [ ] **Step 3: Wait for every remote check**

Run:

```bash
pr_number=$(gh pr view --repo wolph/portalocker \
  --json number --jq .number)
gh pr checks "$pr_number" --repo wolph/portalocker --watch
gh pr view "$pr_number" --repo wolph/portalocker \
  --json mergeStateStatus,reviewDecision,statusCheckRollup
```

Expected: GitHub CI, CodeQL, and AppVeyor are successful and merge state is
clean. Stop and diagnose any failure before merging.

- [ ] **Step 4: Merge and verify develop**

Run:

```bash
gh pr merge "$pr_number" --repo wolph/portalocker \
  --merge --delete-branch
git fetch --prune origin master develop
git checkout develop
git merge --ff-only origin/develop
git merge-base --is-ancestor origin/master develop
git status --short --branch
```

Expected: PR is merged, local `develop` matches `origin/develop`, contains
`origin/master`, and is clean.

### Task 4: Run the signed release helper

**Files:**

- Read release notes: `CHANGELOG.rst`
- Generate ignored artifacts: `build/`, `dist/`
- External writes: `master`, `develop`, `v4.0.0`, GitHub Release, PyPI workflow

- [ ] **Step 1: Perform irreversible-action preflight**

Run:

```bash
git fetch --prune origin master develop
git status --porcelain
git rev-parse HEAD origin/develop origin/master
git merge-base --is-ancestor origin/master HEAD
git ls-remote --tags origin v4.0.0
gh release view v4.0.0 --repo wolph/portalocker
curl -fsSL https://pypi.org/pypi/portalocker/json \
  | jq -e '.releases["4.0.0"] | length == 0'
key_id=$(git config --get user.signingkey)
gpg --batch --list-secret-keys "$key_id"
gh auth status
release_helper=$(command -v build_and_upload_release)
test -n "$release_helper"
zsh -n "$release_helper"
```

Expected: clean worktree on `develop`; `master` is an ancestor; tag, GitHub
Release, and PyPI files are absent; signing key and GitHub auth are available;
the requested helper resolves from `PATH` and has valid syntax. `gh release
view` must fail with release-not-found.

- [ ] **Step 2: Extract and inspect the exact release message**

Run:

```bash
release_message=$(awk '
  /^4[.]0[.]0:$/ { capture = 1 }
  capture && /^For newer changes/ { exit }
  capture { print }
' CHANGELOG.rst)
test -n "$release_message"
test "${release_message%%$'\n'*}" = '4.0.0:'
printf '%s\n' "$release_message"
```

Expected: the complete 4.0.0 changelog section is shown and no older release
history is included.

- [ ] **Step 3: Execute the approved helper without a bump argument**

Run in the same zsh process that defines `release_helper` and
`release_message`:

```bash
"$release_helper" "$release_message"
```

Expected: helper reports `Done: v4.0.0`, after passing local gates, signing the
merge/tag/artifacts, pushing `develop`, `master`, and `v4.0.0`, and creating the
GitHub Release. This is not yet completion because Trusted Publishing is
asynchronous.

### Task 5: Verify publication and clean temporary state

**Files:** No tracked file changes.

- [ ] **Step 1: Monitor Trusted Publishing to success**

Run:

```bash
release_sha=$(git rev-parse 'v4.0.0^{}')
run_id=''
for attempt in {1..30}; do
  run_json=$(gh run list --repo wolph/portalocker \
    --workflow publish.yml --event push --limit 20 \
    --json databaseId,headSha)
  run_id=$(printf '%s' "$run_json" \
    | jq -r --arg sha "$release_sha" \
      '.[] | select(.headSha == $sha) | .databaseId' \
    | head -1)
  test -n "$run_id" && break
  sleep 10
done
test -n "$run_id"
gh run watch "$run_id" --repo wolph/portalocker --exit-status
gh run view "$run_id" --repo wolph/portalocker \
  --json status,conclusion,url,jobs
```

Expected: tag-triggered test, build, and publish jobs all conclude successfully.

- [ ] **Step 2: Verify Git refs and signatures**

Run:

```bash
git fetch --prune --tags origin master develop
master_sha=$(git rev-parse origin/master)
develop_sha=$(git rev-parse origin/develop)
tag_sha=$(git rev-parse 'v4.0.0^{}')
test "$master_sha" = "$develop_sha"
test "$master_sha" = "$tag_sha"
git verify-tag v4.0.0
git verify-commit "$tag_sha"
```

Expected: both remote branches and peeled tag point to the same signed release
commit; tag and commit signatures verify.

- [ ] **Step 3: Verify GitHub Release assets and artifact signatures**

Run:

```bash
gh release view v4.0.0 --repo wolph/portalocker \
  --json tagName,isDraft,isPrerelease,publishedAt,url,assets
asset_dir=$(mktemp -d)
gh release download v4.0.0 --repo wolph/portalocker --dir "$asset_dir"
gpg --verify "$asset_dir/portalocker-4.0.0-py3-none-any.whl.asc" \
  "$asset_dir/portalocker-4.0.0-py3-none-any.whl"
gpg --verify "$asset_dir/portalocker-4.0.0.tar.gz.asc" \
  "$asset_dir/portalocker-4.0.0.tar.gz"
test -f "$asset_dir/portalocker-4.0.0.py"
```

Expected: published non-draft, non-prerelease GitHub Release contains all five
artifacts and both detached signatures verify.

- [ ] **Step 4: Verify PyPI and a clean install**

Run:

```bash
curl -fsSL https://pypi.org/pypi/portalocker/4.0.0/json \
  | jq -e '.info.version == "4.0.0" and (.urls | length == 2)'
pypi_smoke_dir=$(mktemp -d)
uv venv "$pypi_smoke_dir"
uv pip install --python "$pypi_smoke_dir/bin/python" \
  'portalocker==4.0.0'
"$pypi_smoke_dir/bin/python" -c \
  "import portalocker; assert portalocker.__version__ == '4.0.0'"
```

Expected: PyPI has the wheel and sdist, and an index installation imports as
4.0.0.

- [ ] **Step 5: Remove only merged temporary release state**

From the primary checkout, after every previous step succeeds, run:

```bash
git worktree remove .worktrees/release-4.0.0
git branch -d release/4.0.0-integration
git status --short --branch
```

Expected: temporary worktree and merged local integration branch are removed;
the primary `feature/modernize-4.0.0` checkout retains all original local
changes.

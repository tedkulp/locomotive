---
name: release
description: Use when cutting a release of locomotive: running the gate, bumping the version, moving the changelog's Unreleased entries into a dated section, tagging, and pushing so the release and Docker workflows publish. Covers what the tag triggers and how to verify it landed.
---

# Releasing locomotive

A release is a pushed `v*` tag. Two workflows run on it:

- `.github/workflows/release.yml` builds `locomotive-X.Y.Z.tar.gz` (a `git archive` of the tag) and `checksums.txt`, and publishes a GitHub Release whose notes are the tag's `CHANGELOG.md` section.
- `.github/workflows/docker.yml` publishes `ghcr.io/tedkulp/locomotive:X.Y.Z` and `:X.Y` for linux/amd64 and linux/arm64, and moves `:latest` unless it is a pre-release.

Everything after the push is automatic; this skill is the part that is not.

Work through the steps in order. **A pushed tag cannot be taken back**: a re-tag republishes over a release and image tags people may already have pulled.

## Step 1: Check the tree

```sh
git status --short
git fetch && git status -sb | head -1
```

Release from `main`, clean and up to date with `origin/main`. Stop and ask if there are uncommitted changes, the branch is not `main`, or it is ahead/behind.

## Step 2: Run the gate

```sh
uv run pytest
(cd frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build)
```

Both must pass; do not tag on red.

## Step 3: Decide the version

```sh
git tag --sort=-version:refname | head -5
git log --oneline $(git describe --tags --abbrev=0)..HEAD
```

Semver against the last tag:

- **Patch** (`0.1.4 → 0.1.5`): fixes only.
- **Minor** (`0.1.x → 0.2.0`): new features, backwards-compatible.
- **Major** (`0.x.x → 1.0.0`): a break in settings, the database (a migration users cannot roll back), the HTTP API or the MCP tools users rely on.

If the user passed a version as the argument, use it. Otherwise propose one with the reasoning and confirm before tagging.

## Step 4: Bump the version

```sh
uv version X.Y.Z
```

Updates `pyproject.toml` and `uv.lock`. `release.yml` refuses to publish when `pyproject.toml`'s version is not the tag's.

## Step 5: Move the changelog's Unreleased entries

`CHANGELOG.md` follows Keep a Changelog. Add a dated section directly below `## [Unreleased]`, and leave `## [Unreleased]` in place and empty:

```markdown
## [Unreleased]

## [0.2.0] - 2026-12-01

### Added
- ...
```

Heading format: `## [0.2.0] - YYYY-MM-DD`, bracketed, no leading `v`, hyphen. Categories: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`.

If Unreleased is missing things, fill it from the commit log in Step 3, written for users in `GLOSSARY.md` vocabulary (Run, Playbook, Working Copy, Schedule, Tag…), not as commit subjects. Leave out `docs:`, `test:`, `chore:` and refactors with no visible effect.

Update the compare links at the bottom of the file: point `[Unreleased]` at `vX.Y.Z...HEAD` and add a `[X.Y.Z]` line comparing against the previous tag.

Use today's real date:

```sh
date -u +%Y-%m-%d
```

## Step 6: Commit

```sh
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "Release vX.Y.Z: <one-line summary of what changed>"
```

Commit before tagging, so the tag contains the changelog and version it describes.

## Step 7: Tag and push both

```sh
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push && git push origin vX.Y.Z
```

`git push` alone does **not** carry the tag. The second command starts the release.

## Step 8: Verify

```sh
gh run list --limit 4
gh run watch <run-id>
gh release view vX.Y.Z
```

Expect `locomotive-X.Y.Z.tar.gz` and `checksums.txt`, with the changelog section as the notes. Then confirm the image:

```sh
docker manifest inspect ghcr.io/tedkulp/locomotive:X.Y.Z | grep architecture
```

Expect `amd64` and `arm64`.

## When it goes wrong

- **release.yml fails at "Check version and changelog".** The version or the changelog heading does not match the tag. Nothing was published by that job, but `docker.yml` has already pushed the image tags. Fix it on `main`, then move the tag (`git tag -d vX.Y.Z && git push origin :vX.Y.Z`, re-tag, push); the Docker job re-pushes the same image tags.
- **The release published with a mistake.** Do not re-tag. Fix it and release a new patch version.

## Quick reference

| Step | Command |
|---|---|
| Gate | `uv run pytest` and `pnpm typecheck && pnpm build` in `frontend/` |
| Last tags | `git tag --sort=-version:refname \| head -5` |
| Bump | `uv version X.Y.Z` |
| Today, in UTC | `date -u +%Y-%m-%d` |
| Commit | `git commit -m "Release vX.Y.Z: <summary>"` |
| Tag | `git tag -a vX.Y.Z -m "Release vX.Y.Z"` |
| Push both | `git push && git push origin vX.Y.Z` |
| Watch | `gh run list --limit 4` |
| Inspect | `gh release view vX.Y.Z` |

# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- A changelog, and a GitHub Release with a source tarball and checksums for every `v*` tag.

## [0.1.5] - 2026-11-28

### Added
- Multi-arch (linux/amd64, linux/arm64) Docker images on GHCR. Pushes to `main` publish `:dev` and `:sha-<short>`; release tags publish `:X.Y.Z`, `:X.Y` and `:latest`, and only non-pre-release versions move `:latest`. Compose runs the published image by default (`LOCOMOTIVE_TAG` picks the image tag) and still builds locally with `--build`.

## [0.1.4] - 2026-11-28

### Added
- Descriptions for Playbooks and Tags. A Playbook's Description is the block of `#` lines at the top of its file; Tag Descriptions come from the `tags:` map in `locomotive.yml` at the repo root. Launch shows summaries in the sidebar, the full Description above Hosts, and Tags as a full-width list. `GET /api/playbooks` gains `description` and `summary`; previews and MCP `list_tags` gain `tag_descriptions`.
- `docs/describing-an-ansible-repo.md`: instructions for adding Descriptions and a `locomotive.yml` to an existing Ansible repo.

## [0.1.3] - 2026-11-27

### Changed
- Picking the same Playbook again at the same commit reuses the last clean preview instead of re-running the preview helper (about 10s). Working Copy previews are never reused.

### Security
- The Scheduler Ping's healthchecks URL, a bearer secret, no longer appears in the container log: httpx request lines are kept out of it.

## [0.1.2] - 2026-11-26

### Added
- Click a task in the Run page's task × host grid to jump to its Banner in the log.

### Fixed
- The live log and task grid keep following new output, and stop following as soon as you scroll up.
- The log and grid panes take focus, so arrow keys and Page Up scroll them.
- Task rows link to their Banner when Ansible defers the Banner to the first result (`display_skipped_hosts` or `display_ok_hosts` off).

## [0.1.1] - 2026-11-26

### Fixed
- Docker image: uid 1000 has a passwd entry, so ssh starts and Runs no longer come back unreachable.

## [0.1.0] - 2026-11-26

### Added
- First release: a web app plus MCP server that runs Ansible Playbooks from one repo.
- Clean Runs from a commit and Working Copy Runs in place, with a live task × host grid, recap, host-filtered log, Stop/Kill and Run again.
- Launch form with host picker, Tags, options and host/task previews.
- Per-host queue: conflicting Runs wait, start fairly, can be cancelled and survive restarts.
- Schedules: cron firings of clean Runs with skipped and missed entries, DST handling, pause all and a Scheduler Ping.
- MCP server at `/mcp` with bearer tokens, read tools, `start_run`, tail and wait.
- ntfy notifications for failed, interrupted, missed and scheduled-with-changes Runs.
- History of every Run, log retention and housekeeping.
- Access guard: trusted-proxy peer check and identity allowlist.

[Unreleased]: https://github.com/tedkulp/locomotive/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/tedkulp/locomotive/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/tedkulp/locomotive/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/tedkulp/locomotive/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/tedkulp/locomotive/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/tedkulp/locomotive/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tedkulp/locomotive/releases/tag/v0.1.0

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues on tedkulp/locomotive, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Changelog

`CHANGELOG.md` follows Keep a Changelog. Every user-visible change goes under `## [Unreleased]`, grouped by category, in the same PR as the change. Only release work moves those entries into a dated version section; see the `release` skill (`.claude/skills/release/SKILL.md`).

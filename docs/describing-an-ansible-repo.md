# Adding Descriptions to an Ansible repo

Instructions for an agent (or a person) adding locomotive Descriptions to an existing Ansible repo. A
**Description** is plain text that says what a Playbook or Tag is for. locomotive shows it on its Launch
screen, so whoever picks what to run doesn't have to remember what `site.yml` or the `hardening` tag does.

There are two kinds, and each lives in a different place:

| What | Where it lives | Shown as |
| --- | --- | --- |
| Playbook Description | a comment block at the top of each `playbooks/*.yml` file | first paragraph under the Playbook name; full text above the Hosts card |
| Tag Description | the `tags:` map in `locomotive.yml` at the repo root | muted text beside the tag's checkbox, cut to one line, full text on hover |

Neither changes how Ansible runs. Comments are ignored by Ansible, and Ansible never reads `locomotive.yml`.

## 1. Playbook Descriptions

Describe every top-level `*.yml` / `*.yaml` file directly inside `playbooks/`. Don't describe files in
subdirectories, roles, or task files: locomotive doesn't list them.

### Format

```yaml
---
# Base setup for every server: packages, users, SSH keys and time sync.
#
# Safe to re-run. Run it after provisioning a new host, before any
# service-specific Playbook.
- name: Base
  hosts: servers
  roles:
    - common
```

The rules, which match locomotive's parser exactly:

- The block has to come **before any YAML content**. Above it you may have only blank lines, one `---`
  and tool directive comments (`# yaml-language-server: ...`, `# vim: ...`, `# -*- ... -*-`). Directives are
  never shown, whether they sit above the block or inside it.
- The block is the run of **consecutive** `#` lines. A **blank line ends the block**, so separate
  paragraphs with a bare `#` line, never an empty line.
- The `# ` or `#` prefix is stripped, along with leading and trailing spaces.
- Lines in the same paragraph are joined with single spaces, so wrap lines wherever you like. Indentation,
  bullet layout and single line breaks are **not** kept. Only paragraph breaks are.
- The **first paragraph is the summary**, shown in the Playbook list and cut to 2 lines there. Keep it to
  one sentence, under ~120 characters. The hard cap is 200.
- The whole Description is capped at 2000 characters. Aim for 1 to 3 short paragraphs.
- It's plain text. Markdown is shown literally, so don't use `**bold**`, backticks or headings.

### If a Playbook already starts with comments

The existing top comment block **becomes** the Description. Read it before you add anything:

- If it already describes the Playbook, tidy it to fit the rules above (summary first, `#` between
  paragraphs). Don't add a second block.
- If it's something else (a license header, a changelog, commented-out YAML, a `TODO`), move that text
  below the first YAML line or into its own block after a blank line, then put the Description at the top.
  Only the first block counts, and a blank line ends it.

### What to write

Read the Playbook, and the roles and task files it pulls in, before you write anything. Say:

1. **What it does to the hosts**, in concrete words: "installs Docker and the compose plugin", not
   "configures the Docker stack".
2. **Which hosts it targets**, if the `hosts:` pattern doesn't make that obvious.
3. **Anything the operator must know before running it**: it reboots hosts, it's destructive, it needs
   extra vars (name them), it must run after another Playbook, or check mode doesn't work for it.

Leave out restated task lists, author names, dates and anything that will go stale with the next commit.

## 2. Tag Descriptions

### Find the tags

A Tag's Description is repo-wide: one tag means the same thing in every Playbook. Collect every tag the
Playbooks can select:

```sh
grep -rn --include='*.yml' --include='*.yaml' -E '^\s*tags:' playbooks roles
```

Tags can be set on plays, roles (`roles: - { role: x, tags: [...] }`), blocks, tasks, and
`import_*` / `include_*` statements. They can be written as a list (`tags: [a, b]`), a block list, or a
single string (`tags: a`). Read each hit rather than trusting a regex. Ignore tags inside commented-out
code.

`always` and `never` are special Ansible tags. Describe them only if the repo relies on them in a way an
operator should know about.

### Format

Create `locomotive.yml` at the repo root, next to `playbooks/` (not inside it):

```yaml
# Descriptions locomotive shows on its Launch screen. Ansible ignores this file.
tags:
  docker: Install Docker and the compose plugin, and add the deploy user to the docker group
  hardening: "SSH lockdown, firewall rules and sysctl settings. May drop your SSH session: run with --check first"
  packages: Base apt packages only, no services restarted
```

The rules:

- Use the top-level key `tags:`, holding a map of **tag name → string**.
- Every value must be a **string**. Quote any value that contains `: `, starts with a special character
  (`*`, `&`, `!`, `[`, `{`, `%`, `@`, a backtick, a quote) or could load as something else (`yes`, `no`, `on`,
  `3`, `2024-01-01`). A value that isn't a string is skipped and shows a warning on the Launch screen.
- Write one line per tag: it's shown truncated to one line, with the full text on hover. Aim for under ~100
  characters. The cap is 2000.
- It's plain text, with no Markdown.
- A described tag that no Playbook uses is harmless (it's never shown). A used tag with no Description
  still shows, just without text. So it's fine to leave a tag out if you can't say anything useful about it.
- If `locomotive.yml` already exists, keep its other keys and merge into its `tags:` map.

### What to write

Say **what selecting this tag alone would do**: which tasks it limits the Run to, and any side effects
(restarts, reboots, dropped connections). If one tag means different things in different Playbooks, write
the common meaning and mention the inconsistency to the person who asked for this work. Don't silently
pick one meaning.

## 3. Check your work

1. Make sure every Playbook still parses as before. If `ansible-playbook` is available:
   `ansible-playbook --syntax-check playbooks/<name>.yml` for each one you edited.
2. Make sure `locomotive.yml` is valid YAML whose `tags` values are all strings:

   ```sh
   python3 -c 'import yaml; t = yaml.safe_load(open("locomotive.yml"))["tags"]; bad = [k for k, v in t.items() if not isinstance(v, str)]; print(bad or "ok")'
   ```

3. Re-read each Playbook's top lines and confirm that the first `#` paragraph alone reads well as the
   summary.
4. Commit and push. A clean Run, and its Descriptions, come from the pushed branch, so locomotive only shows
   them once they're pushed. The Working Copy shows uncommitted edits straight away, which is handy for
   previewing.

Change only comments and `locomotive.yml`. Never edit YAML content, rename tags, or move files as part of
this work.

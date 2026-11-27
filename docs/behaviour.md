# How locomotive behaves

The detailed rules behind each feature. The [README](../README.md) covers setup; terms in capitals
(Run, Playbook, Working Copy, Schedule, Scheduler Ping) are defined in [GLOSSARY.md](../GLOSSARY.md).

## Access guard

locomotive has no login page. It accepts connections only from `LOCOMOTIVE_TRUSTED_PROXY_IPS` (the
reverse proxy; addresses or CIDRs) and takes the user from `LOCOMOTIVE_IDENTITY_HEADER`, which must be on
`LOCOMOTIVE_ALLOWLIST`. Missing identity is a plain 401, an unlisted one 403; any other peer gets 403.
`/mcp` ignores identity headers and uses bearer tokens instead (see [MCP](#mcp)). Unset values fail
closed.

## Runs

Each clean Run exports the resolved commit, runs `mise install` against that checkout's `mise.toml`,
installs `collections/requirements.yml` into a cache keyed by its sha256, resolves its exact host set, then
runs `ansible-runner` (which must be provided by the checkout's mise toolchain) with the launch options.
Clean Runs take a `branch` (default `LOCOMOTIVE_DEFAULT_BRANCH`; `GET /api/branches` lists the remote's).

### Working Copy Runs

Working Copy Runs (`checkout: "working_copy"`, needs `LOCOMOTIVE_WORKING_COPY_PATH`) run in place in that
clone, dirty files and all, with its own `mise.toml`. Each records HEAD, a dirty flag, `git diff HEAD`
(capped at 1 MB, `GET /api/runs/<id>/working-copy-diff`) and the untracked files.

locomotive only reads the Working Copy (without git's optional locks) and runs in it: it never commits,
pulls, fetches or pushes it, so the ahead/behind shown by `GET /api/working-copy` is against its upstream
as last fetched there. In Docker, mount the clone at the same path inside the container, read-write.

### Per-host queue

Two Runs never touch the same host at once (whatever started them, check mode included).

A Run is `queued` from launch until it holds its hosts: it fetches and fixes its SHA (a failed fetch fails
it at once), resolves its exact host set, then starts when no running Run holds one of its hosts and no
earlier-launched queued Run shares one (so no Run is starved). Meanwhile its `stage` is `preparing`
(fetching, installing, resolving hosts) or `waiting` (for hosts, shown in `waiting_for`), and null once it
leaves the queue; `POST /api/runs/{id}/cancel` takes it out of the queue.

Queued Runs survive a restart with the SHA and host set they were queued with. A Working Copy Run
re-resolves its hosts as it starts (first preparing its toolchain again if its `mise.toml` or
`collections/requirements.yml` changed while it was queued) and waits again on a new overlap.

### History

`GET /api/runs?limit=<n>&before=<id>` pages newest first (`next_before` is the next page's cursor). The
History page loads older pages on demand and shows the reason of each skipped, missed, interrupted, failed
or canceled entry.

### Run page

The Run page streams the log (SSE) under a task x host grid; clicking a host header filters the log to
that host's lines (lines with no host, such as banners, always show).

- **Follow:** the log pane and the grid stay pinned to their bottom as output arrives. Follow stops only
  when the reader scrolls up (a wheel, touch, ArrowUp/PageUp/Home in a focused pane, or the scroll
  position moving up); new output, however much arrives at once, never stops it, and dragging the
  scrollbar is never fought. Scrolling back down to the bottom by hand, or the log's "↓ follow" button,
  resumes it. The grid follows only while the Run is live.
- **Jump to a task:** clicking a task name in the grid scrolls the log to that task's banner, centres and
  briefly highlights it, and turns follow off (as a scroll up would; "↓ follow" resumes, and landing at
  the bottom doesn't). Same-named tasks jump to their own banners. Under a host filter it still lands on
  the banner, which names no host.
- A task name is not clickable when its banner isn't in the log; its tooltip says why: "Not in the log:
  log truncated" (banner past the cap), "Not in the log yet" (live, banner not written yet), "Not in the
  log yet: still loading" (written but not streamed in yet), or "Not in the log: this Run's grid predates
  links to the log".

Each grid task row (`GET /api/runs/<id>/grid` and the SSE `grid` event) carries `log_seq`, the `seq` of
its banner line in the log. It is null when the banner fell past `LOCOMOTIVE_LOG_CAP_BYTES`, when no
banner was printed, and for grids saved before the field existed. The banner is usually written by the
task-start event, but with `display_skipped_hosts` or `display_ok_hosts` off Ansible defers it to the
task's first shown result, and that is linked too.

## Previews

Previews (`POST /api/preview`) and each Run's host set come from `src/locomotive/preview_helper.py`, run
with the Python interpreter of the checkout's own ansible-core (never imported by the app). It emits names
only (groups, hosts, plays, tasks, tags), never host vars, and `ansible-inventory` is never used.

The first preview of a commit or Working Copy prepares its Ansible runtime (`mise install`, collections),
which can outlast the preview time limit: the preparation carries on in the background and the preview
answers `503` with `Retry-After: 2` until it is ready (the launch form keeps asking and says so).

A clean preview's reply is remembered in memory per commit and helper request (Playbook, limit, tags,
extra vars, expand includes), so picking the same Playbook again at the same commit answers at once.
Check, diff and verbosity only change the `command`, so they reuse the reply. Failed previews are not
remembered, each commit keeps its 16 most recently used replies and drops them with its prepared runtime, and Working Copy previews always
ask the helper, since the files can change on disk without a commit.

## Schedules

Schedules (`/api/schedules`, the Schedules page) are saved Run settings (Playbook, limit, tags,
check/diff, verbosity, extra vars, branch; empty branch = the default branch) with a five-field cron
expression read in `LOCOMOTIVE_TIME_ZONE` (unset: `TZ`, then `/etc/localtime`, then UTC).

- The scheduler loop (every `LOCOMOTIVE_SCHEDULER_INTERVAL_SECONDS`, default 30) looks at cron firings
  after each Schedule's `last_fired_at`. Each firing gives one Run with source `schedule`, labelled with
  its Schedule, always a clean export (never the Working Copy).
- If that Schedule's previous Run is still queued or running, the firing leaves a `skipped` history entry
  with the reason instead.
- Firings missed while locomotive was down collapse into one Run if the newest is at most 1h old;
  otherwise one `missed` entry is recorded (no Run).
- DST: a skipped wall-clock time doesn't fire; a repeated one fires once (its first occurrence).
- Disabling a Schedule or pausing all of them (`PUT /api/scheduler {"paused": true}`, shown as a banner)
  drops firings with no record; re-enabling or resuming doesn't catch up.

The loop also sends the Scheduler Ping (GET `LOCOMOTIVE_HEALTHCHECKS_URL`) every 5 minutes, retrying a
failed one on the next tick. The URL is a secret: it never appears in the log or in `/api/scheduler`
(a failed ping logs only `Scheduler Ping failed: <reason>`). The same goes for the repo token, the
ntfy token and the MCP tokens.

## Notifications

Notifications go straight to ntfy (`LOCOMOTIVE_NTFY_URL`, `_TOPIC`, `_TOKEN`; unset URL or topic = off),
one message per notable Run:

| Event | Priority |
| --- | --- |
| Failed or interrupted Run (any source) | 4 |
| Missed Schedule firing | 4 |
| Scheduled Run that changed hosts, when its Schedule's notify-on-changes is on | 3 |

Successful hand-started and MCP Runs, canceled Runs and skipped firings stay silent.

Titles: `<Playbook> failed` / `<Playbook> interrupted` / `<Playbook>: changes on N hosts` /
`<Schedule> missed`. The body has the source, check mode, short SHA (or "working copy"), duration,
failed/unreachable/changed hosts with counts and the first failed task, never log lines. It clicks through
to `LOCOMOTIVE_PUBLIC_URL/runs/<id>` (default `https://LOCOMOTIVE_PUBLIC_HOSTNAME`).

A failed delivery is retried 3 times (backoff `LOCOMOTIVE_NTFY_RETRY_SECONDS`, doubling), then recorded
on the Run (`notification` in `GET /api/runs/<id>`, shown on the Run page); the Run's own outcome never
changes.

## Log retention

Every Run's history row is kept forever; its log and task grid (`<data dir>/history/<id>/`) are not.

- **Cap:** a Run's log stops at `LOCOMOTIVE_LOG_CAP_BYTES` (default 200 MB). The Run carries on with a
  complete grid and recap and is marked "log truncated". The cap bounds the Run's disk use: ansible-runner
  writes no stdout file, each of its event files is deleted once folded into the log and grid, and its own
  output is capped.
- **Compression:** a finished Run's files are gzip-compressed (the log in indexed ~1 MB members, so
  `GET /api/runs/<id>/log?after=<seq>&limit=<n>` and the SSE stream seek to their cursor instead of
  reading the whole log).
- **Housekeeping** (not a Schedule) runs at startup and daily at 04:00 in `LOCOMOTIVE_TIME_ZONE`. It
  prunes the logs of quiet scheduled Runs (no changes, no failures) after 14 days and of all other Runs
  after 90 days, then, while all Run logs exceed `LOCOMOTIVE_LOG_BUDGET_BYTES` (default 10 GB), the oldest
  un-Kept ones.
- **Keep:** a Run with Keep set (`PUT /api/runs/<id>/keep`, the Run page's Keep box) is never pruned. If
  Kept logs alone exceed the budget nothing is pruned for it and one ntfy warning (priority 3, "Kept logs
  exceed budget") goes out until the overflow clears. New Runs are never refused.

A pruned Run keeps its summary and recap and shows "log pruned on <date>" (`log_pruned_at`; its `/log`
and `/grid` answer 410, MCP `tail_run_output` a `log_pruned` error). Each pass and what it pruned is
listed on the Status page (`GET /api/housekeeping`).

## MCP

`/mcp` is an MCP server (official `mcp` SDK, streamable HTTP) for Claude Code and other clients that can
send a header.

- **Tokens:** each client gets a static bearer token; locomotive stores only its sha256, as
  `LOCOMOTIVE_MCP_TOKENS=lxc=<sha256>,mac=<sha256>`. A missing or unknown token is a 401 (no OAuth), and a
  Run a token starts shows `mcp:<name>` as its source.
- **Host check:** `LOCOMOTIVE_PUBLIC_HOSTNAME` is the hostname clients use. MCP's DNS-rebinding check
  accepts it (and localhost) in `Host`/`Origin` and answers 421 to anything else.
- **Tools:** `list_playbooks`, `list_branches`, `working_copy_status`, `list_schedules`, `get_inventory`,
  `list_tags`, `preview_hosts`, `preview_tasks`, `list_runs`, `get_run`, `start_run`,
  `tail_run_output(run_id, cursor)` and `wait_for_run(run_id, max_seconds <= 30)`.
- **`start_run`** is check mode by default; a real Run needs `confirm: true`. It takes
  `source: clean | working_copy` and `branch`, and returns the queued or running Run at once.

Each tool adapts the HTTP API in-process, so it validates and fails exactly as the UI does (an argument a
tool doesn't define fails the call, never dropped), and answers within
`LOCOMOTIVE_MCP_TOOL_BUDGET_SECONDS` (default 9) except `wait_for_run`, which takes what it is asked for
(default 8s).

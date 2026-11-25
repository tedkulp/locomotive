# locomotive

A small web app plus MCP server that runs Ansible Playbooks from one repo: preview, launch, queue, schedule, and keep history of Runs.

Status: under construction (launch form with host/task previews; clean Runs of any remote branch or Working Copy Runs in place, check or real, with their log; per-host queue; Schedules with the Scheduler Ping; MCP server).

## Layout

- `src/locomotive/`: FastAPI app (one process: API, Run supervisor), SQLite via SQLAlchemy + Alembic
- `frontend/`: React + Vite + shadcn/ui, built to static files the backend serves
- `tests/`: seam 1 harness (app in-process over real HTTP, stub toolchain, local bare git remote, fake clock, fake HTTP server)

## Development

Toolchain versions are pinned in `mise.toml` (`mise install`).

```sh
uv sync                      # backend deps
uv run pytest                # backend test suite
cd frontend && pnpm install
pnpm typecheck && pnpm build # frontend checks + build to frontend/dist
```

Run locally (settings are env vars prefixed `LOCOMOTIVE_`, see `.env.example` and `src/locomotive/settings.py`):

```sh
LOCOMOTIVE_DATA_DIR=./data LOCOMOTIVE_REPO_URL=<repo url> \
LOCOMOTIVE_STATIC_DIR=frontend/dist uv run python -m locomotive
cd frontend && pnpm dev      # optional: Vite dev server proxying /api to :8000
```

Access guard: locomotive has no login page. It accepts connections only from
`LOCOMOTIVE_TRUSTED_PROXY_IPS` (the reverse proxy; addresses or CIDRs) and takes the user from
`LOCOMOTIVE_IDENTITY_HEADER`, which must be on `LOCOMOTIVE_ALLOWLIST`. Missing identity is a plain
401, an unlisted one 403; any other peer gets 403. `/mcp` ignores identity headers. Unset values fail
closed, so for local use set e.g. `LOCOMOTIVE_TRUSTED_PROXY_IPS=127.0.0.1
LOCOMOTIVE_IDENTITY_HEADER=X-User LOCOMOTIVE_ALLOWLIST=me` and have your browser (e.g. a header
extension) send `X-User: me`.

Each Run exports the resolved commit, runs `mise install` against that checkout's `mise.toml`, installs
`collections/requirements.yml` into a cache keyed by its sha256, resolves its exact host set, then runs
`ansible-runner` (which must be provided by the checkout's mise toolchain) with the launch options.

Per-host queue: two Runs never touch the same host at once (whatever started them, check mode included).
A Run is `queued` from launch until it holds its hosts: it fetches and fixes its SHA (a failed fetch fails
it at once), resolves its exact host set, then starts when no running Run holds one of its hosts and no
earlier-launched queued Run shares one (so no Run is starved). Meanwhile it shows `waiting_for`; `POST
/api/runs/{id}/cancel` takes it out of the queue. Queued Runs survive a restart with the SHA and host set
they were queued with. A Working Copy Run re-resolves its hosts as it starts and waits again on a new overlap.

Working Copy Runs (`checkout: "working_copy"`, needs `LOCOMOTIVE_WORKING_COPY_PATH`) run in place in that
clone, dirty files and all, with its own `mise.toml`. Each records HEAD, a dirty flag, `git diff HEAD` (capped at
1 MB, `GET /api/runs/<id>/working-copy-diff`) and the untracked files. locomotive only reads the Working Copy
(without git's optional locks) and runs in it: it never commits, pulls, fetches or pushes it, so the
ahead/behind shown by `GET /api/working-copy` is against its upstream as last fetched there. In Docker, mount the
clone at the same path inside the container, read-write. Clean Runs take a `branch` (default
`LOCOMOTIVE_DEFAULT_BRANCH`; `GET /api/branches` lists the remote's).

Schedules (`/api/schedules`, the Schedules page): saved Run settings (Playbook, limit, tags, check/diff,
verbosity, extra vars, branch; empty branch = the default branch) with a five-field cron expression read in
`LOCOMOTIVE_TIME_ZONE` (unset: `TZ`, then `/etc/localtime`, then UTC). The scheduler loop (every
`LOCOMOTIVE_SCHEDULER_INTERVAL_SECONDS`, default 30) looks at cron firings after each Schedule's
`last_fired_at`; each firing gives one Run with source `schedule`, labelled with its Schedule, always a
clean export (never the Working Copy). If that Schedule's previous Run is still queued or running, the firing
leaves a `skipped` history entry with the reason instead. Firings missed while locomotive was down collapse
into one Run if the newest is at most 1h old, otherwise one `missed` entry is recorded (no Run). DST: a
skipped wall-clock time doesn't fire; a repeated one fires once (its first occurrence). Disabling a
Schedule or pausing all of them (`PUT /api/scheduler {"paused": true}`, shown as a banner) drops firings
with no record; re-enabling or resuming doesn't catch up. The loop also sends the Scheduler Ping (GET
`LOCOMOTIVE_HEALTHCHECKS_URL`) every 5 minutes, retrying a failed one on the next tick.

Previews (`POST /api/preview`) and each Run's host set come from `src/locomotive/preview_helper.py`, run
with the Python interpreter of the checkout's own ansible-core (never imported by the app). It emits names
only (groups, hosts, plays, tasks, tags), never host vars, and `ansible-inventory` is never used.

`uv run pytest` includes seam 2 (`tests/test_smoke_real_ansible.py`): the real helper and one real
`ansible-runner` Run against `localhost`, with the ansible-core pinned there. It needs `mise` on PATH
(skipped otherwise) and installs that runtime on first use.

MCP: `/mcp` is an MCP server (official `mcp` SDK, streamable HTTP) for Claude Code and other clients
that can send a header. Each client gets a static bearer token; locomotive stores only its sha256, as
`LOCOMOTIVE_MCP_TOKENS=lxc=<sha256>,mac=<sha256>` (make one with `openssl rand -hex 32`, hash it with
`printf %s "$TOKEN" | sha256sum`). A missing or unknown token is a 401 (no OAuth), and a Run a token
starts shows `mcp:<name>` as its source. `LOCOMOTIVE_PUBLIC_HOSTNAME` is the hostname clients use: MCP's
DNS-rebinding check accepts it (and localhost) in `Host`/`Origin` and answers 421 to anything else.
Tools: `list_playbooks`, `list_branches`, `working_copy_status`, `list_schedules`, `get_inventory`,
`list_tags`, `preview_hosts`, `preview_tasks`, `list_runs`, `get_run`, `start_run` (check by default; a
real Run needs `confirm: true`; `source: clean | working_copy`, `branch`; returns the queued or running
Run at once), `tail_run_output(run_id, cursor)` and `wait_for_run(run_id, max_seconds <= 30)`. Each
tool adapts the HTTP API in-process, so it validates and fails exactly as the UI does, and answers
within `LOCOMOTIVE_MCP_TOOL_BUDGET_SECONDS` (default 9) except `wait_for_run`, which takes what it is asked for
(default 8s).

```sh
claude mcp add --transport http locomotive https://<public hostname>/mcp --header "Authorization: Bearer $TOKEN"
```

## Docker

```sh
cp .env.example .env   # fill in
docker compose up --build
```

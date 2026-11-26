# locomotive

A self-hosted web app and MCP server for running the Ansible Playbooks in one Git repo: preview, launch,
queue, schedule, and keep a history of every Run.

It replaces a general-purpose Ansible UI with something small and opinionated: one repo, one process, one
SQLite database, and no login page (it sits behind a reverse proxy that supplies the user's identity).

## Features

- **Launch form** with live previews of the hosts and tasks a Run will touch, before it starts
- **Clean Runs** of any remote branch, or **Working Copy Runs** in place, uncommitted changes and all
- **Per-host queue**: two Runs never touch the same host at once
- **Run page** with a task × host grid, recap, live log, Stop and Kill
- **Schedules** (cron) with a Scheduler Ping to a healthchecks URL
- **ntfy notifications** for failed, interrupted, missed and changed Runs
- **MCP server** at `/mcp`, so Claude Code and other agents can preview and start Runs
- **Log retention**: per-Run log cap, gzip, and daily pruning under a disk budget

Every Run's history row is kept forever. Not supported: multiple repos, users with different permissions,
and logging in without a reverse proxy.

## What your Ansible repo needs

locomotive runs Playbooks from the repo at `LOCOMOTIVE_REPO_URL`. That repo needs:

- `playbooks/*.yml`: each top-level file is a Playbook locomotive offers to run
  (hide some with `LOCOMOTIVE_HIDDEN_PLAYBOOKS`)
- `mise.toml` pinning the Ansible toolchain, including `ansible-runner`: locomotive runs
  `mise install` and `mise exec` against it for every Run
- Its inventory set by the repo's own Ansible config (locomotive passes no `-i`)
- Optionally `collections/requirements.yml`, installed into a cache per Run

## Quick start (Docker)

Requires Docker with Compose.

```sh
cp .env.example .env   # set LOCOMOTIVE_REPO_URL and the three access-guard values
docker compose up --build
```

The app listens on `127.0.0.1:8000` (change with `LOCOMOTIVE_BIND` / `LOCOMOTIVE_HOST_PORT`) and keeps
its data in `./data` (`LOCOMOTIVE_DATA_PATH`). Put it behind a reverse proxy that sets the identity header.

## Configuration

All settings are env vars prefixed `LOCOMOTIVE_`. `.env.example` lists them, and
`src/locomotive/settings.py` is the full source.

| Variable | Purpose |
| --- | --- |
| `REPO_URL` (required) | The Ansible repo to run Playbooks from. Add `REPO_USERNAME` / `REPO_TOKEN` for a private repo |
| `DATA_DIR` (required outside Docker) | Database, mirror, Run dirs, logs and caches |
| `TRUSTED_PROXY_IPS`, `IDENTITY_HEADER`, `ALLOWLIST` | Access guard: which peers may connect and which identities get in. Unset fails closed |
| `DEFAULT_BRANCH` | Branch for clean Runs and Schedules (default `main`) |
| `WORKING_COPY_PATH` | A clone to allow Working Copy Runs in |
| `MCP_TOKENS`, `PUBLIC_HOSTNAME` | MCP clients' hashed bearer tokens, and the hostname they use |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | Notifications (off unless URL and topic are set) |
| `HEALTHCHECKS_URL` | Where the Scheduler Ping goes |
| `TIME_ZONE` | IANA zone for cron and housekeeping (unset: `TZ`, then `/etc/localtime`, then UTC) |
| `LOG_CAP_BYTES`, `LOG_BUDGET_BYTES` | Per-Run log cap (200 MB) and budget for all logs (10 GB) |

## Connecting an MCP client

1. Make a token: `TOKEN=$(openssl rand -hex 32)`
2. Hash it: `printf %s "$TOKEN" | sha256sum`
3. Add `LOCOMOTIVE_MCP_TOKENS=<name>=<sha256>` (comma-separate several) and restart
4. Register it:

   ```sh
   claude mcp add --transport http locomotive https://<public hostname>/mcp --header "Authorization: Bearer $TOKEN"
   ```

`start_run` uses check mode unless called with `confirm: true`. See [docs/behaviour.md](docs/behaviour.md#mcp)
for the tool list.

## Development

Requires [mise](https://mise.jdx.dev) (pins Python 3.13, uv, Node 22 and pnpm in `mise.toml`).

```sh
mise install
uv sync                        # backend deps
uv run pytest                  # backend tests
cd frontend && pnpm install
pnpm typecheck && pnpm build   # frontend checks, built to frontend/dist
```

Run it locally. The access guard needs a trusted peer and an identity, so have your browser (e.g. a header
extension) send `X-User: me`:

```sh
LOCOMOTIVE_DATA_DIR=./data LOCOMOTIVE_REPO_URL=<repo url> LOCOMOTIVE_STATIC_DIR=frontend/dist \
LOCOMOTIVE_TRUSTED_PROXY_IPS=127.0.0.1 LOCOMOTIVE_IDENTITY_HEADER=X-User LOCOMOTIVE_ALLOWLIST=me \
uv run python -m locomotive    # serves http://127.0.0.1:8000
cd frontend && pnpm dev        # optional: Vite dev server proxying /api to :8000
```

### Layout

- `src/locomotive/`: FastAPI app (one process: API, Run supervisor, scheduler), SQLite via SQLAlchemy + Alembic
- `frontend/`: React + Vite + shadcn/ui, built to static files the backend serves
- `tests/`: the seam 1 harness (app in-process over real HTTP, stub toolchain, local bare git remote, fake
  clock, fake HTTP server) and seam 2, `tests/test_smoke_real_ansible.py`: the real preview helper and one real
  `ansible-runner` Run against `localhost`. Seam 2 needs `mise` on PATH (skipped otherwise) and installs its
  Ansible runtime on first use.

## More

- [docs/behaviour.md](docs/behaviour.md): the detailed rules for the queue, Working Copy, previews,
  Schedules, notifications, log retention and MCP
- [GLOSSARY.md](GLOSSARY.md): what Run, Playbook, Working Copy, Schedule and Scheduler Ping mean here
- Issues: GitHub Issues on this repo

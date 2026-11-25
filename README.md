# locomotive

A small web app plus MCP server that runs Ansible Playbooks from one repo: preview, launch, queue, schedule, and keep history of Runs.

Status: under construction (launch form with host/task previews; clean Runs of any remote branch or Working Copy Runs in place, check or real, with their log).

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

Working Copy Runs (`checkout: "working_copy"`, needs `LOCOMOTIVE_WORKING_COPY_PATH`) run in place in that
clone, dirty files and all, with its own `mise.toml`. Each records HEAD, a dirty flag, `git diff HEAD` (capped at
1 MB, `GET /api/runs/<id>/working-copy-diff`) and the untracked files. locomotive only reads the Working Copy
(without git's optional locks) and runs in it: it never commits, pulls, fetches or pushes it, so the
ahead/behind shown by `GET /api/working-copy` is against its upstream as last fetched there. In Docker, mount the
clone at the same path inside the container, read-write. Clean Runs take a `branch` (default
`LOCOMOTIVE_DEFAULT_BRANCH`; `GET /api/branches` lists the remote's).

Previews (`POST /api/preview`) and each Run's host set come from `src/locomotive/preview_helper.py`, run
with the Python interpreter of the checkout's own ansible-core (never imported by the app). It emits names
only (groups, hosts, plays, tasks, tags), never host vars, and `ansible-inventory` is never used.

`uv run pytest` includes seam 2 (`tests/test_smoke_real_ansible.py`): the real helper and one real
`ansible-runner` Run against `localhost`, with the ansible-core pinned there. It needs `mise` on PATH
(skipped otherwise) and installs that runtime on first use.

## Docker

```sh
cp .env.example .env   # fill in
docker compose up --build
```

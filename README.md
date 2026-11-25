# locomotive

A small web app plus MCP server that runs Ansible Playbooks from one repo: preview, launch, queue, schedule, and keep history of Runs.

Status: under construction (walking skeleton: start a clean check-mode Run of the default branch and watch its log).

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

Each Run exports the resolved commit, runs `mise install` against that checkout's `mise.toml`, installs
`collections/requirements.yml` into a cache keyed by its sha256, then runs `ansible-runner` (which must be
provided by the checkout's mise toolchain) in check mode.

## Docker

```sh
cp .env.example .env   # fill in
docker compose up --build
```

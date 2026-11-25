from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.routing import Route

from locomotive import api
from locomotive.checkout import Checkout
from locomotive.clock import Clock, SystemClock
from locomotive.db import make_engine, make_sessionmaker, migrate
from locomotive.guard import AccessGuard, parse_networks
from locomotive.mcp_server import MCP_PATH, create_mcp
from locomotive.settings import Settings
from locomotive.preview import Previewer
from locomotive.runtime import Toolchain
from locomotive.supervisor import Supervisor


def create_app(settings: Settings, clock: Clock | None = None) -> FastAPI:
    clock = clock or SystemClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.sqlalchemy_url)
        migrate(engine)
        sessions = make_sessionmaker(engine)
        checkout = Checkout(settings)
        toolchain = Toolchain(settings)
        supervisor = Supervisor(settings, clock, sessions, checkout, toolchain)
        supervisor.sweep_orphans()
        supervisor.interrupt_leftovers()
        supervisor.resume_queued()
        previewer = Previewer(settings, checkout, toolchain)
        previewer.sweep()
        app.state.settings = settings
        app.state.clock = clock
        app.state.sessions = sessions
        app.state.checkout = checkout
        app.state.supervisor = supervisor
        app.state.toolchain = toolchain
        app.state.previewer = previewer
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await previewer.shutdown()
            await supervisor.shutdown()
            engine.dispose()

    app = FastAPI(title="locomotive", lifespan=lifespan)
    parse_networks(settings.trusted_proxy_ips)  # a bad entry stops startup, not the first request
    app.add_middleware(AccessGuard, settings=settings)
    app.include_router(api.router, prefix="/api")
    mcp, mcp_app = create_mcp(app, settings)  # a bad token entry stops startup too
    app.router.routes.append(Route(MCP_PATH, endpoint=mcp_app))  # before the frontend's catch-all
    if settings.static_dir is not None:
        _mount_frontend(app, settings.static_dir)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built single-page app: real files as-is, every other path gets index.html."""
    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")

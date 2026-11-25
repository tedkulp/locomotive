from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from locomotive import api
from locomotive.checkout import Checkout
from locomotive.clock import Clock, SystemClock
from locomotive.db import make_engine, make_sessionmaker, migrate
from locomotive.settings import Settings
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
        supervisor = Supervisor(settings, clock, sessions, checkout)
        supervisor.sweep_orphans()
        app.state.settings = settings
        app.state.clock = clock
        app.state.sessions = sessions
        app.state.checkout = checkout
        app.state.supervisor = supervisor
        try:
            yield
        finally:
            await supervisor.shutdown()
            engine.dispose()

    app = FastAPI(title="locomotive", lifespan=lifespan)
    app.include_router(api.router, prefix="/api")
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

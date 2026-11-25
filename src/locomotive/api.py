from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from locomotive.checkout import PLAYBOOK_NAME, CheckoutError, UnknownBranch
from locomotive.models import Run
from locomotive.runlog import RunLog

router = APIRouter()

SSE_POLL_SECONDS = 0.2
SSE_HEARTBEAT_SECONDS = 15


# ---------------------------------------------------------------------- Playbooks


@router.get("/playbooks")
async def list_playbooks(request: Request, branch: str | None = None) -> dict:
    state = request.app.state
    try:
        listing = await state.checkout.list_playbooks(branch or state.settings.default_branch)
    except UnknownBranch as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CheckoutError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "branch": listing.branch,
        "sha": listing.sha,
        "playbooks": [{"name": name, "path": f"playbooks/{name}"} for name in listing.playbooks],
    }


# ---------------------------------------------------------------------- Runs


class StartRun(BaseModel):
    """Launch options. Anything locomotive can't honour yet is rejected (extra="forbid")."""

    model_config = ConfigDict(extra="forbid")

    playbook: str = Field(min_length=1, max_length=255)
    check: Literal[True] = True


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def run_json(run: Run) -> dict:
    duration = None
    if run.started_at and run.finished_at:
        duration = (run.finished_at - run.started_at).total_seconds()
    return {
        "id": run.id,
        "playbook": run.playbook,
        "status": run.status,
        "source": run.source,
        "checkout": run.checkout,
        "branch": run.branch,
        "sha": run.sha,
        "check": run.check,
        "requirements_hash": run.requirements_hash,
        "ansible_core_version": run.ansible_core_version,
        "rc": run.rc,
        "failure_reason": run.failure_reason,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_seconds": duration,
    }


@router.post("/runs", status_code=201)
async def start_run(body: StartRun, request: Request) -> dict:
    state = request.app.state
    if not PLAYBOOK_NAME.match(body.playbook):
        raise HTTPException(status_code=422, detail=f"invalid Playbook name {body.playbook!r}")
    if body.playbook in state.settings.hidden_playbooks:
        raise HTTPException(status_code=422, detail=f"Playbook {body.playbook} is hidden on this Controller")
    now = state.clock.now()
    with state.sessions() as session:
        run = Run(
            playbook=body.playbook,
            status="running",
            source="ui",
            checkout="clean",
            branch=state.settings.default_branch,
            check=body.check,
            created_at=now,
            started_at=now,
        )
        session.add(run)
        session.commit()
    state.supervisor.start(run.id)
    return run_json(run)


@router.get("/runs")
async def list_runs(request: Request, limit: int = 50) -> dict:
    limit = max(1, min(limit, 500))
    with request.app.state.sessions() as session:
        runs = session.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all()
    return {"runs": [run_json(run) for run in runs]}


def _get_run(request: Request, run_id: int) -> Run:
    with request.app.state.sessions() as session:
        run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: int, request: Request) -> dict:
    return run_json(_get_run(request, run_id))


@router.get("/runs/{run_id}/log")
async def get_log(run_id: int, request: Request, after: int = 0) -> dict:
    _get_run(request, run_id)
    path = request.app.state.supervisor.log_path(run_id)
    return {"lines": [line.__dict__ for line in RunLog.read(path, after)]}


@router.get("/runs/{run_id}/log/stream")
async def stream_log(
    run_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-Sent Events: one ``log`` event per line (id = seq), then ``end`` once the Run is over."""
    _get_run(request, run_id)
    path = request.app.state.supervisor.log_path(run_id)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def events() -> AsyncIterator[str]:
        nonlocal after
        idle = 0.0
        while True:
            run = _get_run(request, run_id)  # status first: a finished Run has written its whole log
            lines = RunLog.read(path, after)
            for line in lines:
                after = line.seq
                data = json.dumps({"seq": line.seq, "text": line.text, "kind": line.kind})
                yield f"id: {line.seq}\nevent: log\ndata: {data}\n\n"
            if run.is_finished:
                yield f"event: end\ndata: {json.dumps(run_json(run))}\n\n"
                return
            if lines:
                idle = 0.0
            elif idle >= SSE_HEARTBEAT_SECONDS:
                idle = 0.0
                yield ": keep-alive\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(SSE_POLL_SECONDS)
            idle += SSE_POLL_SECONDS

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from locomotive.checkout import BRANCH_NAME, PLAYBOOK_NAME, CheckoutError, UnknownBranch
from locomotive.launch import LaunchOptions
from locomotive.models import Run
from locomotive.preview import PreviewError
from locomotive.rungrid import RunGrid, public_json
from locomotive.runlog import LogTail, RunLog
from locomotive.supervisor import launch_options
from locomotive.working_copy import WorkingCopyUnavailable

router = APIRouter()

SSE_POLL_SECONDS = 0.2

# Who launched a Run, as recorded in history. Set in the ASGI scope only by locomotive itself (the MCP
# server puts ``mcp:<token name>`` here); a request from outside can't set it, so it is "ui".
RUN_SOURCE_KEY = "locomotive.run_source"


# ---------------------------------------------------------------------- Playbooks


@router.get("/playbooks")
async def list_playbooks(
    request: Request, branch: str | None = None, checkout: Literal["clean", "working_copy"] = "clean"
) -> dict:
    """Playbooks in ``playbooks/`` of the chosen checkout, read now (never frozen)."""
    state = request.app.state
    if checkout == "working_copy":
        if branch is not None:
            raise HTTPException(status_code=422, detail="the Working Copy has no branch choice")
        try:
            status = await state.working_copy.status()
        except WorkingCopyUnavailable as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "checkout": "working_copy",
            "branch": status.branch or "HEAD",
            "sha": status.head_sha,
            "playbooks": _playbooks(state.working_copy.list_playbooks()),
        }
    try:
        listing = await state.checkout.list_playbooks(branch or state.settings.default_branch)
    except UnknownBranch as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CheckoutError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "checkout": "clean",
        "branch": listing.branch,
        "sha": listing.sha,
        "playbooks": _playbooks(listing.playbooks),
    }


def _playbooks(names: list[str]) -> list[dict]:
    return [{"name": name, "path": f"playbooks/{name}"} for name in names]


@router.get("/branches")
async def list_branches(request: Request) -> dict:
    """The remote's branches (fetched now), for a clean Run's branch dropdown."""
    state = request.app.state
    try:
        branches = await state.checkout.list_branches()
    except CheckoutError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"default": state.settings.default_branch, "branches": branches}


# ---------------------------------------------------------------------- Working Copy


@router.get("/working-copy")
async def working_copy_status(request: Request) -> dict:
    """The Working Copy's branch, HEAD, dirty and untracked files, and ahead/behind its upstream.

    Ahead/behind is against the Working Copy's own remote-tracking branch as last fetched by Ted:
    locomotive never fetches (or otherwise writes to) the Working Copy.
    """
    wc = request.app.state.working_copy
    if not wc.configured:
        return {"available": False, "reason": "no Working Copy is configured on this Controller"}
    try:
        status = await wc.status()
    except WorkingCopyUnavailable as exc:
        return {"available": False, "path": str(wc.path), "reason": str(exc)}
    return {
        "available": True,
        "path": str(status.path),
        "branch": status.branch,
        "head_sha": status.head_sha,
        "dirty": status.dirty,
        "changed": status.changed,
        "untracked": status.untracked,
        "upstream": status.upstream,
        "ahead": status.ahead,
        "behind": status.behind,
    }


def _check_playbook(state, playbook: str) -> None:  # type: ignore[no-untyped-def]
    if not PLAYBOOK_NAME.match(playbook):
        raise HTTPException(status_code=422, detail=f"invalid Playbook name {playbook!r}")
    if playbook in state.settings.hidden_playbooks:
        raise HTTPException(status_code=422, detail=f"Playbook {playbook} is hidden on this Controller")


# ---------------------------------------------------------------------- Previews


class RunRequest(LaunchOptions):
    """Launch options plus which checkout to run: a clean export of ``branch`` (default: the default
    branch) or the Working Copy in place (which runs whatever branch it is on)."""

    checkout: Literal["clean", "working_copy"] = "clean"
    branch: str | None = Field(default=None, max_length=255)

    @field_validator("branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        if value is not None and not BRANCH_NAME.match(value):
            raise ValueError(f"invalid branch name {value!r}")
        return value

    @model_validator(mode="after")
    def _branch_only_for_clean(self) -> RunRequest:
        if self.checkout == "working_copy" and self.branch is not None:
            raise ValueError("a Working Copy Run runs the branch the Working Copy is on; don't pass a branch")
        return self

    def launch_options(self) -> LaunchOptions:
        return LaunchOptions.model_validate(self.model_dump(exclude={"checkout", "branch"}))


class PreviewRequest(RunRequest):
    """The launch options to preview, plus how to show includes and (optionally) which commit."""

    expand_includes: bool = False
    sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    def launch_options(self) -> LaunchOptions:
        return LaunchOptions.model_validate(self.model_dump(exclude={"checkout", "branch", "expand_includes", "sha"}))


@router.post("/preview")
async def preview(body: PreviewRequest, request: Request) -> dict:
    """Hosts the Run would touch (exact), tasks that may run, tags and the inventory graph (no vars),
    from the chosen checkout: a clean ``branch`` (default: the default branch) or the Working Copy."""
    state = request.app.state
    _check_playbook(state, body.playbook)
    options = body.launch_options()
    try:
        if body.checkout == "working_copy":
            return await state.previewer.preview_working_copy(options, state.working_copy, body.expand_includes)
        return await state.previewer.preview(
            options, body.branch or state.settings.default_branch, body.sha, body.expand_includes
        )
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except WorkingCopyUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownBranch as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CheckoutError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------- Runs


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
        "diff": run.diff,
        "verbosity": run.verbosity,
        "limit": run.limit,
        "hosts": run.hosts,
        "tags": run.tags or [],
        "extra_vars": run.extra_vars or {},
        "requirements_hash": run.requirements_hash,
        "ansible_core_version": run.ansible_core_version,
        "rc": run.rc,
        "failure_reason": run.failure_reason,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_seconds": duration,
        "recap": run.recap,
        "recap_source": run.recap_source,
        "stop_requested": run.cancel_request == "stop" and not run.is_finished,
        "waiting_for": run.waiting_for if run.status == "queued" else None,
        "dirty": run.dirty,
        "untracked": run.untracked,
        "diff_truncated": run.diff_truncated,
        "schedule": None if run.schedule_name is None else {"id": run.schedule_id, "name": run.schedule_name},
        "first_failed_task": run.first_failed_task,
        # ntfy delivery of this Run's notification (None: it wasn't notable). The error never holds the
        # token or the ntfy URL.
        "notification": None if run.notification_status is None else {
            "status": run.notification_status,
            "attempts": run.notification_attempts,
            "error": run.notification_error,
        },
        # Log retention: live / complete / truncated (stopped at the per-Run cap) / pruned (log, events and
        # grid deleted by housekeeping on log_pruned_at; this record and its recap remain).
        "keep": run.keep,
        "log_state": run.log_state,
        "log_truncated": run.log_truncated,
        "log_pruned": run.log_pruned_at is not None,
        "log_pruned_at": _iso(run.log_pruned_at),
        "log_bytes": run.log_bytes,
    }


@router.post("/runs", status_code=201)
async def start_run(body: RunRequest, request: Request) -> dict:
    """Start a Run. Options locomotive can't honour are rejected here (422), never silently ignored."""
    state = request.app.state
    if body.checkout == "working_copy":
        branch = await _working_copy_branch(request)
    else:
        branch = body.branch or state.settings.default_branch
    return _launch(request, body.launch_options(), branch=branch, checkout=body.checkout)


async def _working_copy_branch(request: Request) -> str:
    """The Working Copy's current branch ("HEAD" when detached); 422 if there is no usable Working Copy."""
    try:
        status = await request.app.state.working_copy.status()
    except WorkingCopyUnavailable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return status.branch or "HEAD"


@router.post("/runs/{run_id}/again", status_code=201)
async def run_again(run_id: int, request: Request) -> dict:
    """A new Run with the settings this one recorded (a clean Run fetches its branch afresh; a Working
    Copy Run runs the Working Copy as it is now)."""
    run = _get_run(request, run_id)
    branch = await _working_copy_branch(request) if run.checkout == "working_copy" else run.branch
    return _launch(request, _launch_settings(run), branch=branch, checkout=run.checkout)


def _launch_settings(run: Run) -> LaunchOptions:
    """The launch options a Run recorded, as they'd be sent to POST /runs.

    The limit is repeated, not the resolved host set: the new Run resolves it afresh, as a launch does.
    """
    return launch_options(run)


def _launch(request: Request, body: LaunchOptions, branch: str, checkout: str = "clean") -> dict:
    state = request.app.state
    _check_playbook(state, body.playbook)
    now = state.clock.now()
    with state.sessions() as session:
        run = Run(
            playbook=body.playbook,
            # Queued until its SHA and host set are fixed and no other Run holds (or is ahead for) a host.
            status="queued",
            source=request.scope.get(RUN_SOURCE_KEY, "ui"),
            checkout=checkout,
            branch=branch,
            check=body.check,
            diff=body.diff,
            verbosity=body.verbosity,
            limit=body.limit,
            tags=body.tags,
            extra_vars=body.extra_vars,
            created_at=now,
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


@router.get("/runs/{run_id}/working-copy-diff")
async def get_working_copy_diff(run_id: int, request: Request) -> dict:
    """What a Working Copy Run had uncommitted: ``git diff HEAD`` (capped at ~1 MB) and untracked files."""
    run = _get_run(request, run_id)
    if run.checkout != "working_copy":
        raise HTTPException(status_code=404, detail=f"Run {run_id} is a clean Run; it has no Working Copy diff")
    return {
        "sha": run.sha,
        "dirty": run.dirty,
        "diff": run.wc_diff,
        "truncated": run.diff_truncated,
        "untracked": run.untracked,
    }


class KeepRequest(BaseModel):
    keep: bool


@router.put("/runs/{run_id}/keep")
async def keep_run(run_id: int, body: KeepRequest, request: Request) -> dict:
    """Keep exempts the Run's log from pruning (by age and by the global budget)."""
    with request.app.state.sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        run.keep = body.keep
        session.commit()
    return run_json(run)


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: int, request: Request) -> dict:
    """Stop after this task: the running task finishes on every host, then the Run ends as canceled."""
    return _control(request, run_id, request.app.state.supervisor.request_stop)


@router.post("/runs/{run_id}/kill")
async def kill_run(run_id: int, request: Request) -> dict:
    """End the Run now: SIGINT, then SIGKILL, to its process group."""
    return _control(request, run_id, request.app.state.supervisor.request_kill)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, request: Request) -> dict:
    """Take a queued Run out of the queue before it touches a host; it ends as canceled."""
    run = _get_run(request, run_id)
    if run.status != "queued" or not request.app.state.supervisor.request_cancel(run_id):
        raise HTTPException(status_code=409, detail=f"Run {run_id} is not queued")
    return run_json(_get_run(request, run_id))


def _control(request: Request, run_id: int, action: Callable[[int], bool]) -> dict:
    run = _get_run(request, run_id)
    if run.is_finished or not action(run_id):
        raise HTTPException(status_code=409, detail=f"Run {run_id} is not running")
    return run_json(_get_run(request, run_id))


def _not_pruned(request: Request, run_id: int) -> Run:
    """The Run, or 410 if housekeeping has pruned its log, events and grid (its record remains)."""
    run = _get_run(request, run_id)
    if run.log_pruned_at is not None:
        day = run.log_pruned_at.astimezone(request.app.state.housekeeper.zone).date().isoformat()
        raise HTTPException(status_code=410, detail=f"Run {run_id}'s log was pruned on {day}")
    return run


@router.get("/runs/{run_id}/grid")
async def get_grid(run_id: int, request: Request) -> dict:
    """The task x host grid and per-host recap, as far as the Run has got."""
    _not_pruned(request, run_id)
    grid = RunGrid.load(request.app.state.supervisor.grid_path(run_id))
    return public_json(grid.to_json())


@router.get("/runs/{run_id}/log")
async def get_log(run_id: int, request: Request, after: int = 0, host: str | None = None) -> dict:
    """The log after line ``after``; with ``host``, only that host's lines plus lines about no host."""
    _not_pruned(request, run_id)
    path = request.app.state.supervisor.log_path(run_id)
    lines = RunLog.read(path, after)
    if host is not None:
        lines = [line for line in lines if line.host in (None, host)]
    return {"lines": [line.__dict__ for line in lines]}


@router.get("/runs/{run_id}/log/stream")
async def stream_log(
    run_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    """Server-Sent Events: one ``log`` event per line (id = seq), a ``grid`` event (the whole grid and
    recap) whenever it changes, then ``end`` once the Run is over."""
    _get_run(request, run_id)
    supervisor = request.app.state.supervisor
    path = supervisor.log_path(run_id)
    grid_path = supervisor.grid_path(run_id)
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    heartbeat = request.app.state.settings.sse_heartbeat_seconds

    async def events() -> AsyncIterator[str]:
        nonlocal after
        idle = 0.0
        tail = LogTail(path, after)
        grid_stamp: tuple[int, int, int] | None = None
        while True:
            run = _get_run(request, run_id)  # status first: a finished Run has written its whole log
            lines = tail.read()
            for line in lines:
                data = json.dumps(line.__dict__)
                yield f"id: {line.seq}\nevent: log\ndata: {data}\n\n"
            stamp = _stamp(grid_path)
            if stamp != grid_stamp:
                grid_stamp = stamp
                grid = public_json(RunGrid.load(grid_path).to_json())
                yield f"event: grid\ndata: {json.dumps(grid)}\n\n"
            if run.is_finished:
                yield f"event: end\ndata: {json.dumps(run_json(run))}\n\n"
                return
            if lines:
                idle = 0.0
            elif idle >= heartbeat:
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


def _stamp(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, 0, 0)
    return (st.st_ino, st.st_mtime_ns, st.st_size)

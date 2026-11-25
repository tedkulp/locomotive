"""HTTP API for Schedules and the scheduler's global state (pause all, Scheduler Ping, time zone)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from locomotive import cron
from locomotive.api import _check_playbook, _iso
from locomotive.checkout import BRANCH_NAME
from locomotive.launch import LaunchOptions
from locomotive.models import Run, Schedule

router = APIRouter()


class ScheduleSettings(LaunchOptions):
    """A Schedule as saved: launch options (always a clean checkout) plus name, branch, cron, toggles."""

    name: str = Field(min_length=1, max_length=255)
    cron: str = Field(max_length=255)
    branch: str | None = Field(default=None, max_length=255)  # None = the default branch
    enabled: bool = True
    notify_on_changes: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a Schedule needs a name")
        return value

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return cron.validate(value)

    @field_validator("branch")
    @classmethod
    def _branch(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip() or None
        if value is not None and not BRANCH_NAME.match(value):
            raise ValueError(f"invalid branch name {value!r}")
        return value


class ScheduleUpdate(BaseModel):
    """Any subset of a Schedule's fields; the result is validated as a whole."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    playbook: str | None = None
    limit: str | None = None
    tags: list[str] | None = None
    check: bool | None = None
    diff: bool | None = None
    verbosity: int | None = None
    extra_vars: dict[str, Any] | None = None
    cron: str | None = None
    branch: str | None = None
    enabled: bool | None = None
    notify_on_changes: bool | None = None


FIELDS = tuple(ScheduleSettings.model_fields)


def schedule_json(request: Request, schedule: Schedule, last_run: Run | None = None) -> dict:
    scheduler = request.app.state.scheduler
    return {
        "id": schedule.id,
        **{name: getattr(schedule, name) for name in FIELDS},
        "tags": schedule.tags or [],
        "extra_vars": schedule.extra_vars or {},
        "last_fired_at": _iso(schedule.last_fired_at),
        "next_fire_at": _iso(scheduler.next_firing(schedule)) if schedule.enabled else None,
        "created_at": _iso(schedule.created_at),
        "last_run": None if last_run is None else {"id": last_run.id, "status": last_run.status},
    }


def _last_runs(session, ids: list[int]) -> dict[int, Run]:  # type: ignore[no-untyped-def]
    runs = session.scalars(
        select(Run).where(Run.schedule_id.in_(ids)).order_by(Run.id.desc())
    ).all() if ids else []
    latest: dict[int, Run] = {}
    for run in runs:
        latest.setdefault(run.schedule_id, run)
    return latest


@router.get("/schedules")
async def list_schedules(request: Request) -> dict:
    state = request.app.state
    with state.sessions() as session:
        schedules = session.scalars(select(Schedule).order_by(Schedule.name, Schedule.id)).all()
        latest = _last_runs(session, [s.id for s in schedules])
    return {
        **state.scheduler.status(),
        "schedules": [schedule_json(request, s, latest.get(s.id)) for s in schedules],
    }


@router.post("/schedules", status_code=201)
async def create_schedule(body: ScheduleSettings, request: Request) -> dict:
    state = request.app.state
    _check_playbook(state, body.playbook)
    now = state.clock.now()
    with state.sessions() as session:
        schedule = Schedule(**body.model_dump(), last_fired_at=now, created_at=now)
        session.add(schedule)
        session.commit()
    return schedule_json(request, schedule)


def _get(request: Request, schedule_id: int) -> Schedule:
    with request.app.state.sessions() as session:
        schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    return schedule


@router.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: int, request: Request) -> dict:
    schedule = _get(request, schedule_id)
    with request.app.state.sessions() as session:
        last = _last_runs(session, [schedule_id]).get(schedule_id)
    return schedule_json(request, schedule, last)


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, body: ScheduleUpdate, request: Request) -> dict:
    state = request.app.state
    current = _get(request, schedule_id)
    merged = {name: getattr(current, name) for name in FIELDS} | body.model_dump(exclude_unset=True)
    try:
        settings = ScheduleSettings.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False)) from exc
    _check_playbook(state, settings.playbook)
    with state.sessions() as session:
        schedule = session.get_one(Schedule, schedule_id)
        restart = (settings.enabled and not schedule.enabled) or settings.cron != schedule.cron
        for name, value in settings.model_dump().items():
            setattr(schedule, name, value)
        if restart:
            # Re-enabled or re-timed: only firings from now on count (nothing is backfilled).
            schedule.last_fired_at = state.clock.now()
        session.commit()
        last = _last_runs(session, [schedule_id]).get(schedule_id)
    return schedule_json(request, schedule, last)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: int, request: Request) -> Response:
    """Delete a Schedule. Its Runs and history entries stay (labelled with its name)."""
    with request.app.state.sessions() as session:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
        session.delete(schedule)
        session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------- scheduler


class SchedulerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paused: bool


@router.get("/scheduler")
async def scheduler_status(request: Request) -> dict:
    """Pause-all state, the time zone cron is read in, the last tick and the last Scheduler Ping."""
    return request.app.state.scheduler.status()


@router.put("/scheduler")
async def update_scheduler(body: SchedulerUpdate, request: Request) -> dict:
    """Pause or resume all Schedules. Firings during a pause are dropped without a record."""
    scheduler = request.app.state.scheduler
    scheduler.set_paused(body.paused)
    return scheduler.status()

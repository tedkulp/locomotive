"""The scheduler: an own loop (no APScheduler) that fires Schedules and sends the Scheduler Ping.

Every ``scheduler_interval_seconds`` it reads the clock and, per Schedule, looks for cron firings after
``last_fired_at`` (see ``cron`` for the time zone and DST rules). For a Schedule with due firings:

- paused (globally) or disabled: they are dropped, with no record;
- the newest is more than an hour old (locomotive was down): one "missed" entry, no Run;
- otherwise (several collapse into one): if the Schedule's previous Run is still queued or running,
  a "skipped" entry with the reason; else one clean Run of the Schedule's settings.

The Scheduler Ping goes to the healthchecks URL from the loop itself, every 5 minutes, so it stops when
Schedules stop being looked at.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from locomotive import cron
from locomotive.clock import Clock
from locomotive.models import ControllerSetting, Run, Schedule
from locomotive.notifier import Notifier
from locomotive.runlog import RunLog
from locomotive.settings import Settings
from locomotive.supervisor import Supervisor

log = logging.getLogger(__name__)

MISSED_GRACE = timedelta(hours=1)
MISSED_GRACE_TEXT = "1h"
MISSED_COUNT_CAP = 1000
PING_EVERY = timedelta(minutes=5)
PING_TIMEOUT_SECONDS = 10.0
PAUSED_KEY = "schedules_paused"


class PingFailed(Exception):
    pass


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        sessions: sessionmaker,
        supervisor: Supervisor,
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._clock = clock
        self._sessions = sessions
        self._supervisor = supervisor
        self.zone = cron.local_zone(settings.time_zone)
        self.last_tick_at: datetime | None = None
        self.last_ping_at: datetime | None = None
        self.last_ping_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - one bad tick must not stop the scheduler
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._settings.scheduler_interval_seconds)

    async def tick(self) -> None:
        now = self._clock.now().astimezone(UTC)
        started, ended = self._fire_due(now)
        for run_id in started:
            self._supervisor.start(run_id)
        if self._notifier is not None:
            for run_id in ended:  # missed, skipped or failed entries: the notifier picks the notable ones
                self._notifier.run_finished(run_id)
        await self._ping(now)
        self.last_tick_at = now

    async def _ping(self, now: datetime) -> None:
        """The Scheduler Ping: every 5 minutes; a failure is retried on the next tick."""
        url = self._settings.healthchecks_url
        if not url or (self.last_ping_at is not None and now - self.last_ping_at < PING_EVERY):
            return
        try:
            async with httpx.AsyncClient(timeout=PING_TIMEOUT_SECONDS) as client:
                resp = await client.get(url)
            if resp.status_code >= 400:
                raise PingFailed(f"healthchecks answered HTTP {resp.status_code}")
        except (httpx.HTTPError, PingFailed) as exc:
            # Never the URL itself (it is the secret part): just what went wrong.
            self.last_ping_error = str(exc) if isinstance(exc, PingFailed) else type(exc).__name__
            log.warning("Scheduler Ping failed: %s", self.last_ping_error)
            return
        self.last_ping_at, self.last_ping_error = now, None

    # ------------------------------------------------------------------ firing

    def _fire_due(self, now: datetime) -> tuple[list[int], list[int]]:
        """Deal with every due firing; returns the Runs to start and the entries that ended at once."""
        started: list[int] = []
        ended: list[int] = []
        paused = self.paused
        with self._sessions() as session:
            for schedule in session.scalars(select(Schedule).order_by(Schedule.id)).all():
                first = cron.next_after(schedule.cron, self.zone, schedule.last_fired_at)
                if first is None or first > now:
                    continue
                newest = cron.last_at_or_before(schedule.cron, self.zone, now) or first
                mark, schedule.last_fired_at = schedule.last_fired_at, newest
                if paused or not schedule.enabled:
                    continue  # dropped, no record
                run, note = self._firing(session, schedule, mark, newest, now)
                session.add(run)
                session.flush()
                RunLog(self._supervisor.log_path(run.id)).append(note, kind="locomotive")
                (started if run.status == "queued" else ended).append(run.id)
            session.commit()
        return started, ended

    def _firing(
        self, session: Session, schedule: Schedule, mark: datetime, newest: datetime, now: datetime
    ) -> tuple[Run, str]:
        """The Run, or skipped entry, that one (collapsed) firing produces, and its first log line."""
        run = Run(
            playbook=schedule.playbook,
            status="queued",
            source="schedule",
            checkout="clean",  # scheduled Runs never use the Working Copy
            branch=schedule.branch or self._settings.default_branch,
            check=schedule.check,
            diff=schedule.diff,
            verbosity=schedule.verbosity,
            limit=schedule.limit,
            tags=list(schedule.tags or []),
            extra_vars=dict(schedule.extra_vars or {}),
            created_at=now,
            schedule_id=schedule.id,
            schedule_name=schedule.name,
        )
        due = f"Schedule {schedule.name!r} ({schedule.cron}) came due at {self._local(newest)}"
        if now - newest > MISSED_GRACE:
            # Too stale to run now. A "missed" entry (Notifications hook these) and no Run.
            count = cron.count_between(schedule.cron, self.zone, mark, newest, cap=MISSED_COUNT_CAP)
            firings = "1 firing" if count == 1 else f"{count}{'+' if count >= MISSED_COUNT_CAP else ''} firings"
            reason = (
                f"locomotive was not running: {firings} missed, the newest at {self._local(newest)}"
                f" (more than {MISSED_GRACE_TEXT} ago)"
            )
            self._end_entry(run, "missed", reason, now)
            return run, f"{due}. Missed: {reason}"
        previous = session.scalars(
            select(Run)
            .where(Run.schedule_id == schedule.id, Run.status.in_(("queued", "running")))
            .order_by(Run.id.desc())
        ).first()
        if previous is not None:
            reason = f"the Schedule's previous Run #{previous.id} is still {previous.status}"
            self._end_entry(run, "skipped", reason, now)
            return run, f"{due}. Skipped: {reason}"
        if schedule.playbook in self._settings.hidden_playbooks:
            reason = f"Playbook {schedule.playbook} is hidden on this Controller"
            self._end_entry(run, "failed", reason, now)
            return run, f"{due}. Not run: {reason}"
        return run, f"{due}; fired a clean Run of {run.branch}"

    @staticmethod
    def _end_entry(run: Run, status: str, reason: str, now: datetime) -> None:
        run.status, run.failure_reason, run.finished_at = status, reason, now

    def _local(self, when: datetime) -> str:
        return when.astimezone(self.zone).strftime("%Y-%m-%d %H:%M %Z")

    # ------------------------------------------------------------------ state

    @property
    def paused(self) -> bool:
        with self._sessions() as session:
            row = session.get(ControllerSetting, PAUSED_KEY)
        return bool(row and row.value)

    def set_paused(self, paused: bool) -> None:
        now = self._clock.now()
        with self._sessions() as session:
            row = session.get(ControllerSetting, PAUSED_KEY)
            was_paused = bool(row and row.value)
            if row is None:
                session.add(ControllerSetting(key=PAUSED_KEY, value=paused))
            else:
                row.value = paused
            if was_paused and not paused:
                # Whatever came due during the pause is dropped, even if no tick has seen it yet.
                for schedule in session.scalars(select(Schedule)):
                    schedule.last_fired_at = max(schedule.last_fired_at, now)
            session.commit()

    def next_firing(self, schedule: Schedule) -> datetime | None:
        after = max(schedule.last_fired_at, self._clock.now())
        return cron.next_after(schedule.cron, self.zone, after)

    def status(self) -> dict:
        def iso(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat().replace("+00:00", "Z")

        return {
            "paused": self.paused,
            "time_zone": self.zone.key,
            "last_tick_at": iso(self.last_tick_at),
            "ping_configured": bool(self._settings.healthchecks_url),
            "last_ping_at": iso(self.last_ping_at),
            "last_ping_error": self.last_ping_error,
        }

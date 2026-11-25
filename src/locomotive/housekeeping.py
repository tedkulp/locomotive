"""Housekeeping: bounds the disk Run logs take while every Run's history row is kept forever.

Not a Schedule (it produces no Run): its own loop runs one pass at startup and one daily at 04:00 local
(``time_zone``; days the app was down collapse into one pass). Each pass:

1. gzips finished Runs' files that aren't yet (a Run compresses its own as it ends; this catches the
   ones cut off by a shutdown or a crash, and Schedules' skipped/missed entries);
2. prunes by age: a quiet scheduled Run (no changes, no failures) after 14 days, every other Run after
   90 days;
3. prunes by budget: while all Run logs together exceed ``log_budget_bytes``, the oldest un-Kept finished
   Run's. If Kept Runs alone exceed the budget, nothing is pruned for the budget and one ntfy warning
   (priority 3) goes out, not repeated until the overflow has cleared once. New Runs are never refused.

Pruning deletes a Run's log, events and grid together; its row (summary, recap) stays, marked with the
date. Kept Runs are never pruned. Each pass is recorded with what it pruned, for the status page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from locomotive import cron, history
from locomotive.clock import Clock
from locomotive.models import ControllerSetting, HousekeepingPass, Run
from locomotive.notifier import DEFAULT, Message, Notifier
from locomotive.runlog import size_text
from locomotive.settings import Settings

logger = logging.getLogger(__name__)

DAILY = "0 4 * * *"
QUIET_DAYS = 14
DEFAULT_DAYS = 90
WARNED_KEY = "kept_logs_over_budget_warned"
SHOWN_PASSES = 30
WARNING_TITLE = "Kept logs exceed budget"


def is_quiet(run: Run) -> bool:
    """A scheduled Run that changed nothing and failed nowhere (or a firing skipped for that reason)."""
    if run.source != "schedule" or run.status not in ("succeeded", "skipped"):
        return False
    return not any(
        counts.get("changed") or counts.get("failed") or counts.get("unreachable")
        for counts in (run.recap or {}).values()
    )


def retention(run: Run) -> timedelta:
    return timedelta(days=QUIET_DAYS if is_quiet(run) else DEFAULT_DAYS)


@dataclass
class PassResult:
    pruned: list[dict] = field(default_factory=list)
    compressed: int = 0
    total_bytes: int = 0
    kept_bytes: int = 0
    warning: str | None = None  # set when Kept logs alone exceed the budget
    notify: bool = False  # the warning is new: send it

    @property
    def freed_bytes(self) -> int:
        return sum(p["bytes"] for p in self.pruned)


class Housekeeper:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        sessions: sessionmaker,
        history_dir: Callable[[int], Path],
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sessions = sessions
        self._history_dir = history_dir
        self._notifier = notifier
        self.zone = cron.local_zone(settings.time_zone)
        self.last_check_at: datetime | None = None
        self._last_pass_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="housekeeping")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        trigger = "startup"
        while True:
            now = self._clock.now().astimezone(UTC)
            due = self.next_pass_at
            if trigger == "startup" or (due is not None and now >= due):
                try:
                    await self.run_pass(trigger, now)
                except Exception:  # noqa: BLE001 - one bad pass must not stop housekeeping
                    logger.exception("housekeeping pass failed")
                trigger = "daily"
            self.last_check_at = now
            await asyncio.sleep(self._settings.housekeeping_interval_seconds)

    @property
    def next_pass_at(self) -> datetime | None:
        if self._last_pass_at is None:
            return None
        return cron.next_after(DAILY, self.zone, self._last_pass_at)

    # ------------------------------------------------------------------ one pass

    async def run_pass(self, trigger: str, now: datetime) -> None:
        self._last_pass_at = now
        with self._sessions() as session:
            record = HousekeepingPass(
                trigger=trigger, started_at=now, pruned=[], budget_bytes=self._settings.log_budget_bytes,
            )
            session.add(record)
            session.commit()
            pass_id = record.id
        result, error = PassResult(), None
        try:
            result = await asyncio.to_thread(self._pass, now)
        except Exception as exc:  # noqa: BLE001 - recorded on the pass, shown on the status page
            logger.exception("housekeeping pass failed")
            error = f"{type(exc).__name__}: {exc}"
        with self._sessions() as session:
            record = session.get_one(HousekeepingPass, pass_id)
            record.finished_at = self._clock.now()
            record.pruned = result.pruned
            record.compressed = result.compressed
            record.freed_bytes = result.freed_bytes
            record.total_bytes = result.total_bytes
            record.kept_bytes = result.kept_bytes
            record.warning = result.warning
            record.warning_sent = result.notify and self._notifier is not None and self._notifier.notify(
                Message(WARNING_TITLE, result.warning or "", priority=DEFAULT)
            )
            record.error = error
            session.commit()

    def _pass(self, now: datetime) -> PassResult:
        """The filesystem work, off the event loop."""
        result = PassResult()
        budget = self._settings.log_budget_bytes
        with self._sessions() as session:
            runs = session.scalars(select(Run).where(Run.log_pruned_at.is_(None)).order_by(Run.id)).all()
        sizes: dict[int, int] = {}
        for run in runs:
            directory = self._history_dir(run.id)
            if run.is_finished and history.compress(directory):
                result.compressed += 1
            sizes[run.id] = history.disk_usage(directory)
            if run.is_finished and sizes[run.id] != run.log_bytes:
                self._set(run.id, log_bytes=sizes[run.id])

        finished = [r for r in runs if r.is_finished and not r.keep]
        for run in finished:
            if now - (run.finished_at or run.created_at) > retention(run):
                self._prune(run, sizes, now, "age", result)

        total = sum(sizes.values())
        kept = sum(sizes[r.id] for r in runs if r.keep)
        if kept > budget:
            result.warning = (
                f"Kept Runs' logs take {size_text(kept)}, over the {size_text(budget)} log budget. Nothing "
                "was pruned for the budget: un-Keep some Runs (new Runs still start)."
            )
        else:
            oldest_first = sorted(
                (r for r in finished if r.id in sizes), key=lambda r: (r.finished_at or r.created_at, r.id)
            )
            for run in oldest_first:
                if total <= budget:
                    break
                total -= self._prune(run, sizes, now, "budget", result)
        result.total_bytes = sum(sizes.values())
        result.kept_bytes = kept
        result.notify = self._warned(result.warning is not None)
        return result

    def _prune(self, run: Run, sizes: dict[int, int], now: datetime, reason: str, result: PassResult) -> int:
        """Delete the Run's log, events and grid (unless it was Kept meanwhile); the bytes freed."""
        with self._sessions() as session:
            row = session.get_one(Run, run.id)
            if row.keep or row.log_pruned_at is not None:
                return 0
            history.prune(self._history_dir(run.id))
            row.log_pruned_at, row.log_bytes = now, 0
            session.commit()
        freed = sizes.pop(run.id, 0)
        result.pruned.append({"run_id": run.id, "playbook": run.playbook, "reason": reason, "bytes": freed})
        return freed

    def _warned(self, over: bool) -> bool:
        """Records whether Kept logs are over the budget; True when they just went over (warn once)."""
        with self._sessions() as session:
            row = session.get(ControllerSetting, WARNED_KEY)
            was = bool(row and row.value)
            if row is None:
                session.add(ControllerSetting(key=WARNED_KEY, value=over))
            else:
                row.value = over
            session.commit()
        return over and not was

    def _set(self, run_id: int, **fields: object) -> None:
        with self._sessions() as session:
            run = session.get_one(Run, run_id)
            for key, value in fields.items():
                setattr(run, key, value)
            session.commit()


# ---------------------------------------------------------------------- HTTP API

router = APIRouter()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def pass_json(record: HousekeepingPass) -> dict:
    return {
        "id": record.id,
        "trigger": record.trigger,
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
        "pruned": record.pruned or [],
        "compressed": record.compressed,
        "freed_bytes": record.freed_bytes,
        "total_bytes": record.total_bytes,
        "kept_bytes": record.kept_bytes,
        "budget_bytes": record.budget_bytes,
        "warning": record.warning,
        "warning_sent": record.warning_sent,
        "error": record.error,
    }


@router.get("/housekeeping")
async def housekeeping_status(request: Request) -> dict:
    """Log retention settings, when the next pass is due, and the recent passes (newest first)."""
    state = request.app.state
    keeper: Housekeeper = state.housekeeper
    with state.sessions() as session:
        records = session.scalars(
            select(HousekeepingPass).order_by(HousekeepingPass.id.desc()).limit(SHOWN_PASSES)
        ).all()
    settings: Settings = state.settings
    return {
        "time_zone": keeper.zone.key,
        "budget_bytes": settings.log_budget_bytes,
        "log_cap_bytes": settings.log_cap_bytes,
        "retention_days": {"quiet_scheduled": QUIET_DAYS, "other": DEFAULT_DAYS},
        "last_check_at": _iso(keeper.last_check_at),
        "next_pass_at": _iso(keeper.next_pass_at),
        "passes": [pass_json(r) for r in records],
    }


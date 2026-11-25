"""The notifier: one ntfy message per notable Run, published straight to the configured ntfy server.

Not via core, so a core outage doesn't swallow alerts. Deliveries happen in the background; a failed one
is retried, then recorded on the Run. Nothing here ever changes a Run's outcome.

``send`` is the small reusable entry point for messages that aren't about a Run (e.g. housekeeping's
"Kept logs exceed budget" warning); ``run_finished`` decides whether a Run that just ended is notable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session, sessionmaker

from locomotive.models import Run, Schedule
from locomotive.settings import Settings

log = logging.getLogger(__name__)

RETRIES = 3
TIMEOUT_SECONDS = 10.0
SHUTDOWN_GRACE_SECONDS = 5.0

HIGH = 4  # failed, interrupted, missed
DEFAULT = 3  # changes on a scheduled Run; housekeeping warnings


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: int = DEFAULT
    click: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Delivery:
    ok: bool
    attempts: int
    error: str | None = None  # what went wrong, never the token or the URL


class DeliveryFailed(Exception):
    pass


class Notifier:
    def __init__(self, settings: Settings, sessions: sessionmaker) -> None:
        self._settings = settings
        self._sessions = sessions
        self._tasks: set[asyncio.Task[Delivery]] = set()

    @property
    def configured(self) -> bool:
        return bool(self._settings.ntfy_url and self._settings.ntfy_topic)

    # ------------------------------------------------------------------ entry points

    def notify(self, message: Message, on_done: Callable[[Delivery], None] | None = None) -> bool:
        """Deliver ``message`` in the background (with retries). False if ntfy isn't configured."""
        if not self.configured:
            return False
        task = asyncio.get_running_loop().create_task(self._deliver(message, on_done), name="ntfy")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def send(self, message: Message) -> Delivery:
        """Deliver ``message`` now, retrying a failure ``RETRIES`` times with backoff."""
        attempts, error = 0, None
        delay = self._settings.ntfy_retry_seconds
        while attempts <= RETRIES:
            if attempts:
                await asyncio.sleep(delay)
                delay *= 2
            attempts += 1
            try:
                await self._publish(message)
                return Delivery(ok=True, attempts=attempts)
            except DeliveryFailed as exc:
                error = str(exc)
            except httpx.HTTPError as exc:
                # The exception text can carry the URL; its type says enough.
                error = f"could not reach ntfy ({type(exc).__name__})"
        log.warning("ntfy delivery failed after %d attempts: %s", attempts, error)
        return Delivery(ok=False, attempts=attempts, error=error)

    def run_finished(self, run_id: int) -> None:
        """A Run (or a Schedule's missed entry) just ended: notify if it is notable."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            message = self._message_for(session, run) if run is not None else None
        if message is None or not self.configured:
            return
        self._record(run_id, notification_status="pending", notification_attempts=0, notification_error=None)
        self.notify(message, on_done=lambda d: self._record(
            run_id,
            notification_status="sent" if d.ok else "failed",
            notification_attempts=d.attempts,
            notification_error=d.error,
        ))

    async def shutdown(self) -> None:
        """Give deliveries in flight (e.g. for Runs the shutdown interrupted) a moment, then drop them."""
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(set(self._tasks), timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ delivery

    async def _deliver(self, message: Message, on_done: Callable[[Delivery], None] | None) -> Delivery:
        delivery = await self.send(message)
        if on_done is not None:
            on_done(delivery)
        return delivery

    async def _publish(self, message: Message) -> None:
        """ntfy's JSON publishing (POST to the server root), so titles may hold any character."""
        settings = self._settings
        payload: dict[str, object] = {
            "topic": settings.ntfy_topic,
            "title": message.title,
            "message": message.body,
            "priority": message.priority,
        }
        if message.click:
            payload["click"] = message.click
        if message.tags:
            payload["tags"] = message.tags
        headers = {}
        if settings.ntfy_token is not None:
            headers["Authorization"] = f"Bearer {settings.ntfy_token.get_secret_value()}"
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.post(str(settings.ntfy_url).rstrip("/") + "/", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise DeliveryFailed(f"ntfy answered HTTP {resp.status_code}")

    def _record(self, run_id: int, **fields: object) -> None:
        with self._sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            for key, value in fields.items():
                setattr(run, key, value)
            session.commit()

    # ------------------------------------------------------------------ what a Run says

    def _message_for(self, session: Session, run: Run) -> Message | None:
        """The one message a Run gets, or None if it isn't notable.

        Failed and interrupted (any source) and missed firings at high priority; changes only on
        scheduled Runs whose Schedule has notify-on-changes on. Everything else stays silent.
        """
        click = self._click(run)
        if run.status == "missed":
            name = run.schedule_name or run.playbook
            body = "\n".join([f"Playbook: {run.playbook}", run.failure_reason or "A firing was missed"])
            return Message(title=f"{name} missed", body=body, priority=HIGH, click=click)
        if run.status in ("failed", "interrupted"):
            return Message(title=f"{run.playbook} {run.status}", body=run_summary(run), priority=HIGH, click=click)
        if run.status == "succeeded" and run.source == "schedule" and self._notify_on_changes(session, run):
            changed = hosts_with(run.recap, "changed")
            if changed:
                return Message(
                    title=f"{run.playbook}: changes on {len(changed)} host{'s' if len(changed) != 1 else ''}",
                    body=run_summary(run),
                    priority=DEFAULT,
                    click=click,
                )
        return None

    @staticmethod
    def _notify_on_changes(session: Session, run: Run) -> bool:
        """The Schedule's toggle; on (the default) if the Schedule has since been deleted."""
        schedule = session.get(Schedule, run.schedule_id) if run.schedule_id is not None else None
        return True if schedule is None else schedule.notify_on_changes

    def _click(self, run: Run) -> str | None:
        base = self._settings.public_url
        return f"{base.rstrip('/')}/runs/{run.id}" if base else None


# ---------------------------------------------------------------------- message body


def run_summary(run: Run) -> str:
    """What Ted needs to triage from his phone. Built from the Run record only: never log lines."""
    source = run.source
    if run.schedule_name:
        source = f"{source} ({run.schedule_name})"
    if run.checkout == "working_copy":
        commit = "working copy"
    elif run.sha:
        commit = f"{run.sha[:7]} ({run.branch})"
    else:
        commit = f"no commit yet ({run.branch})"
    lines = [
        f"Source: {source}",
        f"Check mode: {'yes' if run.check else 'no'}",
        f"Commit: {commit}",
        f"Duration: {format_duration(run)}",
    ]
    for key, label in (("failed", "Failed"), ("unreachable", "Unreachable"), ("changed", "Changed")):
        hosts = hosts_with(run.recap, key)
        if hosts:
            lines.append(f"{label} ({len(hosts)}): " + ", ".join(f"{h} ({n})" for h, n in hosts))
    task = run.first_failed_task
    if task:
        on = ", ".join(task.get("hosts") or [])
        lines.append(f"First failed task: {task.get('name') or '(unnamed)'}" + (f" on {on}" if on else ""))
    if run.status == "interrupted":
        lines.append("Interrupted: re-run it if it still needs to happen")
    elif not run.recap:
        lines.append("Failed before Ansible touched a host (see the Run page)")
    return "\n".join(lines)


def hosts_with(recap: dict | None, key: str) -> list[tuple[str, int]]:
    """Hosts whose recap count for ``key`` is non-zero, with the count, in recap order."""
    return [(host, counts[key]) for host, counts in (recap or {}).items() if counts.get(key)]


def format_duration(run: Run) -> str:
    start = run.started_at or run.created_at
    if start is None or run.finished_at is None:
        return "unknown"
    seconds = max(0, int((run.finished_at - start).total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"

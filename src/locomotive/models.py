from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from locomotive.db import Base, UTCDateTime

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "interrupted", "skipped", "missed"})
# History entries a Schedule leaves instead of a Run (never touched a host): the reason is in failure_reason.
NON_RUN_STATUSES = frozenset({"skipped", "missed"})


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playbook: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    checkout: Mapped[str] = mapped_column(String(32))
    branch: Mapped[str] = mapped_column(String(255))
    sha: Mapped[str | None] = mapped_column(String(64))
    check: Mapped[bool] = mapped_column(Boolean)
    diff: Mapped[bool] = mapped_column(Boolean, default=False)
    verbosity: Mapped[int] = mapped_column(Integer, default=0)
    limit: Mapped[str | None] = mapped_column(Text)  # as requested; None = every host the Playbook targets
    hosts: Mapped[list[str] | None] = mapped_column(JSON)  # the resolved host set (exact host preview)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    extra_vars: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requirements_hash: Mapped[str | None] = mapped_column(String(64))
    ansible_core_version: Mapped[str | None] = mapped_column(String(64))
    rc: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Per-host ok/changed/skipped/failed/unreachable, set when the Run ends.
    recap: Mapped[dict | None] = mapped_column(JSON)
    recap_source: Mapped[str | None] = mapped_column(String(16))  # "ansible" or "locomotive"
    cancel_request: Mapped[str | None] = mapped_column(String(16))  # "stop", "kill" or "cancel"
    # While queued: the Run it waits for, {"id", "playbook"} (the first one holding or ahead of its hosts).
    waiting_for: Mapped[dict | None] = mapped_column(JSON)
    # Working Copy Runs only (None for clean Runs), recorded as the Run starts.
    dirty: Mapped[bool | None] = mapped_column(Boolean)
    wc_diff: Mapped[str | None] = mapped_column(Text)  # `git diff HEAD`, capped at ~1 MB
    diff_truncated: Mapped[bool | None] = mapped_column(Boolean)
    untracked: Mapped[list[str] | None] = mapped_column(JSON)
    # Set on scheduled Runs and skipped/missed entries; the name is kept if the Schedule is deleted.
    schedule_id: Mapped[int | None] = mapped_column(Integer, index=True)
    schedule_name: Mapped[str | None] = mapped_column(String(255))

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES


class Schedule(Base):
    """Saved Run settings with a cron expression; each firing produces one clean Run."""

    __tablename__ = "schedules"
    __table_args__ = {"sqlite_autoincrement": True}  # ids are never reused: Runs keep pointing at theirs

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    playbook: Mapped[str] = mapped_column(String(255))
    branch: Mapped[str | None] = mapped_column(String(255))  # None = the default branch at firing time
    check: Mapped[bool] = mapped_column(Boolean)
    diff: Mapped[bool] = mapped_column(Boolean, default=False)
    verbosity: Mapped[int] = mapped_column(Integer, default=0)
    limit: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    extra_vars: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cron: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    # Every firing up to this instant has been dealt with (fired, skipped, missed or dropped).
    last_fired_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ControllerSetting(Base):
    """Global settings changed from the UI (e.g. ``schedules_paused``), as JSON values by key."""

    __tablename__ = "controller_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)

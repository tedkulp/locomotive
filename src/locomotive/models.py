from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from locomotive.db import Base, UTCDateTime

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "interrupted"})


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
    cancel_request: Mapped[str | None] = mapped_column(String(16))  # "stop" or "kill"

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

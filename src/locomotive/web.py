"""Small pieces the HTTP routers share (Runs, Schedules, status pages)."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException

from locomotive.checkout import PLAYBOOK_NAME
from locomotive.settings import Settings


def iso(value: datetime | None) -> str | None:
    """A timestamp as the API shows it: ISO 8601 in UTC with a ``Z``."""
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def check_playbook(settings: Settings, playbook: str) -> None:
    """422 unless ``playbook`` is a valid Playbook name that isn't hidden on this Controller."""
    if not PLAYBOOK_NAME.match(playbook):
        raise HTTPException(status_code=422, detail=f"invalid Playbook name {playbook!r}")
    if playbook in settings.hidden_playbooks:
        raise HTTPException(status_code=422, detail=f"Playbook {playbook} is hidden on this Controller")

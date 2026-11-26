"""A Run's task x host grid and per-host recap, folded from ansible-runner's job events.

The grid is persisted as ``grid.json`` in the Run's history dir so the Run page (and a Run cut short by a
kill, a shutdown or a crash) always has the latest picture without replaying events.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from locomotive.runlog import LogLine

RECAP_KEYS = ("ok", "changed", "skipped", "failed", "unreachable")
# playbook_on_stats key -> recap key
STATS_KEYS = {"ok": "ok", "changed": "changed", "skipped": "skipped", "failures": "failed", "dark": "unreachable"}
# Results that end a host's part in a task. Loop-item and retry events are folded into these by Ansible.
RESULT_EVENTS = {
    "runner_on_ok", "runner_on_failed", "runner_on_skipped", "runner_on_unreachable",
}
TASK_START_EVENTS = {"playbook_on_task_start", "playbook_on_handler_task_start"}
# How a task's banner line in the Run log starts.
BANNER_PREFIXES = ("TASK [", "RUNNING HANDLER [")


class RunGrid:
    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.tasks: list[dict[str, Any]] = []
        self._by_uuid: dict[str, dict[str, Any]] = {}
        self._included: dict[str, str] = {}  # included file path -> display name
        self.counts: dict[str, dict[str, int]] = {}
        self.stats: dict[str, dict[str, int]] | None = None
        self.version = 0
        # Tasks locomotive's stop plugin skipped to reach the notified handlers (see
        # ``ansible_plugins/locomotive_stop.py``): they never ran, so they get no cells.
        self.stop_skipped: set[str] = set()

    # ------------------------------------------------------------------ folding events

    def apply(self, event: dict[str, Any], written: Sequence[LogLine] = ()) -> None:
        """Folds one runner event in. ``written`` is what the event's stdout became in the Run log (see
        ``RunLog.append``), so the event's task row can be linked to its banner line there."""
        kind = event.get("event")
        data = event.get("event_data") or {}
        changed = True
        if kind in TASK_START_EVENTS:
            self._task(data)
        elif kind == "playbook_on_include":
            path = data.get("included_file")
            if path:
                self._included[path] = os.path.basename(path)
        elif kind == "runner_on_start":
            self._cell(data, "running")
        elif kind in RESULT_EVENTS:
            self._result(kind, data)
        elif kind == "playbook_on_stats":
            self.stats = _recap_from_stats(data)
        else:
            changed = False
        if self._link_banner(data, written) or changed:
            self.version += 1

    def _task(self, data: dict[str, Any]) -> dict[str, Any] | None:
        uuid = data.get("task_uuid")
        if not uuid:
            return None
        row = self._by_uuid.get(uuid)
        if row is None:
            path = (data.get("task_path") or "").rsplit(":", 1)[0]
            row = {
                "id": uuid,
                "name": data.get("task") or data.get("name") or "",
                "play": data.get("play"),
                "included_from": self._included.get(path),
                # seq of the task's banner line in the Run log, once written; None if it never was (log cap)
                "log_seq": None,
                "results": {},
            }
            self._by_uuid[uuid] = row
            self.tasks.append(row)
        return row

    def _link_banner(self, data: dict[str, Any], written: Sequence[LogLine]) -> bool:
        """Links the event's task row to its banner line ("TASK [...]", "RUNNING HANDLER [...]") if that is the
        first non-blank line of ``written``. Usually the task-start event writes it, but with
        ``display_skipped_hosts`` or ``display_ok_hosts`` off, Ansible's default callback defers the banner to
        the stdout of the first result it shows, so any event for the task can carry it. Nothing written (past
        the log cap) leaves the row unlinked; the first banner wins (a handler can run again). True if linked."""
        row = self._by_uuid.get(data.get("task_uuid") or "")
        if row is None or row.get("log_seq") is not None:
            return False
        first = next((line for line in written if line.text.strip()), None)
        if first is None or not first.text.lstrip().startswith(BANNER_PREFIXES):
            return False
        row["log_seq"] = first.seq
        return True

    def _cell(self, data: dict[str, Any], status: str) -> None:
        host = data.get("host")
        row = self._task(data)
        if not host or row is None:
            return
        if host not in self.hosts:
            self.hosts.append(host)
            self.counts[host] = dict.fromkeys(RECAP_KEYS, 0)
        row["results"][host] = status

    def _result(self, kind: str, data: dict[str, Any]) -> None:
        res = data.get("res") if isinstance(data.get("res"), dict) else {}
        if kind == "runner_on_skipped" and data.get("task_uuid") in self.stop_skipped:
            row, host = self._task(data), data.get("host")
            if row is not None and host:
                row["results"].pop(host, None)
            return
        is_changed = bool(res.get("changed"))
        if kind == "runner_on_ok":
            status = "changed" if is_changed else "ok"
        elif kind == "runner_on_failed":
            status = "ignored" if data.get("ignore_errors") else "failed"
        elif kind == "runner_on_skipped":
            status = "skipped"
        else:
            status = "unreachable"
        self._cell(data, status)
        host = data.get("host")
        if not host or host not in self.counts:
            return
        counts = self.counts[host]
        # Counted the way Ansible's own stats count them, so both recaps agree.
        if status in ("ok", "changed", "ignored"):
            counts["ok"] += 1
            if is_changed:
                counts["changed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status == "skipped":
            counts["skipped"] += 1
        else:
            counts["unreachable"] += 1

    # ------------------------------------------------------------------ the recap

    @property
    def recap(self) -> dict[str, dict[str, int]]:
        """Ansible's final stats when it emitted them, else locomotive's own count from events."""
        if self.stats is not None:
            return self.stats
        return {host: dict(counts) for host, counts in self.counts.items()}

    @property
    def first_failed_task(self) -> dict[str, Any] | None:
        """The first task (in run order) that failed on some host, with those hosts. Ignored errors don't count."""
        for row in self.tasks:
            hosts = [host for host, status in row["results"].items() if status == "failed"]
            if hosts:
                return {"name": row["name"], "hosts": hosts}
        return None

    @property
    def recap_source(self) -> str:
        return "ansible" if self.stats is not None else "locomotive"

    def finish(self) -> None:
        """The Run is over: a host still running a task never got a result."""
        for row in self.tasks:
            for host, status in row["results"].items():
                if status == "running":
                    row["results"][host] = "interrupted"
        self.version += 1

    # ------------------------------------------------------------------ persistence

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "hosts": self.hosts,
            "tasks": self.tasks,
            "counts": self.counts,
            "stats": self.stats,
            "included": self._included,
            "recap": self.recap,
            "recap_source": self.recap_source,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunGrid:
        grid = cls()
        grid.version = data.get("version", 0)
        grid.hosts = data.get("hosts", [])
        grid.tasks = data.get("tasks", [])
        for row in grid.tasks:
            row.setdefault("log_seq", None)  # saved before task rows linked to the log
        grid._by_uuid = {row["id"]: row for row in grid.tasks}
        grid.counts = data.get("counts", {})
        grid.stats = data.get("stats")
        grid._included = data.get("included", {})
        return grid

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json()))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> RunGrid:
        try:
            return cls.from_json(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            return cls()


def public_json(grid_json: dict[str, Any]) -> dict[str, Any]:
    """What the API shows of a grid."""
    return {
        "version": grid_json.get("version", 0),
        "hosts": grid_json.get("hosts", []),
        "tasks": grid_json.get("tasks", []),
        "recap": grid_json.get("recap", {}),
        "recap_source": grid_json.get("recap_source"),
    }


def _recap_from_stats(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    hosts: list[str] = []
    for key in (*STATS_KEYS, "processed", "ignored", "rescued"):
        for host in data.get(key) or {}:
            if host not in hosts:
                hosts.append(host)
    return {
        host: {ours: int((data.get(theirs) or {}).get(host, 0)) for theirs, ours in STATS_KEYS.items()}
        for host in hosts
    }

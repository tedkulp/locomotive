"""A Run's log: an append-only file of numbered lines in the Run's data directory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True)
class LogLine:
    seq: int
    text: str
    kind: str  # "locomotive" (supervisor) or "ansible"
    host: str | None = None  # the host an Ansible line is about, if any


class RunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = _last_seq(path)

    def append(self, text: str, kind: str = "ansible", host: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for line in ANSI.sub("", text).splitlines() or [""]:
                self._seq += 1
                item = {"seq": self._seq, "text": line, "kind": kind, "host": host}
                f.write(json.dumps(item) + "\n")

    @staticmethod
    def read(path: Path, after: int = 0) -> list[LogLine]:
        """Complete lines with seq > ``after``. Safe to call while the file is being written."""
        if not path.exists():
            return []
        lines: list[LogLine] = []
        with path.open(encoding="utf-8") as f:
            for raw in f:
                if not raw.endswith("\n"):
                    break  # a line still being written
                item = json.loads(raw)
                if item["seq"] > after:
                    lines.append(LogLine(item["seq"], item["text"], item["kind"], item.get("host")))
        return lines


class LogTail:
    """Reads a growing log incrementally from a byte offset, so following a long Run stays cheap."""

    def __init__(self, path: Path, after: int = 0) -> None:
        self.path = path
        self.after = after
        self._offset = 0

    def read(self) -> list[LogLine]:
        if not self.path.exists():
            return []
        lines: list[LogLine] = []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written; read it whole next time
                self._offset += len(raw)
                item = json.loads(raw)
                if item["seq"] > self.after:
                    self.after = item["seq"]
                    lines.append(LogLine(item["seq"], item["text"], item["kind"], item.get("host")))
        return lines


def _last_seq(path: Path) -> int:
    """Continue numbering an existing log (e.g. a Run marked interrupted at startup)."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            chunk = b""
            while pos > 0 and chunk.count(b"\n") < 2:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + chunk
    except OSError:
        return 0
    for raw in reversed(chunk.splitlines()):
        try:
            return int(json.loads(raw)["seq"])
        except (ValueError, KeyError, TypeError):
            continue
    return 0

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


class RunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._seq = 0

    def append(self, text: str, kind: str = "ansible") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for line in ANSI.sub("", text).splitlines() or [""]:
                self._seq += 1
                f.write(json.dumps({"seq": self._seq, "text": line, "kind": kind}) + "\n")

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
                    lines.append(LogLine(item["seq"], item["text"], item["kind"]))
        return lines

"""A Run's log: an append-only file of numbered lines in the Run's data directory.

While the Run goes it is plain JSON lines (``log.jsonl``); once it has finished it is gzip-compressed
(``log.jsonl.gz``, see ``history``). Readers take whichever is there.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True)
class LogLine:
    seq: int
    text: str
    kind: str  # "locomotive" (supervisor) or "ansible"
    host: str | None = None  # the host an Ansible line is about, if any


def gz_path(path: Path) -> Path:
    return path.with_name(path.name + ".gz")


def open_log(path: Path) -> IO[bytes] | None:
    """The log as a binary stream (decompressed if the Run is finished); None if there is none."""
    try:
        return path.open("rb")
    except FileNotFoundError:
        pass
    try:
        return gzip.open(gz_path(path), "rb")
    except FileNotFoundError:
        return None


def size_text(n: int) -> str:
    """200 MB, 19.5 KB, 512 bytes."""
    if n < 1024:
        return f"{n} bytes"
    size = float(n)
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            break
    return f"{round(size, 1):g} {unit}"


class RunLog:
    """Appends to a Run's log. With ``cap``, Ansible output stops once the file reaches that many bytes:
    one "Log truncated" line goes in, ``on_truncate`` is called, and later Ansible lines are dropped.
    locomotive's own lines (a handful per Run) always go in, so the log still says how the Run ended."""

    def __init__(
        self, path: Path, cap: int | None = None, on_truncate: Callable[[], None] | None = None
    ) -> None:
        self.path = path
        self._seq = _last_seq(path)
        self._cap = cap
        self._on_truncate = on_truncate
        try:
            self._bytes = path.stat().st_size
        except OSError:
            self._bytes = 0
        self.truncated = cap is not None and self._bytes >= cap

    def append(self, text: str, kind: str = "ansible", host: str | None = None) -> None:
        if self.truncated and kind != "locomotive":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for line in ANSI.sub("", text).splitlines() or [""]:
                item = {"seq": self._seq + 1, "text": line, "kind": kind, "host": host}
                raw = json.dumps(item) + "\n"
                if kind != "locomotive" and self._cap is not None and self._bytes + len(raw) > self._cap:
                    self._truncate(f)
                    return
                self._seq += 1
                self._bytes += len(raw)  # json.dumps escapes to ASCII: chars are bytes
                f.write(raw)

    def _truncate(self, f: IO[str]) -> None:
        self.truncated = True
        self._seq += 1
        text = (
            f"Log truncated: it reached the {size_text(self._cap or 0)} cap. The Run carries on; its grid and "
            "recap stay complete, but further output is not kept"
        )
        f.write(json.dumps({"seq": self._seq, "text": text, "kind": "locomotive", "host": None}) + "\n")
        if self._on_truncate is not None:
            self._on_truncate()

    @staticmethod
    def read(path: Path, after: int = 0) -> list[LogLine]:
        """Complete lines with seq > ``after``. Safe to call while the file is being written."""
        f = open_log(path)
        if f is None:
            return []
        lines: list[LogLine] = []
        with f:
            for raw in f:
                if not raw.endswith(b"\n"):
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
        f = open_log(self.path)  # offsets are the same in the plain and the gzipped log
        if f is None:
            return []
        lines: list[LogLine] = []
        with f:
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

"""A Run's log: an append-only file of numbered lines in the Run's data directory.

While the Run goes it is plain JSON lines (``log.jsonl``); once it has finished it is gzip-compressed
(``log.jsonl.gz`` in gzip members of about 1 MB, indexed by ``log.jsonl.idx``, see ``history``). Readers take
whichever is there and seek to where they start reading, so a read costs what it returns.
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


def index_path(path: Path) -> Path:
    """The gzipped log's index: ``[[first seq, plain offset, gzip offset], ...]``, one per gzip member."""
    return path.with_name(path.name + ".idx")


def _open_at(path: Path, after: int = 0, offset: int | None = None) -> tuple[IO[bytes], int] | None:
    """The log as a binary stream positioned on a line boundary, and that position as a plain-log offset.

    At plain offset ``offset`` if given, else early enough that reading on finds every line with seq >
    ``after``, without reading what comes before: a binary search over the plain log (seqs only grow), or
    the index into the gzipped one's members. None if there is no log.
    """
    try:
        f = path.open("rb")
    except FileNotFoundError:
        return _open_gz_at(path, after, offset)
    pos = offset if offset is not None else _bisect(f, after)
    f.seek(pos)
    return f, pos


SCAN_BYTES = 64 * 1024  # below this, scanning beats seeking


def _bisect(f: IO[bytes], after: int) -> int:
    """A line start in the plain log with only lines of seq <= ``after`` before it."""
    lo, hi = 0, f.seek(0, 2)
    while hi - lo > SCAN_BYTES:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()  # to the next line start
        start = f.tell()
        raw = f.readline()
        if start >= hi or not raw.endswith(b"\n"):
            hi = mid
        elif json.loads(raw)["seq"] <= after:
            lo = start + len(raw)
        else:
            hi = mid
    return lo


def _open_gz_at(path: Path, after: int, offset: int | None) -> tuple[IO[bytes], int] | None:
    try:
        raw = gz_path(path).open("rb")
    except FileNotFoundError:
        return None
    try:
        index = json.loads(index_path(path).read_text())
    except (OSError, ValueError):
        index = []  # compressed before members were indexed: read from the start
    entry = [0, 0, 0]
    for item in index:
        if (item[1] <= offset) if offset is not None else (item[0] <= after + 1):
            entry = item
        else:
            break
    raw.seek(entry[2])
    f = gzip.GzipFile(fileobj=raw, mode="rb")
    f.myfileobj = raw  # closed with the GzipFile, as gzip.open does
    pos = entry[1]
    while offset is not None and pos < offset:  # within one member: at most a member's worth
        chunk = f.read(min(offset - pos, 1024 * 1024))
        if not chunk:
            break
        pos += len(chunk)
    return f, pos


def _line(item: dict) -> LogLine:
    return LogLine(item["seq"], item["text"], item["kind"], item.get("host"))


def read_page(
    path: Path, after: int = 0, limit: int | None = None, host: str | None = None
) -> tuple[list[LogLine], bool]:
    """Complete lines with seq > ``after`` (with ``host``: that host's lines and lines about no host), at most
    ``limit`` of them, and whether more are already there. Costs what it reads, not the log before ``after``.
    Safe to call while the file is being written; blocking file I/O, so call it off the event loop."""
    opened = _open_at(path, after)
    if opened is None:
        return [], False
    f, _pos = opened
    lines: list[LogLine] = []
    with f:
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # a line still being written
            item = json.loads(raw)
            if item["seq"] <= after or (host is not None and item.get("host") not in (None, host)):
                continue
            if limit is not None and len(lines) >= limit:
                return lines, True
            lines.append(_line(item))
    return lines, False


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

    def append(self, text: str, kind: str = "ansible", host: str | None = None) -> list[LogLine]:
        """Appends ``text`` line by line; returns the lines that went in (none past the cap)."""
        written: list[LogLine] = []
        if self.truncated and kind != "locomotive":
            return written
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for line in ANSI.sub("", text).splitlines() or [""]:
                item = {"seq": self._seq + 1, "text": line, "kind": kind, "host": host}
                raw = json.dumps(item) + "\n"
                if kind != "locomotive" and self._cap is not None and self._bytes + len(raw) > self._cap:
                    self._truncate(f)
                    return written
                self._seq += 1
                self._bytes += len(raw)  # json.dumps escapes to ASCII: chars are bytes
                f.write(raw)
                written.append(LogLine(self._seq, line, kind, host))
        return written

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
        return read_page(path, after)[0]


class LogTail:
    """Reads a growing log incrementally, so following a long Run stays cheap: the first read starts at
    ``after`` (found without reading the log before it), each later one where the last stopped, at most
    ``max_lines`` lines at a time. Blocking file I/O: call ``read`` off the event loop."""

    def __init__(self, path: Path, after: int = 0, max_lines: int | None = None) -> None:
        self.path = path
        self.after = after
        self.max_lines = max_lines
        self._offset: int | None = None  # plain-log offset (the same in the plain and the gzipped log)

    def read(self) -> list[LogLine]:
        opened = _open_at(self.path, self.after, self._offset)
        if opened is None:
            return []
        f, pos = opened
        lines: list[LogLine] = []
        with f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written; read it whole next time
                pos += len(raw)
                item = json.loads(raw)
                if item["seq"] > self.after:
                    self.after = item["seq"]
                    lines.append(_line(item))
                    if self.max_lines is not None and len(lines) >= self.max_lines:
                        break
        self._offset = pos
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

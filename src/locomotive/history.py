"""A Run's kept data on disk (``history/<id>/``): its log, the grid and ansible-runner's (capped) output.
ansible-runner's event files are deleted as the supervisor folds them into the log and grid, and runner
writes no stdout file (both would repeat the output past the log cap); Runs from before that keep theirs. Finished Runs' files are gzip-compressed; housekeeping measures and prunes them as one unit."""

from __future__ import annotations

import gzip
import json
import os
import shutil
from pathlib import Path

GZIP_LEVEL = 6  # most of the gain of 9 at a fraction of the time on a 200 MB log
LOG_FILE = "log.jsonl"
PLAIN_FILES = ("runner.out",)
MEMBER_BYTES = 1024 * 1024  # the log's gzip members: a reader seeks to one and decompresses at most this


def compress(directory: Path) -> bool:
    """Gzip a finished Run's files in place: the log, ansible-runner's output, and its job events (packed
    into one ``job_events.jsonl.gz``, in counter order). True if there was anything left to compress."""
    done = _gzip_log(directory / LOG_FILE)
    for name in PLAIN_FILES:
        done |= _gzip(directory / name)
    artifacts = directory / "artifacts"
    if artifacts.is_dir():
        for ident in artifacts.iterdir():
            if ident.is_dir() and not ident.is_symlink():
                done |= _gzip(ident / "stdout")
                done |= _pack_events(ident / "job_events")
    return done


def disk_usage(directory: Path) -> int:
    """Bytes the Run's files take; 0 once pruned."""
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def prune(directory: Path) -> None:
    """Delete the log, events and grid together. The Run's database row is untouched."""
    shutil.rmtree(directory, ignore_errors=True)


def _gzip(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    target = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.tmp")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=GZIP_LEVEL) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    tmp.replace(target)  # a reader sees the plain file, or the whole gzip (offsets are the same)
    path.unlink()
    return True


def _gzip_log(path: Path) -> bool:
    """Gzip the log as a series of members of about ``MEMBER_BYTES`` (whole lines each) plus an index of
    ``[first seq, plain offset, gzip offset]`` per member, so a reader can start anywhere (``runlog``)."""
    if not path.is_file() or path.is_symlink():
        return False
    target = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.tmp")
    index: list[list[int]] = []
    plain = packed = 0
    with path.open("rb") as src, tmp.open("wb") as dst:
        chunk: list[bytes] = []
        size = 0

        def flush() -> None:
            nonlocal plain, packed, chunk, size
            if not chunk:
                return
            try:
                first = int(json.loads(chunk[0])["seq"])
            except (ValueError, KeyError, TypeError):
                first = index[-1][0] if index else 0
            member = gzip.compress(b"".join(chunk), compresslevel=GZIP_LEVEL, mtime=0)
            index.append([first, plain, packed])
            dst.write(member)
            plain += size
            packed += len(member)
            chunk, size = [], 0

        for raw in src:
            chunk.append(raw)
            size += len(raw)
            if size >= MEMBER_BYTES:
                flush()
        flush()
    idx = path.with_name(path.name + ".idx")
    idx_tmp = path.with_name(path.name + ".idx.tmp")
    idx_tmp.write_text(json.dumps(index))
    idx_tmp.replace(idx)  # in place before the gzip: a reader takes the plain log while it exists
    tmp.replace(target)
    path.unlink()
    return True


def _pack_events(directory: Path) -> bool:
    if not directory.is_dir() or directory.is_symlink():
        return False
    names = [n for n in os.listdir(directory) if n.endswith(".json") and n.split("-", 1)[0].isdigit()]
    names.sort(key=lambda n: int(n.split("-", 1)[0]))
    if not names:  # the supervisor deletes each event once folded into the log and grid
        shutil.rmtree(directory, ignore_errors=True)
        return False
    tmp = directory.with_name(directory.name + ".jsonl.gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=GZIP_LEVEL) as dst:
        for name in names:
            try:
                event = json.loads((directory / name).read_text())
            except (OSError, json.JSONDecodeError):
                continue
            dst.write(json.dumps(event) + "\n")
    tmp.replace(directory.with_name(directory.name + ".jsonl.gz"))
    shutil.rmtree(directory, ignore_errors=True)
    return True

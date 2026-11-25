"""A Run's kept data on disk (``history/<id>/``): its log, ansible-runner's events and output, and the
grid. Finished Runs' files are gzip-compressed; housekeeping measures and prunes them as one unit."""

from __future__ import annotations

import gzip
import json
import os
import shutil
from pathlib import Path

GZIP_LEVEL = 6  # most of the gain of 9 at a fraction of the time on a 200 MB log
PLAIN_FILES = ("log.jsonl", "runner.out")


def compress(directory: Path) -> bool:
    """Gzip a finished Run's files in place: the log, ansible-runner's output, and its job events (packed
    into one ``job_events.jsonl.gz``, in counter order). True if there was anything left to compress."""
    done = False
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


def _pack_events(directory: Path) -> bool:
    if not directory.is_dir() or directory.is_symlink():
        return False
    names = [n for n in os.listdir(directory) if n.endswith(".json") and n.split("-", 1)[0].isdigit()]
    names.sort(key=lambda n: int(n.split("-", 1)[0]))
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

"""Working Copy: the user's own clone, which Runs may execute in place, dirty files and all.

Part of the Checkout module. locomotive only reads the Working Copy and runs in it: it never commits,
pulls, pushes or fetches it, and it reads it without taking git's optional locks, so even ``git status``
doesn't refresh its index. The only git commands run here are read-only ones (``status``, ``diff``,
``rev-parse``, ``ls-files``).
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from locomotive.checkout import PLAYBOOK_NAME, CheckoutError
from locomotive.descriptions import PlaybookDescription, playbook_description, read_text

DIFF_CAP_BYTES = 1_000_000
AB = re.compile(r"^\+(\d+) -(\d+)$")


class WorkingCopyUnavailable(CheckoutError):
    """No Working Copy is configured, or it isn't a git clone locomotive can read."""


@dataclass(frozen=True)
class WorkingCopyStatus:
    path: Path
    branch: str | None  # None when HEAD is detached
    head_sha: str
    changed: list[str]  # tracked files that differ from HEAD (staged or not)
    untracked: list[str]
    upstream: str | None
    ahead: int | None
    behind: int | None

    @property
    def dirty(self) -> bool:
        return bool(self.changed or self.untracked)


@dataclass(frozen=True)
class WorkingCopySnapshot:
    """What a Working Copy Run records: HEAD, the dirty flag, the diff against HEAD and untracked files."""

    status: WorkingCopyStatus
    diff: str
    diff_truncated: bool


class WorkingCopy:
    def __init__(self, path: Path | None, hidden_playbooks: list[str]) -> None:
        self._path = path
        self._hidden = set(hidden_playbooks)

    @property
    def configured(self) -> bool:
        return self._path is not None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise WorkingCopyUnavailable("no Working Copy is configured on this Controller")
        return self._path

    # ------------------------------------------------------------------ reads

    async def status(self) -> WorkingCopyStatus:
        path = self.path
        if not path.is_dir():
            raise WorkingCopyUnavailable(f"the Working Copy {path} does not exist")
        out = await self._git("status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
        head_sha = branch = upstream = None
        ahead = behind = None
        changed: list[str] = []
        untracked: list[str] = []
        fields = out.split("\0")
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if entry.startswith("# branch.oid "):
                head_sha = entry.removeprefix("# branch.oid ")
            elif entry.startswith("# branch.head "):
                name = entry.removeprefix("# branch.head ")
                branch = None if name == "(detached)" else name
            elif entry.startswith("# branch.upstream "):
                upstream = entry.removeprefix("# branch.upstream ")
            elif entry.startswith("# branch.ab "):
                m = AB.match(entry.removeprefix("# branch.ab "))
                if m:
                    ahead, behind = int(m.group(1)), int(m.group(2))
            elif entry.startswith("1 "):
                changed.append(entry.split(" ", 8)[8])
            elif entry.startswith("2 "):
                changed.append(entry.split(" ", 9)[9])
                i += 1  # the original path of a rename/copy follows as its own field
            elif entry.startswith("u "):
                changed.append(entry.split(" ", 10)[10])
            elif entry.startswith("? "):
                untracked.append(entry[2:])
        if head_sha is None or head_sha == "(initial)":
            raise WorkingCopyUnavailable(f"the Working Copy {path} has no commits")
        return WorkingCopyStatus(
            path=path,
            branch=branch,
            head_sha=head_sha,
            changed=sorted(changed),
            untracked=sorted(untracked),
            upstream=upstream,
            ahead=ahead,
            behind=behind,
        )

    async def snapshot(self) -> WorkingCopySnapshot:
        status = await self.status()
        raw = await self._git_bytes("diff", "--no-color", "--no-ext-diff", "HEAD")
        truncated = len(raw) > DIFF_CAP_BYTES
        diff = raw[:DIFF_CAP_BYTES].decode(errors="replace")
        return WorkingCopySnapshot(status=status, diff=diff, diff_truncated=truncated)

    def list_playbooks(self) -> list[str]:
        """Playbooks as they are on disk now, uncommitted ones included."""
        directory = self.path / "playbooks"
        if not directory.is_dir():
            return []
        return sorted(
            p.name for p in directory.iterdir()
            if p.is_file() and PLAYBOOK_NAME.match(p.name) and p.name not in self._hidden
        )

    def describe_playbooks(self, names: list[str]) -> dict[str, PlaybookDescription]:
        """Each Playbook's Description as it is on disk now (never remembered: the files change)."""
        return {name: playbook_description(read_text(self.path / "playbooks" / name)) for name in names}

    # ------------------------------------------------------------------ git plumbing (read-only)

    async def _git_bytes(self, *args: str) -> bytes:
        env = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",  # never write the index (status would otherwise refresh it)
            "GIT_TERMINAL_PROMPT": "0",
        }
        proc = await asyncio.create_subprocess_exec(
            "git", "--no-pager", *args,
            cwd=self.path, env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip() or f"exit code {proc.returncode}"
            raise WorkingCopyUnavailable(f"Reading the Working Copy failed: {detail}")
        return out

    async def _git(self, *args: str) -> str:
        return (await self._git_bytes(*args)).decode(errors="replace")

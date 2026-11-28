"""Checkout module: a bare mirror of the repo, fetched before use, exported per Run with ``git archive``.

locomotive never creates worktrees or branches, and never writes to the remote.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from locomotive.descriptions import MAX_READ_BYTES, PlaybookDescription, playbook_description
from locomotive.settings import Settings

PLAYBOOK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
BRANCH_NAME = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")
KEEP_DESCRIPTIONS = 16  # commits whose Playbook Descriptions are remembered (a commit never changes)


class CheckoutError(Exception):
    """A git operation failed; the message is fit to show to the user."""


class UnknownBranch(CheckoutError):
    """The branch doesn't exist (or isn't a valid name)."""


@dataclass(frozen=True)
class PlaybookList:
    branch: str
    sha: str
    playbooks: list[str]
    descriptions: dict[str, PlaybookDescription]


class Checkout:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mirror = settings.data_dir / "mirror.git"
        self._lock = asyncio.Lock()
        self._descriptions: dict[str, dict[str, PlaybookDescription]] = {}  # by commit, least recently used first

    # ------------------------------------------------------------------ public

    async def fetch(self, branch: str) -> str:
        """Bring the mirror up to date with the remote and resolve ``branch`` to a SHA."""
        if not BRANCH_NAME.match(branch):
            raise UnknownBranch(f"invalid branch name {branch!r}")
        async with self._lock:
            await self._sync()
            out = await self._git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}",
                what=f"Resolve branch {branch}", allow_fail=True,
            )
        if out is None:
            raise UnknownBranch(f"branch {branch!r} does not exist on the remote")
        return out.strip()

    async def list_branches(self) -> list[str]:
        """The remote's branches, fetched now."""
        async with self._lock:
            await self._sync()
            out = await self._git("for-each-ref", "--format=%(refname:strip=2)", "refs/heads/", what="List branches")
        return sorted(name for name in (out or "").splitlines() if BRANCH_NAME.match(name))

    async def _sync(self) -> None:
        """Clone or fetch the mirror (caller holds the lock)."""
        if not (self._mirror / "HEAD").exists():
            await self._git(
                "clone", "--mirror", "--quiet", self._settings.repo_url, str(self._mirror),
                cwd=self._settings.data_dir, what="Fetch",
            )
        else:
            await self._git("remote", "set-url", "origin", self._settings.repo_url, what="Fetch")
            await self._git("fetch", "--prune", "--quiet", "origin", what="Fetch")

    async def has_commit(self, sha: str) -> bool:
        """Whether the mirror already has commit ``sha`` (no fetch)."""
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not (self._mirror / "HEAD").exists():
            return False
        out = await self._git("cat-file", "-e", f"{sha}^{{commit}}", what="Look up commit", allow_fail=True)
        return out is not None

    async def list_playbooks(self, branch: str) -> PlaybookList:
        sha = await self.fetch(branch)
        out = await self._git("ls-tree", "--name-only", f"{sha}:playbooks", what="List Playbooks", allow_fail=True)
        hidden = set(self._settings.hidden_playbooks)
        names = sorted(
            name
            for name in (out or "").splitlines()
            if PLAYBOOK_NAME.match(name) and name not in hidden
        )
        return PlaybookList(branch=branch, sha=sha, playbooks=names, descriptions=await self._describe(sha, names))

    async def _describe(self, sha: str, names: list[str]) -> dict[str, PlaybookDescription]:
        """Each Playbook's Description at ``sha``, read from its raw text (remembered per commit)."""
        known = self._descriptions.pop(sha, {})
        missing = [name for name in names if name not in known]
        if missing:
            texts = await self._read_blobs(sha, [f"playbooks/{name}" for name in missing])
            for name, text in zip(missing, texts, strict=True):
                known[name] = playbook_description(text)
        self._descriptions[sha] = known
        if len(self._descriptions) > KEEP_DESCRIPTIONS:
            del self._descriptions[next(iter(self._descriptions))]
        return {name: known[name] for name in names}

    async def _read_blobs(self, sha: str, paths: list[str]) -> list[str | None]:
        """The text of each file at ``sha`` (up to ``MAX_READ_BYTES``), None where it isn't a file."""
        query = "".join(f"{sha}:{path}\n" for path in paths).encode()
        out = await self._git_bytes("cat-file", "--batch", what="Read Playbooks", stdin=query) or b""
        texts: list[str | None] = []
        pos = 0
        for _ in paths:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            pos = end + 1
            if len(header) != 3:  # "<name> missing" (or ambiguous)
                texts.append(None)
                continue
            size = int(header[2])
            body = out[pos : pos + size]
            pos += size + 1  # the contents are followed by a newline
            texts.append(body[:MAX_READ_BYTES].decode(errors="replace") if header[1] == b"blob" else None)
        return texts

    async def export(self, sha: str, dest: Path) -> None:
        """Write the tree at ``sha`` into ``dest`` (which must not exist yet)."""
        tar_bytes = await self._git_bytes("archive", "--format=tar", sha, what="Export")
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            tar.extractall(dest, filter="data")

    # ------------------------------------------------------------------ git plumbing

    def _env(self) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
        token = self._settings.repo_token
        if token is not None:
            creds = f"{self._settings.repo_username}:{token.get_secret_value()}"
            header = "Authorization: Basic " + base64.b64encode(creds.encode()).decode()
            # Passed via env so the token never shows up in a process list or the mirror's config.
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.extraHeader",
                GIT_CONFIG_VALUE_0=header,
            )
        return env

    async def _git_bytes(
        self, *args: str, what: str, cwd: Path | None = None, allow_fail: bool = False, stdin: bytes | None = None
    ) -> bytes | None:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd or self._mirror,
            env=self._env(),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(stdin)
        if proc.returncode != 0:
            if allow_fail:
                return None
            detail = self._redact(err.decode(errors="replace").strip()) or f"exit code {proc.returncode}"
            raise CheckoutError(f"{what} failed: {detail}")
        return out

    async def _git(
        self, *args: str, what: str, cwd: Path | None = None, allow_fail: bool = False
    ) -> str | None:
        out = await self._git_bytes(*args, what=what, cwd=cwd, allow_fail=allow_fail)
        return None if out is None else out.decode(errors="replace")

    def _redact(self, text: str) -> str:
        token = self._settings.repo_token
        if token is not None and token.get_secret_value():
            text = text.replace(token.get_secret_value(), "***")
        return text

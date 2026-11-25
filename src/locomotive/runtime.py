"""The checkout's own Ansible runtime: installed with mise, collections cached by requirements hash.

Everything Ansible runs in subprocesses started through the checkout's mise toolchain; no ansible-core
code runs in this process. The preview helper is run with the Python interpreter of that toolchain's
ansible-core, so it sees exactly the ansible-core version (and collections) a Run would use.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locomotive.settings import Settings

REQUIREMENTS = Path("collections/requirements.yml")
CORE_VERSION = re.compile(r"core ([0-9][0-9A-Za-z.+-]*)")
PYTHON_PATH = re.compile(r"^\s*python version = .*\((/[^()]+)\)\s*$", re.MULTILINE)
HELPER = Path(__file__).parent / "preview_helper.py"
NOISE = ("Run with --verbose", "mise ERROR Version:")

Log = Callable[[str], None]


class ToolchainError(Exception):
    """A runtime step failed; the message is fit to show to Ted (and is a Run's failure reason)."""


class PreviewTimeout(ToolchainError):
    """The preview helper ran out of time."""


@dataclass(frozen=True)
class Runtime:
    """A prepared runtime for one checkout directory."""

    project: Path
    ansible_core_version: str
    python: str
    requirements_hash: str | None
    collections_path: Path | None


class Toolchain:
    """Prepares checkouts' runtimes and runs things in them."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._collection_locks: dict[str, asyncio.Lock] = {}

    @property
    def collections_dir(self) -> Path:
        return self._settings.data_dir / "collections"

    # ------------------------------------------------------------------ prepare

    async def prepare(
        self,
        project: Path,
        log: Log | None = None,
        on_step: Callable[..., None] | None = None,
    ) -> Runtime:
        """``mise install``, find ansible-core's version and interpreter, install collections.

        ``on_step(**fields)`` is told each recorded fact as soon as it is known (for the Run record).
        """
        say = log or (lambda _line: None)
        record = on_step or (lambda **_fields: None)

        say("$ mise install")
        code, out = await self.run_tool(["install"], project, log)
        if code != 0:
            raise ToolchainError(f"Runtime install failed: mise install exited {code}{last_line(out)}")

        code, out = await self.run_tool(["exec", "-C", str(project), "--", "ansible", "--version"], project, None)
        version = CORE_VERSION.search(out)
        python = PYTHON_PATH.search(out)
        if code != 0 or not version or not python:
            raise ToolchainError(f"Could not determine the ansible-core version{last_line(out)}")
        record(ansible_core_version=version.group(1))
        say(f"ansible-core {version.group(1)}")

        digest, collections = await self._install_collections(project, say, log, record)
        return Runtime(
            project=project,
            ansible_core_version=version.group(1),
            python=python.group(1),
            requirements_hash=digest,
            collections_path=collections,
        )

    async def _install_collections(
        self, project: Path, say: Log, log: Log | None, record: Callable[..., None]
    ) -> tuple[str | None, Path | None]:
        requirements = project / REQUIREMENTS
        if not requirements.is_file():
            say(f"No {REQUIREMENTS}; skipping collections")
            return None, None
        digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
        record(requirements_hash=digest)
        target = self.collections_dir / digest
        lock = self._collection_locks.setdefault(digest, asyncio.Lock())
        async with lock:
            if target.is_dir():
                say(f"Collections for requirements {digest[:12]} already installed")
                return digest, target
            staging = self.collections_dir / f".staging-{digest}-{os.getpid()}-{id(project)}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.parent.mkdir(parents=True, exist_ok=True)
            say(f"$ ansible-galaxy collection install -r {REQUIREMENTS}")
            code, out = await self.run_tool(
                ["exec", "-C", str(project), "--", "ansible-galaxy", "collection", "install",
                 "-r", str(REQUIREMENTS), "-p", str(staging)],
                project, log,
                extra_env={"ANSIBLE_COLLECTIONS_PATH": str(staging)},
            )
            if code != 0:
                shutil.rmtree(staging, ignore_errors=True)
                raise ToolchainError(f"Collections install failed: ansible-galaxy exited {code}{last_line(out)}")
            staging.rename(target)
        return digest, target

    # ------------------------------------------------------------------ preview helper

    async def preview(
        self, runtime: Runtime, request: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """Run the preview helper in ``runtime`` and return its JSON reply (``ok`` true or false)."""
        timeout = self._settings.preview_timeout_seconds if timeout is None else timeout
        proc = await asyncio.create_subprocess_exec(
            self._settings.mise_bin, "exec", "-C", str(runtime.project), "--",
            runtime.python, "-I", str(HELPER),
            cwd=runtime.project, env=self.ansible_env(runtime),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps(request).encode()),
                timeout=max(timeout, 0.1),
            )
        except TimeoutError:
            kill_group(proc.pid)
            await proc.wait()
            raise PreviewTimeout(f"Preview helper did not answer within {timeout:.3g}s") from None
        except asyncio.CancelledError:
            kill_group(proc.pid)
            raise
        try:
            reply = json.loads(out.decode(errors="replace"))
        except json.JSONDecodeError:
            reply = None
        if not isinstance(reply, dict) or "ok" not in reply:
            # stderr is not passed on: it is Ansible's own output and is never shown to clients.
            raise ToolchainError(f"Preview helper failed (exit code {proc.returncode})")
        return reply

    # ------------------------------------------------------------------ subprocess helpers

    def tool_env(self, project: Path) -> dict[str, str]:
        """mise sees only the checkout's own config: no global/system config, no parent dirs."""
        return {
            **os.environ,
            "MISE_TRUSTED_CONFIG_PATHS": str(project),
            "MISE_CEILING_PATHS": str(project.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(project.parent / "no-global-mise.toml"),
            "MISE_SYSTEM_CONFIG_FILE": str(project.parent / "no-system-mise.toml"),
            "MISE_YES": "1",
        }

    def ansible_env(self, runtime: Runtime) -> dict[str, str]:
        env = self.tool_env(runtime.project)
        if runtime.collections_path is not None:
            env["ANSIBLE_COLLECTIONS_PATH"] = str(runtime.collections_path)
        return env

    async def run_tool(
        self,
        args: list[str],
        project: Path,
        log: Log | None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self._settings.mise_bin, *args,
            cwd=project, env={**self.tool_env(project), **(extra_env or {})},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        chunks: list[str] = []
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                chunks.append(line)
                if log is not None:
                    log(line)
            await proc.wait()
        except asyncio.CancelledError:
            kill_group(proc.pid)
            raise
        return proc.returncode or 0, "\n".join(chunks)


def kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def last_line(output: str) -> str:
    """The most telling line of a tool's output, for a failure reason."""
    lines = [line for line in output.strip().splitlines() if line.strip() and not any(n in line for n in NOISE)]
    return f": {lines[-1].strip()}" if lines else ""

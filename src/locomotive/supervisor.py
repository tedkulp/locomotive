"""Run supervisor: prepares a checkout, installs the runtime, runs ansible-runner and follows its events.

No ansible-core code runs in this process; everything Ansible happens in subprocesses started through
the checkout's own mise-pinned toolchain.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from locomotive.checkout import Checkout, CheckoutError
from locomotive.clock import Clock
from locomotive.models import Run
from locomotive.runlog import RunLog
from locomotive.settings import Settings

REQUIREMENTS = Path("collections/requirements.yml")
CORE_VERSION = re.compile(r"core ([0-9][0-9A-Za-z.+-]*)")
POLL_SECONDS = 0.1


class RunFailed(Exception):
    """A step failed; the message is the Run's failure reason."""


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        sessions: sessionmaker,
        checkout: Checkout,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sessions = sessions
        self._checkout = checkout
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._collection_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ layout

    @property
    def runs_dir(self) -> Path:
        """Throwaway per-Run working dirs (the export lives here); deleted when a Run ends."""
        return self._settings.data_dir / "runs"

    @property
    def collections_dir(self) -> Path:
        return self._settings.data_dir / "collections"

    def history_dir(self, run_id: int) -> Path:
        """Kept per-Run data: the log and ansible-runner's artifacts."""
        return self._settings.data_dir / "history" / str(run_id)

    def log_path(self, run_id: int) -> Path:
        return self.history_dir(run_id) / "log.jsonl"

    # ------------------------------------------------------------------ lifecycle

    def sweep_orphans(self) -> None:
        """At startup nothing is running, so every Run dir left behind is an orphan."""
        if self.runs_dir.exists():
            for child in self.runs_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def start(self, run_id: int) -> None:
        task = asyncio.create_task(self._supervise(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ one Run

    def _update(self, run_id: int, **fields: object) -> Run:
        with self._sessions() as session:
            run = session.get_one(Run, run_id)
            for key, value in fields.items():
                setattr(run, key, value)
            session.commit()
            return run

    async def _supervise(self, run_id: int) -> None:
        with self._sessions() as session:
            run = session.get_one(Run, run_id)
        log = RunLog(self.log_path(run_id))
        work = self.runs_dir / str(run_id)
        status, reason, rc = "failed", None, None
        try:
            rc = await self._execute(run, work, log)
            if rc == 0:
                status = "succeeded"
            else:
                reason = f"ansible-playbook failed (exit code {rc})"
        except RunFailed as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            reason = "locomotive shut down during the Run"
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must still end the Run visibly
            reason = f"internal error: {exc!r}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if reason:
                log.append(f"Run failed: {reason}", kind="locomotive")
            else:
                log.append("Run succeeded", kind="locomotive")
            self._update(
                run_id, status=status, failure_reason=reason, rc=rc, finished_at=self._clock.now()
            )

    async def _execute(self, run: Run, work: Path, log: RunLog) -> int:
        project = work / "project"

        log.append(f"Fetching {run.branch} from the remote", kind="locomotive")
        try:
            sha = await self._checkout.fetch(run.branch)
        except CheckoutError as exc:
            raise RunFailed(str(exc)) from exc
        self._update(run.id, sha=sha)
        log.append(f"{run.branch} is at {sha}", kind="locomotive")

        try:
            await self._checkout.export(sha, project)
        except CheckoutError as exc:
            raise RunFailed(str(exc)) from exc
        playbook = Path("playbooks") / run.playbook
        if not (project / playbook).is_file():
            raise RunFailed(f"Playbook {playbook} does not exist at {sha[:12]}")

        log.append("$ mise install", kind="locomotive")
        code, out = await self._run_tool(["install"], project, log)
        if code != 0:
            raise RunFailed(f"Runtime install failed: mise install exited {code}{_last_line(out)}")

        code, out = await self._run_tool(["exec", "-C", str(project), "--", "ansible", "--version"], project, None)
        match = CORE_VERSION.search(out)
        if code != 0 or not match:
            raise RunFailed(f"Could not determine the ansible-core version{_last_line(out)}")
        self._update(run.id, ansible_core_version=match.group(1))
        log.append(f"ansible-core {match.group(1)}", kind="locomotive")

        collections_path = await self._install_collections(run, project, log)

        cmdline = ["--check"]
        log.append(f"$ ansible-playbook {playbook} {shlex.join(cmdline)}", kind="locomotive")
        return await self._run_ansible_runner(run, work, project, playbook, cmdline, collections_path, log)

    async def _install_collections(self, run: Run, project: Path, log: RunLog) -> Path | None:
        requirements = project / REQUIREMENTS
        if not requirements.is_file():
            log.append(f"No {REQUIREMENTS}; skipping collections", kind="locomotive")
            return None
        digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
        self._update(run.id, requirements_hash=digest)
        target = self.collections_dir / digest
        lock = self._collection_locks.setdefault(digest, asyncio.Lock())
        async with lock:
            if target.is_dir():
                log.append(f"Collections for requirements {digest[:12]} already installed", kind="locomotive")
                return target
            staging = self.collections_dir / f".staging-{digest}-{run.id}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.parent.mkdir(parents=True, exist_ok=True)
            log.append(f"$ ansible-galaxy collection install -r {REQUIREMENTS}", kind="locomotive")
            code, out = await self._run_tool(
                ["exec", "-C", str(project), "--", "ansible-galaxy", "collection", "install",
                 "-r", str(REQUIREMENTS), "-p", str(staging)],
                project, log,
                extra_env={"ANSIBLE_COLLECTIONS_PATH": str(staging)},
            )
            if code != 0:
                shutil.rmtree(staging, ignore_errors=True)
                raise RunFailed(f"Collections install failed: ansible-galaxy exited {code}{_last_line(out)}")
            staging.rename(target)
        return target

    async def _run_ansible_runner(
        self,
        run: Run,
        work: Path,
        project: Path,
        playbook: Path,
        cmdline: list[str],
        collections_path: Path | None,
        log: RunLog,
    ) -> int:
        artifacts = self.history_dir(run.id) / "artifacts"
        ident = "run"
        env = self._tool_env(project)
        if collections_path is not None:
            env["ANSIBLE_COLLECTIONS_PATH"] = str(collections_path)
        runner_out = self.history_dir(run.id) / "runner.out"
        runner_out.parent.mkdir(parents=True, exist_ok=True)
        with runner_out.open("wb") as out:
            proc = await asyncio.create_subprocess_exec(
                self._settings.mise_bin, "exec", "-C", str(project), "--",
                "ansible-runner", "run", str(work),
                "--project-dir", str(project),
                "--artifact-dir", str(artifacts),
                "--ident", ident,
                "-q",
                "-p", str(playbook),
                # "=" form: argparse would otherwise read "--check" as an option of its own.
                f"--cmdline={shlex.join(cmdline)}",
                cwd=project, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            follower = EventFollower(artifacts / ident / "job_events", log)
            try:
                while proc.returncode is None:
                    follower.poll()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), POLL_SECONDS)
            except asyncio.CancelledError:
                _kill_group(proc.pid)
                raise
            follower.poll(final=True)
        rc = proc.returncode
        if follower.count == 0 and rc != 0:
            raise RunFailed(f"ansible-runner failed to start (exit code {rc}){_last_line(runner_out.read_text(errors='replace'))}")
        return rc

    # ------------------------------------------------------------------ subprocess helpers

    def _tool_env(self, project: Path) -> dict[str, str]:
        """mise sees only the checkout's own config: no global/system config, no parent dirs."""
        return {
            **os.environ,
            "MISE_TRUSTED_CONFIG_PATHS": str(project),
            "MISE_CEILING_PATHS": str(project.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(project.parent / "no-global-mise.toml"),
            "MISE_SYSTEM_CONFIG_FILE": str(project.parent / "no-system-mise.toml"),
            "MISE_YES": "1",
        }

    async def _run_tool(
        self,
        args: list[str],
        project: Path,
        log: RunLog | None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self._settings.mise_bin, *args,
            cwd=project, env={**self._tool_env(project), **(extra_env or {})},
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
                    log.append(line, kind="locomotive")
            await proc.wait()
        except asyncio.CancelledError:
            _kill_group(proc.pid)
            raise
        return proc.returncode or 0, "\n".join(chunks)


class EventFollower:
    """Follows the job_events files ansible-runner writes, in counter order, into the Run log."""

    def __init__(self, directory: Path, log: RunLog) -> None:
        self._dir = directory
        self._log = log
        self._seen: set[int] = set()
        self._next = 1

    @property
    def count(self) -> int:
        return len(self._seen)

    def poll(self, final: bool = False) -> None:
        if not self._dir.is_dir():
            return
        pending: dict[int, Path] = {}
        for path in self._dir.glob("*.json"):
            counter = path.name.split("-", 1)[0]
            if counter.isdigit() and int(counter) not in self._seen:
                pending[int(counter)] = path
        for counter in sorted(pending):
            if counter != self._next and not final:
                break  # wait for the gap to fill so the log keeps Ansible's order
            try:
                event = json.loads(pending[counter].read_text())
            except (OSError, json.JSONDecodeError):
                if final:
                    continue
                break
            self._seen.add(counter)
            self._next = counter + 1
            if event.get("stdout"):
                self._log.append(event["stdout"], kind="ansible")


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


NOISE = ("Run with --verbose", "mise ERROR Version:")


def _last_line(output: str) -> str:
    """The most telling line of a tool's output, for a failure reason."""
    lines = [line for line in output.strip().splitlines() if line.strip() and not any(n in line for n in NOISE)]
    return f": {lines[-1].strip()}" if lines else ""

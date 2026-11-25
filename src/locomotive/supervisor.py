"""Run supervisor: prepares a checkout, installs the runtime, runs ansible-runner and follows its events.

No ansible-core code runs in this process; everything Ansible happens in subprocesses started through
the checkout's own mise-pinned toolchain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import shutil
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from locomotive.checkout import Checkout, CheckoutError
from locomotive.clock import Clock
from locomotive.launch import LaunchOptions
from locomotive.models import Run
from locomotive.runlog import RunLog
from locomotive.runtime import Runtime, Toolchain, ToolchainError, kill_group, last_line
from locomotive.settings import Settings

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
        toolchain: Toolchain,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sessions = sessions
        self._checkout = checkout
        self._toolchain = toolchain
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ layout

    @property
    def runs_dir(self) -> Path:
        """Throwaway per-Run working dirs (the export lives here); deleted when a Run ends."""
        return self._settings.data_dir / "runs"

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

        try:
            runtime = await self._toolchain.prepare(
                project,
                log=lambda line: log.append(line, kind="locomotive"),
                on_step=lambda **fields: self._update(run.id, **fields),
            )
        except ToolchainError as exc:
            raise RunFailed(str(exc)) from exc

        options = launch_options(run)
        hosts = await self._resolve_hosts(run, runtime, options, log)
        self._update(run.id, hosts=hosts)

        log.append(f"$ {options.command()}", kind="locomotive")
        return await self._run_ansible_runner(run, work, runtime, playbook, options.cmdline(), log)

    async def _resolve_hosts(self, run: Run, runtime: Runtime, options: LaunchOptions, log: RunLog) -> list[str]:
        """The exact host set this Run will touch (``--list-hosts --limit`` semantics); fails loudly."""
        try:
            reply = await self._toolchain.preview(runtime, options.helper_request())
        except ToolchainError as exc:
            raise RunFailed(f"Host preview failed: {exc}") from exc
        if not reply.get("ok"):
            raise RunFailed(f"Host preview failed: {reply.get('error') or 'unknown error'}")
        unmatched = reply.get("unmatched") or []
        if unmatched:
            raise RunFailed(f"The limit has patterns that match no hosts: {', '.join(unmatched)}")
        hosts = list(reply.get("hosts") or [])
        if not hosts:
            raise RunFailed("No hosts to run on: the limit leaves none of the hosts this Playbook targets")
        log.append(f"Hosts ({len(hosts)}): {', '.join(hosts)}", kind="locomotive")
        return hosts

    async def _run_ansible_runner(
        self,
        run: Run,
        work: Path,
        runtime: Runtime,
        playbook: Path,
        cmdline: list[str],
        log: RunLog,
    ) -> int:
        project = runtime.project
        artifacts = self.history_dir(run.id) / "artifacts"
        ident = "run"
        env = self._toolchain.ansible_env(runtime)
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
                *([f"--cmdline={shlex.join(cmdline)}"] if cmdline else []),
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
                kill_group(proc.pid)
                raise
            follower.poll(final=True)
        rc = proc.returncode
        if follower.count == 0 and rc != 0:
            raise RunFailed(f"ansible-runner failed to start (exit code {rc}){last_line(runner_out.read_text(errors='replace'))}")
        return rc


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


def launch_options(run: Run) -> LaunchOptions:
    return LaunchOptions(
        playbook=run.playbook,
        limit=run.limit,
        tags=run.tags or [],
        check=run.check,
        diff=run.diff,
        verbosity=run.verbosity,
        extra_vars=run.extra_vars or {},
    )

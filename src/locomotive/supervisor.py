"""Run supervisor: prepares a checkout, installs the runtime, runs ansible-runner and follows its events.

No ansible-core code runs in this process; everything Ansible happens in subprocesses started through
the checkout's own mise-pinned toolchain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import signal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from locomotive.checkout import Checkout, CheckoutError
from locomotive.clock import Clock
from locomotive.launch import LaunchOptions
from locomotive.models import Run
from locomotive.rungrid import RunGrid
from locomotive.runlog import RunLog
from locomotive.runtime import Runtime, Toolchain, ToolchainError, kill_group, last_line, signal_group
from locomotive.settings import Settings
from locomotive.working_copy import WorkingCopy, WorkingCopyUnavailable

POLL_SECONDS = 0.1


STOP_PLUGIN_DIR = Path(__file__).parent / "ansible_plugins"
STOPPED = "Stopped after a task"
KILLED = "Killed"


class RunFailed(Exception):
    """A step failed; the message is the Run's failure reason."""


class RunCanceled(Exception):
    """Stop or Kill was requested before ansible-playbook started."""


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        sessions: sessionmaker,
        checkout: Checkout,
        toolchain: Toolchain,
        working_copy: WorkingCopy | None = None,
    ) -> None:
        self._settings = settings
        self._working_copy = working_copy or WorkingCopy(settings.working_copy_path, settings.hidden_playbooks)
        self._clock = clock
        self._sessions = sessions
        self._checkout = checkout
        self._toolchain = toolchain
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._logs: dict[int, RunLog] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancel: dict[int, str] = {}  # run id -> "stop" | "kill"

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

    def grid_path(self, run_id: int) -> Path:
        return self.history_dir(run_id) / "grid.json"

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

    def interrupt_leftovers(self) -> None:
        """At startup nothing is running, so a Run still marked running was cut off. Never retried."""
        with self._sessions() as session:
            ids = list(session.scalars(select(Run.id).where(Run.status == "running")))
        for run_id in ids:
            reason = "locomotive restarted during the Run"
            RunLog(self.log_path(run_id)).append(f"Run interrupted: {reason}", kind="locomotive")
            grid = RunGrid.load(self.grid_path(run_id))
            grid.finish()
            grid.save(self.grid_path(run_id))
            self._update(
                run_id, status="interrupted", failure_reason=reason, finished_at=self._clock.now(),
                recap=grid.recap, recap_source=grid.recap_source,
            )

    def start(self, run_id: int) -> None:
        self._logs[run_id] = RunLog(self.log_path(run_id))
        task = asyncio.create_task(self._supervise(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    def stop_file(self, run_id: int) -> Path:
        """Read by the ``locomotive_stop`` callback plugin as each task starts."""
        return self.runs_dir / str(run_id) / "stop"

    def request_stop(self, run_id: int) -> bool:
        """Let the current task finish, then end the Run. False if the Run isn't running here."""
        if run_id not in self._tasks:
            return False
        if self._cancel.get(run_id):
            return True
        self._cancel[run_id] = "stop"
        self._update(run_id, cancel_request="stop")
        path = self.stop_file(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self._logs[run_id].append("Stop requested: the Run ends when the current task finishes", kind="locomotive")
        return True

    def request_kill(self, run_id: int) -> bool:
        """SIGINT the Run's process group now, SIGKILL it if it outlives the grace period."""
        if run_id not in self._tasks:
            return False
        if self._cancel.get(run_id) == "kill":
            return True
        self._cancel[run_id] = "kill"
        self._update(run_id, cancel_request="kill")
        self._logs[run_id].append("Kill requested: sending SIGINT", kind="locomotive")
        proc = self._procs.get(run_id)
        if proc is not None:
            self._kill(run_id, proc)
        return True

    def _kill(self, run_id: int, proc: asyncio.subprocess.Process) -> None:
        signal_group(proc.pid, signal.SIGINT)
        asyncio.create_task(self._escalate(run_id, proc), name=f"kill-{run_id}")

    async def _escalate(self, run_id: int, proc: asyncio.subprocess.Process) -> None:
        deadline = asyncio.get_running_loop().time() + self._settings.kill_grace_seconds
        while proc.returncode is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS / 2)
        if proc.returncode is None:
            log = self._logs.get(run_id)
            if log is not None:
                log.append("Still running after SIGINT: sending SIGKILL", kind="locomotive")
            signal_group(proc.pid, signal.SIGKILL)

    def _register(self, run_id: int, proc: asyncio.subprocess.Process) -> None:
        self._procs[run_id] = proc
        if self._cancel.get(run_id) == "kill":
            self._kill(run_id, proc)

    def _checkpoint(self, run_id: int) -> None:
        if self._cancel.get(run_id):
            raise RunCanceled

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
        log = self._logs[run_id]
        grid = RunGrid()
        work = self.runs_dir / str(run_id)
        status, reason, rc = "failed", None, None
        try:
            rc = await self._execute(run, work, log, grid)
            if rc == 0:
                status = "succeeded"
            else:
                reason = f"ansible-playbook failed (exit code {rc})"
        except RunCanceled:
            pass
        except RunFailed as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            status, reason = "interrupted", "locomotive shut down during the Run"
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must still end the Run visibly
            reason = f"internal error: {exc!r}"
        finally:
            cancel = self._cancel.pop(run_id, None)
            self._procs.pop(run_id, None)
            self._logs.pop(run_id, None)
            # Asked to stop and Ansible never reached its final stats: the Run was cut short on request.
            if cancel and status != "interrupted" and grid.stats is None:
                status, reason = "canceled", KILLED if cancel == "kill" else STOPPED
            shutil.rmtree(work, ignore_errors=True)
            if status == "succeeded":
                log.append("Run succeeded", kind="locomotive")
            else:
                verb = {"canceled": "canceled", "interrupted": "interrupted"}.get(status, "failed")
                log.append(f"Run {verb}: {reason}", kind="locomotive")
            grid.finish()
            grid.save(self.grid_path(run_id))
            self._update(
                run_id, status=status, failure_reason=reason, rc=rc, finished_at=self._clock.now(),
                recap=grid.recap, recap_source=grid.recap_source,
            )

    async def _execute(self, run: Run, work: Path, log: RunLog, grid: RunGrid) -> int:
        self._checkpoint(run.id)
        if run.checkout == "working_copy":
            project = await self._resolve_working_copy(run, log)
            work.mkdir(parents=True, exist_ok=True)  # ansible-runner's private data dir; the project is elsewhere
        else:
            project = await self._resolve_clean(run, work / "project", log)
        playbook = Path("playbooks") / run.playbook
        if not (project / playbook).is_file():
            where = "in the Working Copy" if run.checkout == "working_copy" else f"at {(run.sha or '')[:12]}"
            raise RunFailed(f"Playbook {playbook} does not exist {where}")

        self._checkpoint(run.id)
        try:
            runtime = await self._toolchain.prepare(
                project,
                log=lambda line: log.append(line, kind="locomotive"),
                on_step=lambda **fields: self._update(run.id, **fields),
                on_proc=lambda proc: self._register(run.id, proc),
                checkpoint=lambda: self._checkpoint(run.id),
            )
        except ToolchainError as exc:
            if self._cancel.get(run.id):
                raise RunCanceled from exc
            raise RunFailed(str(exc)) from exc
        self._checkpoint(run.id)

        options = launch_options(run)
        hosts = await self._resolve_hosts(run, runtime, options, log)
        self._update(run.id, hosts=hosts)
        self._checkpoint(run.id)

        log.append(f"$ {options.command()}", kind="locomotive")
        return await self._run_ansible_runner(run, work, runtime, playbook, options.cmdline(), log, grid)

    async def _resolve_clean(self, run: Run, project: Path, log: RunLog) -> Path:
        """Fetch the branch, record its SHA and export that tree into this Run's throwaway dir."""
        log.append(f"Fetching {run.branch} from the remote", kind="locomotive")
        try:
            sha = await self._checkout.fetch(run.branch)
        except CheckoutError as exc:
            raise RunFailed(str(exc)) from exc
        run.sha = sha
        self._update(run.id, sha=sha)
        log.append(f"{run.branch} is at {sha}", kind="locomotive")
        try:
            await self._checkout.export(sha, project)
        except CheckoutError as exc:
            raise RunFailed(str(exc)) from exc
        return project

    async def _resolve_working_copy(self, run: Run, log: RunLog) -> Path:
        """Record HEAD, the dirty flag, the diff against HEAD and untracked files; run in place.

        The Working Copy is only read here, never written: no commit, pull, push or fetch.
        """
        try:
            snap = await self._working_copy.snapshot()
        except WorkingCopyUnavailable as exc:
            raise RunFailed(str(exc)) from exc
        status = snap.status
        run.sha = status.head_sha
        self._update(
            run.id, sha=status.head_sha, branch=status.branch or "HEAD", dirty=status.dirty,
            wc_diff=snap.diff, diff_truncated=snap.diff_truncated, untracked=status.untracked,
        )
        state = (
            f"dirty: {len(status.changed)} changed, {len(status.untracked)} untracked" if status.dirty else "clean"
        )
        log.append(
            f"Running in place from the Working Copy {status.path} on {status.branch or 'a detached HEAD'} "
            f"at {status.head_sha} ({state})",
            kind="locomotive",
        )
        if snap.diff_truncated:
            log.append("The recorded diff against HEAD is capped at 1 MB and was truncated", kind="locomotive")
        return status.path

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
        grid: RunGrid,
    ) -> int:
        project = runtime.project
        artifacts = self.history_dir(run.id) / "artifacts"
        ident = "run"
        env = self._toolchain.ansible_env(runtime)
        # Stop after this task: our callback plugin ends Ansible as the next task starts, once the stop
        # file exists, so no host is left mid-task.
        env["ANSIBLE_CALLBACK_PLUGINS"] = os.pathsep.join(
            filter(None, (str(STOP_PLUGIN_DIR), env.get("ANSIBLE_CALLBACK_PLUGINS")))
        )
        env["LOCOMOTIVE_STOP_FILE"] = str(self.stop_file(run.id))
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
            self._register(run.id, proc)
            follower = EventFollower(artifacts / ident / "job_events", log, grid, self.grid_path(run.id))
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
    """Follows the job_events files ansible-runner writes, in counter order, into the Run log and grid."""

    def __init__(self, directory: Path, log: RunLog, grid: RunGrid, grid_path: Path) -> None:
        self._dir = directory
        self._log = log
        self._grid = grid
        self._grid_path = grid_path
        self._saved_version = -1
        self._seen: set[int] = set()
        self._next = 1

    @property
    def count(self) -> int:
        return len(self._seen)

    def poll(self, final: bool = False) -> None:
        self._read(final)
        if self._grid.version != self._saved_version:
            self._grid.save(self._grid_path)
            self._saved_version = self._grid.version

    def _read(self, final: bool) -> None:
        if not self._dir.is_dir():
            return
        pending: dict[int, Path] = {}
        for name in os.listdir(self._dir):
            counter = name.split("-", 1)[0]
            if name.endswith(".json") and counter.isdigit() and int(counter) not in self._seen:
                pending[int(counter)] = self._dir / name
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
            self._grid.apply(event)
            if event.get("stdout"):
                host = (event.get("event_data") or {}).get("host")
                self._log.append(event["stdout"], kind="ansible", host=host)


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

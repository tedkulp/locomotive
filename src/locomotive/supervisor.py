"""Run supervisor: prepares a checkout, installs the runtime, runs ansible-runner and follows its events.

No ansible-core code runs in this process; everything Ansible happens in subprocesses started through
the checkout's own mise-pinned toolchain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from locomotive import history
from locomotive.checkout import Checkout, CheckoutError
from locomotive.clock import Clock
from locomotive.launch import LaunchOptions
from locomotive.models import ACTIVE_STATUSES, CheckoutKind, Run, RunStatus
from locomotive.notifier import Notifier
from locomotive.rungrid import RunGrid
from locomotive.runlog import RunLog
from locomotive.runtime import (
    REQUIREMENTS,
    Runtime,
    Toolchain,
    ToolchainError,
    kill_group,
    last_line,
    signal_group,
)
from locomotive.settings import Settings
from locomotive.working_copy import WorkingCopy, WorkingCopyUnavailable

logger = logging.getLogger(__name__)

POLL_SECONDS = 0.1


STOP_PLUGIN_DIR = Path(__file__).parent / "ansible_plugins"
STOPPED = "Stopped after a task"
KILLED = "Killed"
CANCELED = "Canceled while queued"
CANCEL_REASONS = {"stop": STOPPED, "kill": KILLED, "cancel": CANCELED}


class RunFailed(Exception):
    """A step failed; the message is the Run's failure reason."""


class RunCanceled(Exception):
    """Stop, Kill or a queue cancel was requested before ansible-playbook started."""


def resolves_hosts_at_start(run: Run) -> bool:
    """A Working Copy can change while its Run waits, so its host set is resolved again as it starts.

    A clean Run's host set (like its SHA) is fixed when it is queued, so what was previewed is what runs.
    """
    return run.checkout == CheckoutKind.WORKING_COPY


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        sessions: sessionmaker,
        checkout: Checkout,
        toolchain: Toolchain,
        working_copy: WorkingCopy | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._working_copy = working_copy or WorkingCopy(settings.working_copy_path, settings.hidden_playbooks)
        self._clock = clock
        self._sessions = sessions
        self._checkout = checkout
        self._toolchain = toolchain
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._logs: dict[int, RunLog] = {}
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancel: dict[int, str] = {}  # run id -> "stop" | "kill" | "cancel"
        self._holding: set[int] = set()  # Runs that hold their hosts (status running)
        self._queue_changed = asyncio.Event()

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
            ids = list(session.scalars(select(Run.id).where(Run.status == RunStatus.RUNNING)))
        for run_id in ids:
            reason = "locomotive restarted during the Run"
            RunLog(self.log_path(run_id)).append(f"Run interrupted: {reason}", kind="locomotive")
            grid = RunGrid.load(self.grid_path(run_id))
            grid.finish()
            grid.save(self.grid_path(run_id))
            self._update(
                run_id, status=RunStatus.INTERRUPTED, failure_reason=reason, finished_at=self._clock.now(),
                recap=grid.recap, recap_source=grid.recap_source, first_failed_task=grid.first_failed_task,
            )
            self._ended(run_id)

    def _ended(self, run_id: int) -> None:
        """The Run reached its final status: tell the notifier (which decides whether it is notable)."""
        if self._notifier is not None:
            self._notifier.run_finished(run_id)

    def resume_queued(self) -> None:
        """Queued Runs haven't touched a host, so they survive a restart and carry on waiting.

        A clean Run keeps the SHA and host set it was queued with; its export is redone from that SHA.
        """
        with self._sessions() as session:
            ids = list(session.scalars(select(Run.id).where(Run.status == RunStatus.QUEUED).order_by(Run.id)))
        for run_id in ids:
            RunLog(self.log_path(run_id)).append("locomotive restarted; the Run is still queued", kind="locomotive")
            self.start(run_id)

    def start(self, run_id: int) -> None:
        self._logs[run_id] = RunLog(
            self.log_path(run_id), cap=self._settings.log_cap_bytes,
            on_truncate=lambda: self._update(run_id, log_truncated=True),
        )
        task = asyncio.create_task(self._supervise(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))

    def stop_file(self, run_id: int) -> Path:
        """Read by the ``locomotive_stop`` callback plugin as each task starts."""
        return self.runs_dir / str(run_id) / "stop"

    def stop_done_file(self, run_id: int) -> Path:
        """Written by the plugin once the stop takes effect; lists the tasks it skipped to run handlers."""
        return self.runs_dir / str(run_id) / "stop.done"

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

    def request_cancel(self, run_id: int) -> bool:
        """Take a queued Run out of the queue. False if it isn't queued here (it may already hold its hosts)."""
        if run_id not in self._tasks or run_id in self._holding:
            return False
        if self._cancel.get(run_id) in ("cancel", "kill"):
            return True
        self._cancel[run_id] = "cancel"
        self._update(run_id, cancel_request="cancel")
        self._logs[run_id].append("Cancel requested: the Run leaves the queue", kind="locomotive")
        proc = self._procs.get(run_id)
        if proc is not None and proc.returncode is None:
            self._kill(run_id, proc)
        self._wake_queue()
        return True

    def _wake_queue(self) -> None:
        """Hosts were freed or a queued Run left: every waiting Run looks again."""
        self._queue_changed.set()
        self._queue_changed = asyncio.Event()

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
        if self._cancel.get(run_id) in ("kill", "cancel"):
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
        if run.waiting_for is not None:  # resumed after a restart: it prepares again before it waits
            self._update(run_id, waiting_for=None)
        log = self._logs[run_id]
        grid = RunGrid()
        work = self.runs_dir / str(run_id)
        status, reason, rc = RunStatus.FAILED, None, None
        queued_at_shutdown = False
        try:
            rc = await self._execute(run, work, log, grid)
            if rc == 0:
                status = RunStatus.SUCCEEDED
            else:
                reason = f"ansible-playbook failed (exit code {rc})"
        except RunCanceled:
            pass
        except RunFailed as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            if run_id in self._holding:
                status, reason = RunStatus.INTERRUPTED, "locomotive shut down during the Run"
            else:
                queued_at_shutdown = True
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must still end the Run visibly
            reason = f"internal error: {exc!r}"
        finally:
            cancel = self._cancel.pop(run_id, None)
            stopped = self.stop_done_file(run_id).exists()
            self._procs.pop(run_id, None)
            self._logs.pop(run_id, None)
            self._holding.discard(run_id)
            shutil.rmtree(work, ignore_errors=True)
            if queued_at_shutdown:
                # It never touched a host: it stays queued and carries on after the restart.
                log.append("locomotive shut down; the Run stays queued", kind="locomotive")
            else:
                fields = self._finish(run_id, log, grid, status, reason, rc, cancel, stopped)
                if fields["status"] != RunStatus.INTERRUPTED:  # shutting down: housekeeping compresses it at startup
                    fields["log_bytes"] = await self._compress(run_id)
                self._update(run_id, **fields)
                self._ended(run_id)
            self._wake_queue()

    def _finish(
        self, run_id: int, log: RunLog, grid: RunGrid, status: str, reason: str | None, rc: int | None,
        cancel: str | None, stopped: bool = False,
    ) -> dict[str, object]:
        """The Run's last log line and final grid; returns the fields that end it."""
        # Asked to stop and Ansible never reached its final stats, or the stop plugin skipped work to end it
        # (it leaves ``stop.done``): the Run was cut short on request.
        if cancel and status != RunStatus.INTERRUPTED and (grid.stats is None or stopped):
            status, reason = RunStatus.CANCELED, CANCEL_REASONS[cancel]
        if status == RunStatus.SUCCEEDED:
            log.append("Run succeeded", kind="locomotive")
        else:
            verb = {RunStatus.CANCELED: "canceled", RunStatus.INTERRUPTED: "interrupted"}.get(status, "failed")
            log.append(f"Run {verb}: {reason}", kind="locomotive")
        grid.finish()
        grid.save(self.grid_path(run_id))
        return {
            "status": status, "failure_reason": reason, "rc": rc, "finished_at": self._clock.now(),
            "recap": grid.recap, "recap_source": grid.recap_source, "first_failed_task": grid.first_failed_task,
        }

    async def _compress(self, run_id: int) -> int | None:
        """Gzip the finished Run's files (off the event loop: a log can be 200 MB); its disk use after."""
        directory = self.history_dir(run_id)
        try:
            await asyncio.to_thread(history.compress, directory)
        except OSError:
            logger.exception("compressing Run %d's log failed; housekeeping will retry", run_id)
            return None
        return history.disk_usage(directory)

    async def _execute(self, run: Run, work: Path, log: RunLog, grid: RunGrid) -> int:
        self._checkpoint(run.id)
        if run.checkout == CheckoutKind.WORKING_COPY:
            # Runs in place; its state (HEAD, dirty, diff, untracked) is recorded as it leaves the queue.
            project = self._working_copy_path()
            work.mkdir(parents=True, exist_ok=True)  # ansible-runner's private data dir; the project is elsewhere
        else:
            project = await self._resolve_clean(run, work / "project", log)
        playbook = Path("playbooks") / run.playbook
        if not (project / playbook).is_file():
            where = "in the Working Copy" if run.checkout == CheckoutKind.WORKING_COPY else f"at {(run.sha or '')[:12]}"
            raise RunFailed(f"Playbook {playbook} does not exist {where}")

        self._checkpoint(run.id)
        runtime = await self._prepare(run, project, log)

        options = launch_options(run)
        fresh = run.hosts is None
        if fresh:
            hosts = await self._resolve_hosts(run, runtime, options, log)
            self._update(run.id, hosts=hosts)
        else:
            hosts = list(run.hosts or [])
            log.append(f"Hosts ({len(hosts)}), fixed when the Run was queued: {', '.join(hosts)}", kind="locomotive")
        self._checkpoint(run.id)

        runtime = await self._acquire(run, hosts, fresh, runtime, options, log)

        log.append(f"$ {options.command()}", kind="locomotive")
        return await self._run_ansible_runner(run, work, runtime, playbook, options.cmdline(), log, grid)

    async def _prepare(self, run: Run, project: Path, log: RunLog) -> Runtime:
        """The checkout's toolchain: mise install, ansible-core, collections (see ``Toolchain.prepare``)."""
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
        return runtime

    async def _acquire(
        self, run: Run, hosts: list[str], fresh: bool, runtime: Runtime, options: LaunchOptions, log: RunLog
    ) -> Runtime:
        """Wait until no running Run holds one of ``hosts`` and no earlier queued Run shares one, then
        take them (status running). The check and the take happen with no await between them.

        A Working Copy Run's toolchain is prepared again first if its ``mise.toml`` or collections
        requirements changed since it was prepared. Returns the runtime the Run uses."""
        waiting_for: int | None = None
        recorded = False  # Working Copy state recorded since the Run last waited
        prepared = toolchain_files(runtime.project) if run.checkout == CheckoutKind.WORKING_COPY else None
        while True:
            changed = self._queue_changed
            self._checkpoint(run.id)
            blocker, held = self._blocker(run.id, hosts)
            if blocker is None:
                if prepared is not None and toolchain_files(runtime.project) != prepared:
                    log.append(
                        "The Working Copy's mise.toml or collections/requirements.yml changed while the Run was "
                        "queued: preparing its toolchain again",
                        kind="locomotive",
                    )
                    if waiting_for is not None:  # its stage is "preparing" again meanwhile
                        waiting_for = None
                        self._update(run.id, waiting_for=None)
                    runtime = await self._prepare(run, runtime.project, log)
                    prepared = toolchain_files(runtime.project)  # (a change during it is caught next time round)
                    fresh = recorded = False  # its hosts, resolved with the new runtime
                    continue
                if resolves_hosts_at_start(run) and not fresh:
                    hosts = await self._resolve_hosts(run, runtime, options, log)
                    self._update(run.id, hosts=hosts)
                    self._wake_queue()  # a smaller host set may unblock Runs queued behind this one
                    fresh = True
                    continue
                if run.checkout == CheckoutKind.WORKING_COPY and not recorded:
                    await self._record_working_copy(run, log)
                    recorded = True
                    continue  # it awaited: look at the queue again before taking the hosts
                self._holding.add(run.id)
                self._update(run.id, status=RunStatus.RUNNING, started_at=self._clock.now(), waiting_for=None)
                if waiting_for is not None:
                    log.append("Hosts are free; starting", kind="locomotive")
                return runtime
            fresh = recorded = False
            if blocker.id != waiting_for:
                waiting_for = blocker.id
                self._update(run.id, waiting_for={"id": blocker.id, "playbook": blocker.playbook})
                verb = "holds" if blocker.status == RunStatus.RUNNING else "is queued ahead for"
                log.append(
                    f"Waiting for Run #{blocker.id} ({blocker.playbook}): it {verb} {', '.join(held)}",
                    kind="locomotive",
                )
            await changed.wait()

    def _blocker(self, run_id: int, hosts: list[str]) -> tuple[Run | None, list[str]]:
        """The Run that keeps this one waiting: a running Run sharing a host, else an earlier queued Run
        (whose host set is known) sharing one. Earlier = launched first, so no Run waits forever."""
        wanted = set(hosts)
        with self._sessions() as session:
            others = session.scalars(
                select(Run)
                .where(Run.status.in_(ACTIVE_STATUSES), Run.id != run_id, Run.hosts.is_not(None))
                .order_by(Run.id)
            ).all()
        running = [r for r in others if r.status == RunStatus.RUNNING]
        ahead = [r for r in others if r.status == RunStatus.QUEUED and r.id < run_id]
        for other in (*running, *ahead):
            shared = [h for h in (other.hosts or []) if h in wanted]
            if shared:
                return other, shared
        return None, []

    async def _resolve_clean(self, run: Run, project: Path, log: RunLog) -> Path:
        """Fetch the branch and fix its SHA (unless fixed before a restart), then export that tree into
        this Run's throwaway dir."""
        if run.sha is None:
            log.append(f"Fetching {run.branch} from the remote", kind="locomotive")
            try:
                sha = await self._checkout.fetch(run.branch)
            except CheckoutError as exc:
                raise RunFailed(str(exc)) from exc
            run.sha = sha
            self._update(run.id, sha=sha)
            log.append(f"{run.branch} is at {sha}", kind="locomotive")
        else:
            sha = run.sha
            log.append(f"Using {sha}, fixed when the Run was queued", kind="locomotive")
        try:
            await self._checkout.export(sha, project)
        except CheckoutError as exc:
            raise RunFailed(str(exc)) from exc
        return project

    def _working_copy_path(self) -> Path:
        """Where a Working Copy Run runs in place."""
        try:
            path = self._working_copy.path
        except WorkingCopyUnavailable as exc:
            raise RunFailed(str(exc)) from exc
        if not path.is_dir():
            raise RunFailed(f"the Working Copy {path} does not exist")
        return path

    async def _record_working_copy(self, run: Run, log: RunLog) -> None:
        """Record HEAD, the dirty flag, the diff against HEAD and untracked files as the Run starts.

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
        # ansible-runner's own stdout file would repeat the whole output uncapped: locomotive keeps the
        # (capped) log instead, so runner doesn't write it.
        settings_file = work / "env" / "settings"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"suppress_output_file": True}))  # YAML, as runner reads it
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
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self._register(run.id, proc)
        assert proc.stdout is not None
        output = asyncio.create_task(_capped_copy(proc.stdout, runner_out, RUNNER_OUT_CAP))
        # Each event file is deleted once folded into the log and grid, so they don't pile up either.
        follower = EventFollower(
            artifacts / ident / "job_events", log, grid, self.grid_path(run.id), self.stop_done_file(run.id),
            consume=True,
        )
        try:
            while proc.returncode is None:
                follower.poll()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), POLL_SECONDS)
        except asyncio.CancelledError:
            kill_group(proc.pid)
            output.cancel()
            raise
        follower.poll(final=True)
        try:
            tail = await asyncio.wait_for(output, OUTPUT_DRAIN_SECONDS)
        except TimeoutError:  # a leftover grandchild holds the pipe: it was cancelled, keep going
            tail = b""
        rc = proc.returncode
        if follower.count == 0 and rc != 0:
            raise RunFailed(f"ansible-runner failed to start (exit code {rc}){last_line(tail.decode(errors='replace'))}")
        return rc


RUNNER_OUT_CAP = 64 * 1024  # ansible-runner's own output (-q: errors only), kept head and tail
OUTPUT_DRAIN_SECONDS = 5


async def _capped_copy(stream: asyncio.StreamReader, path: Path, cap: int) -> bytes:
    """Copy ``stream`` to ``path``: its first ``cap`` bytes, then (if there was more) a note of how much was
    dropped and its last ``cap`` bytes. Returns the last ``cap`` bytes (the most telling part)."""
    head = bytearray()
    last = bytearray()
    total = 0
    while chunk := await stream.read(64 * 1024):
        total += len(chunk)
        if len(head) < cap:
            head += chunk[: cap - len(head)]
        last += chunk
        del last[: max(0, len(last) - cap)]
    with path.open("wb") as out:
        out.write(head)
        rest = total - len(head)  # bytes after the head; ``last`` ends them
        if rest > 0:
            kept = last[-rest:] if rest < len(last) else last
            if rest > len(kept):
                out.write(f"\n[locomotive: {rest - len(kept)} bytes of output dropped]\n".encode())
            out.write(kept)
    return bytes(last)


class EventFollower:
    """Follows the job_events files ansible-runner writes, in counter order, into the Run log and grid."""

    def __init__(
        self, directory: Path, log: RunLog, grid: RunGrid, grid_path: Path, stop_done: Path | None = None,
        consume: bool = False,
    ) -> None:
        self._dir = directory
        self._consume = consume  # delete each event file once it is folded in
        self._stop_done = stop_done
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
            if event.get("event") == "runner_on_skipped" and self._stop_done is not None:
                with contextlib.suppress(OSError):  # written before the skip, so it's there by now
                    self._grid.stop_skipped.update(self._stop_done.read_text().split())
            self._grid.apply(event)
            if event.get("stdout"):
                host = (event.get("event_data") or {}).get("host")
                self._grid.link_log(event, self._log.append(event["stdout"], kind="ansible", host=host))
            if self._consume:
                pending[counter].unlink(missing_ok=True)


TOOLCHAIN_FILES = (Path("mise.toml"), REQUIREMENTS)


def toolchain_files(project: Path) -> dict[str, bytes | None]:
    """What a toolchain is prepared from: the checkout's mise.toml and collections requirements."""
    contents: dict[str, bytes | None] = {}
    for rel in TOOLCHAIN_FILES:
        try:
            contents[str(rel)] = (project / rel).read_bytes()
        except OSError:
            contents[str(rel)] = None
    return contents


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

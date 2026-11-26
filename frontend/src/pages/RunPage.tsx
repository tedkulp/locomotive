import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router";

import { CheckoutBadge, ModeBadge, SourceBadge, StatusBadge } from "@/components/RunBadges";
import { Button } from "@/components/ui/button";
import { useFollowBottom } from "@/hooks/useFollowBottom";
import { useNow, usePolling } from "@/hooks/usePolling";
import { useRunLog } from "@/hooks/useRunLog";
import { api, isFinished, type CellStatus, type Grid, type GridTask, type LogLine, type Recap, type Run, type WorkingCopyDiff } from "@/lib/api";
import { formatBytes, formatDate, runDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

export function CurrentRunPage() {
  const { data: runs } = usePolling(() => api.runs(20), 5000);
  if (!runs) return null;
  const current = runs.find((r) => r.status === "running") ?? runs[0];
  if (!current) return <div className="p-10 text-muted-foreground">No Runs yet.</div>;
  return <Navigate to={`/runs/${current.id}`} replace />;
}

export function RunPage() {
  const runId = Number(useParams().id);
  const { lines, grid, finalRun } = useRunLog(runId);
  const { data: polled, error } = usePolling(() => api.run(runId), 2000, [runId]);
  // The poll keeps Keep and the log state fresh after the stream has ended.
  const run = polled?.id === runId && isFinished(polled) ? polled : (finalRun ?? polled);
  const [hostFilter, setHostFilter] = useState<string | null>(null);
  const toggleHost = (host: string) => setHostFilter((current) => (current === host ? null : host));
  useEffect(() => setHostFilter(null), [runId]);
  // A click on a task name: scroll the log to that seq. `n` makes clicking the same task again jump again.
  const [jump, setJump] = useState<{ seq: number; n: number } | null>(null);
  useEffect(() => setJump(null), [runId]);
  const jumpTo = (seq: number) => setJump((current) => ({ seq, n: (current?.n ?? 0) + 1 }));
  const lastSeq = lines.length ? lines[lines.length - 1].seq : 0;

  if (error && !run) return <div className="p-10 text-destructive">{error}</div>;
  if (!run) return null;
  const live = !isFinished(run);

  return (
    <div className="flex flex-col h-full">
      <RunHeader run={run} />
      {run.status === "queued" && (
        <div className="px-6 py-2 border-b border-border bg-muted text-sm text-zinc-700">
          {run.stage === "waiting" && run.waiting_for ? (
            <>
              Waiting for{" "}
              <Link to={`/runs/${run.waiting_for.id}`} className="underline">
                Run #{run.waiting_for.id} ({run.waiting_for.playbook})
              </Link>
            </>
          ) : (
            "Preparing: fixing the commit, installing its toolchain and resolving its hosts…"
          )}
        </div>
      )}
      {run.checkout === "working_copy" &&
        (run.sha ? (
          <WorkingCopyChanges run={run} />
        ) : run.status === "queued" ? (
          <div className="px-6 py-2 border-b border-amber-200 bg-amber-50 text-sm text-amber-900">
            Working Copy state is recorded when the Run starts
          </div>
        ) : null)}
      {run.failure_reason && (
        <div
          className={cn(
            "px-6 py-2 border-b text-sm",
            run.status === "failed" ? "border-red-200 bg-red-50 text-red-700" : "border-border bg-muted text-zinc-700",
          )}
        >
          {run.failure_reason}
        </div>
      )}
      {run.notification?.status === "failed" && (
        <div className="px-6 py-2 border-b border-amber-200 bg-amber-50 text-sm text-amber-800">
          The ntfy notification for this Run could not be delivered after {run.notification.attempts} attempts
          {run.notification.error ? ` (${run.notification.error})` : ""}.
        </div>
      )}
      {run.log_truncated && (
        <div className="px-6 py-2 border-b border-amber-200 bg-amber-50 text-sm text-amber-800">
          Log truncated: it reached the per-Run cap, so later output was not kept. The grid and recap are complete.
        </div>
      )}
      {run.log_pruned && run.log_pruned_at && (
        <div className="px-6 py-2 border-b border-border bg-muted text-sm text-zinc-700">
          Log pruned on {formatDate(run.log_pruned_at)}. The summary and recap remain; the log, events and task grid
          were deleted by housekeeping.
        </div>
      )}
      {!run.log_pruned && grid && grid.tasks.length > 0 && (
        <TaskGrid
          grid={grid}
          live={live}
          logTruncated={run.log_truncated}
          lastSeq={lastSeq}
          hostFilter={hostFilter}
          onHost={toggleHost}
          onTask={jumpTo}
        />
      )}
      <div className="flex-1 grid grid-cols-[1fr_320px] min-h-0">
        {run.log_pruned ? (
          <div className="p-6 text-sm text-muted-foreground bg-card">No log: it was pruned.</div>
        ) : (
          <LogView
            lines={lines}
            live={live}
            hostFilter={hostFilter}
            onClear={() => setHostFilter(null)}
            jump={jump}
          />
        )}
        <RecapPanel
          hosts={run.log_pruned ? [] : (grid?.hosts ?? [])}
          recap={run.log_pruned ? (run.recap ?? {}) : (grid?.recap ?? run.recap ?? {})}
          appBuilt={!live && (grid?.recap_source ?? run.recap_source) === "locomotive"}
          hostFilter={hostFilter}
          onHost={toggleHost}
        />
      </div>
    </div>
  );
}

function RunHeader({ run }: { run: Run }) {
  const now = useNow();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [keep, setKeep] = useState(run.keep);
  useEffect(() => setKeep(run.keep), [run.keep]);
  const act = (action: () => Promise<Run>, then?: (r: Run) => void) => {
    setBusy(true);
    setActionError(null);
    action()
      .then((r) => then?.(r))
      .catch((err: Error) => setActionError(err.message))
      .finally(() => setBusy(false));
  };
  const live = !isFinished(run);

  return (
    <div className="px-6 py-4 border-b border-border bg-card flex flex-wrap items-center gap-3">
      <div className="text-lg font-semibold">
        Run #{run.id} <span className="font-mono text-base font-normal">{run.playbook}</span>
      </div>
      <StatusBadge run={run} />
      <ModeBadge run={run} />
      <CheckoutBadge run={run} />
      <SourceBadge run={run} />
      <span className="text-sm text-muted-foreground tabular-nums">{runDuration(run, now)}</span>
      {run.ansible_core_version && (
        <span className="text-xs text-muted-foreground">ansible-core {run.ansible_core_version}</span>
      )}
      {run.log_bytes != null && !run.log_pruned && (
        <span className="text-xs text-muted-foreground">log {formatBytes(run.log_bytes)}</span>
      )}
      <div className="ml-auto flex items-center gap-2">
        {actionError && <span className="text-xs text-destructive">{actionError}</span>}
        <label
          className="flex items-center gap-1.5 text-sm text-muted-foreground cursor-pointer select-none"
          title="Keep exempts this Run's log from pruning by age and by the log budget"
        >
          <input
            type="checkbox"
            checked={keep}
            onChange={(e) => {
              const next = e.currentTarget.checked;
              setKeep(next);
              setActionError(null);
              api.setKeep(run.id, next).then(
                (r) => setKeep(r.keep),
                (err: Error) => {
                  setKeep(!next);
                  setActionError(err.message);
                },
              );
            }}
          />
          Keep
        </label>
        {run.status === "queued" ? (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => act(() => api.cancelRun(run.id))}
            title="Take the Run out of the queue; it never touches a host"
          >
            Cancel
          </Button>
        ) : live ? (
          <>
            <Button
              variant="outline"
              disabled={busy || run.stop_requested}
              onClick={() => act(() => api.stopRun(run.id))}
              title="Let the current task finish on every host, then end the Run"
            >
              {run.stop_requested ? "Stopping after task…" : "Stop after this task"}
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => {
                if (window.confirm(`Kill Run #${run.id} now? A task in progress is cut off mid-way.`))
                  act(() => api.killRun(run.id));
              }}
              title="SIGINT, then SIGKILL, to the Run's processes"
            >
              Kill
            </Button>
          </>
        ) : (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => act(() => api.runAgain(run.id), (r) => navigate(`/runs/${r.id}`))}
            title="Start a new Run with the same settings"
          >
            Run again
          </Button>
        )}
      </div>
    </div>
  );
}

const CELL: Record<CellStatus, string> = {
  ok: "bg-emerald-500",
  changed: "bg-amber-400",
  skipped: "bg-zinc-300",
  failed: "bg-red-500",
  ignored: "bg-red-200",
  unreachable: "bg-fuchsia-500",
  running: "bg-sky-300 animate-pulse",
  interrupted: "bg-zinc-500",
};

/** What a Working Copy Run had uncommitted when it started: untracked files and the diff against HEAD. */
function WorkingCopyChanges({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<WorkingCopyDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open && !diff) api.workingCopyDiff(run.id).then(setDiff, (e: Error) => setError(e.message));
  }, [open, diff, run.id]);
  const untracked = run.untracked ?? [];
  return (
    <details
      className="px-6 py-2 border-b border-amber-200 bg-amber-50 text-sm"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer text-amber-900">
        Working Copy on <span className="font-mono">{run.branch}</span> at{" "}
        <span className="font-mono">{run.sha?.slice(0, 7)}</span>
        {run.dirty
          ? ` with uncommitted changes${untracked.length ? ` (${untracked.length} untracked file${untracked.length === 1 ? "" : "s"})` : ""}`
          : ", no uncommitted changes"}
        {run.diff_truncated && " · diff truncated at 1 MB"}
      </summary>
      {error && <div className="mt-2 text-xs text-destructive">{error}</div>}
      {untracked.length > 0 && (
        <div className="mt-2 font-mono text-xs text-amber-900">
          {untracked.map((f) => (
            <div key={f}>?? {f}</div>
          ))}
        </div>
      )}
      {diff?.diff && (
        <pre className="mt-2 max-h-96 overflow-auto rounded-md border border-amber-200 bg-white p-2 font-mono text-xs">
          {diff.diff.split("\n").map((line, i) => (
            <div
              key={i}
              className={cn(
                line.startsWith("+") && !line.startsWith("+++") && "text-emerald-700",
                line.startsWith("-") && !line.startsWith("---") && "text-red-700",
              )}
            >
              {line || "\u00a0"}
            </div>
          ))}
        </pre>
      )}
    </details>
  );
}

/** Why a task name can't be clicked through to the log, or null if it can. */
function notInLog(task: GridTask, lastSeq: number, live: boolean, logTruncated: boolean): string | null {
  if (task.log_seq != null) return task.log_seq <= lastSeq ? null : "Not in the log yet: still loading";
  if (logTruncated) return "Not in the log: log truncated";
  if (live) return "Not in the log yet";
  return "Not in the log: this Run's grid predates links to the log";
}

function TaskGrid({
  grid,
  live,
  logTruncated,
  lastSeq,
  hostFilter,
  onHost,
  onTask,
}: {
  grid: Grid;
  live: boolean;
  logTruncated: boolean;
  lastSeq: number;
  hostFilter: string | null;
  onHost: (host: string) => void;
  onTask: (seq: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { onScroll } = useFollowBottom(ref, grid.tasks.length, live);

  return (
    <div
      ref={ref}
      onScroll={onScroll}
      className="overflow-auto max-h-[45%] border-b border-border bg-card"
    >
      <table className="text-xs border-separate border-spacing-0">
        <thead className="sticky top-0 bg-card z-10">
          <tr>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground min-w-[280px] align-bottom">Task</th>
            {grid.hosts.map((host) => (
              <th
                key={host}
                onClick={() => onHost(host)}
                title="Filter the log to this host"
                className={cn(
                  "px-1 py-2 font-medium cursor-pointer [writing-mode:vertical-rl] rotate-180 h-24 text-left",
                  hostFilter === host ? "text-foreground bg-muted" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {host}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.tasks.map((task, i) => {
            const newPlay = task.play && task.play !== grid.tasks[i - 1]?.play;
            const running = Object.values(task.results).includes("running");
            const why = notInLog(task, lastSeq, live, logTruncated);
            const from = task.included_from ? `from ${task.included_from}` : null;
            return (
              <Fragment key={task.id}>
                {newPlay && (
                  <tr>
                    <td
                      colSpan={grid.hosts.length + 1}
                      className="px-3 pt-2 pb-0.5 font-semibold text-sky-700 font-mono"
                    >
                      PLAY [{task.play}]
                    </td>
                  </tr>
                )}
                <tr className={cn(running && "bg-sky-50")}>
                  <td
                    title={[from, why ?? "Show this task in the log"].filter(Boolean).join(" · ")}
                    className={cn(
                      "px-3 py-0.5 font-mono truncate max-w-[360px]",
                      task.included_from && "pl-7 text-fuchsia-800",
                    )}
                  >
                    {why === null && task.log_seq != null ? (
                      <button
                        type="button"
                        onClick={() => onTask(task.log_seq!)}
                        className="max-w-full truncate text-left hover:underline cursor-pointer"
                      >
                        {task.name}
                      </button>
                    ) : (
                      <span className="cursor-default">{task.name}</span>
                    )}
                  </td>
                  {grid.hosts.map((host) => {
                    const status = task.results[host];
                    return (
                      <td key={host} className={cn("px-1", hostFilter === host && "bg-muted")}>
                        <div
                          title={status ? `${host}: ${status}` : undefined}
                          className={cn("h-3.5 w-3.5 rounded-sm mx-auto", status && CELL[status])}
                        />
                      </td>
                    );
                  })}
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RecapPanel({
  hosts,
  recap,
  appBuilt,
  hostFilter,
  onHost,
}: {
  hosts: string[];
  recap: Recap;
  appBuilt: boolean;
  hostFilter: string | null;
  onHost: (host: string) => void;
}) {
  const names = useMemo(() => [...new Set([...hosts, ...Object.keys(recap)])], [hosts, recap]);
  return (
    <div className="overflow-auto p-3 bg-card border-l border-border">
      <div className="text-xs font-medium text-muted-foreground mb-2">
        Recap {appBuilt && <span className="font-normal">(built by locomotive: Ansible emitted no final stats)</span>}
      </div>
      {names.length === 0 && <div className="text-xs text-muted-foreground">No host results yet.</div>}
      {names.length > 0 && (
        <table className="w-full text-xs font-mono">
          <thead>
            <tr className="text-[10px] text-muted-foreground">
              <th className="text-left font-normal">host</th>
              <th className="font-normal text-right">ok</th>
              <th className="font-normal text-right">chg</th>
              <th className="font-normal text-right">skip</th>
              <th className="font-normal text-right">fail</th>
              <th className="font-normal text-right">unr</th>
            </tr>
          </thead>
          <tbody>
            {names.map((host) => {
              const r = recap[host] ?? { ok: 0, changed: 0, skipped: 0, failed: 0, unreachable: 0 };
              return (
                <tr
                  key={host}
                  onClick={() => onHost(host)}
                  className={cn(
                    "cursor-pointer hover:bg-muted",
                    hostFilter === host && "bg-muted",
                    (r.failed > 0 || r.unreachable > 0) && "text-red-600",
                  )}
                >
                  <td className="py-1 pl-1 truncate max-w-[120px]">{host}</td>
                  <td className="text-right text-emerald-600">{r.ok}</td>
                  <td className="text-right text-amber-600">{r.changed}</td>
                  <td className="text-right text-zinc-400">{r.skipped}</td>
                  <td className="text-right text-red-600">{r.failed}</td>
                  <td className="text-right pr-1 text-fuchsia-600">{r.unreachable}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <div className="text-[10px] text-muted-foreground mt-2">Click a host to filter the log.</div>
    </div>
  );
}

const HIGHLIGHT_MS = 2000;

function LogView({
  lines,
  live,
  hostFilter,
  onClear,
  jump,
}: {
  lines: LogLine[];
  live: boolean;
  hostFilter: string | null;
  onClear: () => void;
  jump: { seq: number; n: number } | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const shown = hostFilter ? lines.filter((line) => !line.host || line.host === hostFilter) : lines;
  const { follow, onScroll, resume, jumpTo } = useFollowBottom(ref, `${hostFilter ?? ""}:${shown.length}`);
  const [highlight, setHighlight] = useState<number | null>(null);

  // Only a new click jumps (not new lines or a filter change): put the banner mid-log, follow off, and flash it.
  // The banner names no host, so it is shown under any host filter.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!jump || !el) return;
    const target = el.querySelector<HTMLElement>(`[data-seq="${jump.seq}"]`);
    if (!target) return;
    const offset = target.getBoundingClientRect().top - el.getBoundingClientRect().top + el.scrollTop;
    jumpTo(Math.max(0, offset - el.clientHeight / 2 + target.offsetHeight / 2));
    setHighlight(jump.seq);
    const timer = window.setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [jump, jumpTo]);
  return (
    <div className="relative min-h-0">
      {hostFilter && (
        <div className="absolute top-2 right-5 z-10 flex items-center gap-2 rounded-full bg-muted border border-border px-3 py-0.5 text-xs">
          only <span className="font-mono">{hostFilter}</span>
          <button type="button" onClick={onClear} className="text-muted-foreground hover:text-foreground">
            ✕
          </button>
        </div>
      )}
      <div
        ref={ref}
        onScroll={onScroll}
        className="h-full overflow-auto font-mono text-[12px] leading-5 p-3 bg-card"
      >
        {shown.map((line) => (
          <div
            key={line.seq}
            data-seq={line.seq}
            className={cn(
              "whitespace-pre-wrap transition-colors duration-500",
              lineTone(line),
              highlight === line.seq && "bg-yellow-200",
            )}
          >
            {line.text || " "}
          </div>
        ))}
        {live && <div className="text-zinc-300 animate-pulse">▍</div>}
      </div>
      {!follow && (
        <button
          type="button"
          onClick={resume}
          className="absolute bottom-3 right-5 text-xs rounded-full bg-primary text-primary-foreground px-3 py-1 shadow"
        >
          ↓ follow
        </button>
      )}
    </div>
  );
}

function lineTone(line: LogLine): string | false {
  if (line.kind === "locomotive") return "text-violet-700";
  if (line.text.startsWith("ok:")) return "text-emerald-700";
  if (line.text.startsWith("changed:")) return "text-amber-700";
  if (line.text.startsWith("skipping:")) return "text-zinc-400";
  if (line.text.startsWith("fatal:") || line.text.startsWith("failed:")) return "text-red-600";
  if (line.text.startsWith("TASK [") || line.text.startsWith("RUNNING HANDLER [")) return "font-semibold";
  if (line.text.startsWith("PLAY [")) return "text-sky-700 font-bold";
  if (line.text.startsWith("included:")) return "text-fuchsia-700 italic";
  return false;
}

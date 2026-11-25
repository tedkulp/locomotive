import { useEffect, useRef, useState } from "react";
import { Navigate, useParams } from "react-router";

import { CheckoutBadge, ModeBadge, SourceBadge, StatusBadge } from "@/components/RunBadges";
import { useNow, usePolling } from "@/hooks/usePolling";
import { useRunLog } from "@/hooks/useRunLog";
import { api, isFinished, type LogLine } from "@/lib/api";
import { runDuration } from "@/lib/format";
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
  const { lines, finalRun } = useRunLog(runId);
  const { data: polled, error } = usePolling(() => api.run(runId), 2000, [runId]);
  const run = finalRun ?? polled;
  const now = useNow();

  if (error && !run) return <div className="p-10 text-destructive">{error}</div>;
  if (!run) return null;

  return (
    <div className="flex flex-col h-full">
      <div className="px-6 py-4 border-b border-border bg-card flex flex-wrap items-center gap-3">
        <div className="text-lg font-semibold">
          Run #{run.id} <span className="font-mono text-base font-normal">{run.playbook}</span>
        </div>
        <StatusBadge run={run} />
        <ModeBadge run={run} />
        <CheckoutBadge run={run} />
        <SourceBadge run={run} />
        <span className="text-sm text-muted-foreground">{runDuration(run, now)}</span>
        {run.ansible_core_version && (
          <span className="ml-auto text-xs text-muted-foreground">ansible-core {run.ansible_core_version}</span>
        )}
      </div>
      {run.failure_reason && (
        <div className="px-6 py-2 border-b border-red-200 bg-red-50 text-sm text-red-700">{run.failure_reason}</div>
      )}
      <LogView lines={lines} live={!isFinished(run)} />
    </div>
  );
}

function LogView({ lines, live }: { lines: LogLine[]; live: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines.length, follow]);
  return (
    <div className="relative flex-1 min-h-0">
      <div
        ref={ref}
        onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 20);
        }}
        className="h-full overflow-auto font-mono text-[12px] leading-5 p-3 bg-card"
      >
        {lines.map((line) => (
          <div
            key={line.seq}
            className={cn("whitespace-pre-wrap", line.kind === "locomotive" && "text-violet-700")}
          >
            {line.text || " "}
          </div>
        ))}
        {live && <div className="text-zinc-300 animate-pulse">▍</div>}
      </div>
      {!follow && (
        <button
          type="button"
          onClick={() => setFollow(true)}
          className="absolute bottom-3 right-5 text-xs rounded-full bg-primary text-primary-foreground px-3 py-1 shadow"
        >
          ↓ follow
        </button>
      )}
    </div>
  );
}

import { NavLink } from "react-router";

import { CheckoutBadge, ModeBadge, SourceBadge, StatusBadge } from "@/components/RunBadges";
import { useNow, usePolling } from "@/hooks/usePolling";
import { api } from "@/lib/api";
import { runDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

export function RecentRuns() {
  const { data: runs, error } = usePolling(() => api.runs(20), 3000);
  const now = useNow();
  return (
    <aside className="w-80 shrink-0 border-l border-border bg-card flex flex-col min-h-0">
      <div className="px-4 h-14 flex items-center text-sm font-medium">Recent Runs</div>
      <div className="flex-1 overflow-auto px-2 pb-4 space-y-1">
        {error && <div className="px-2 text-xs text-destructive">{error}</div>}
        {runs?.length === 0 && <div className="px-2 text-sm text-muted-foreground">No Runs yet.</div>}
        {runs?.map((run) => (
          <NavLink
            key={run.id}
            to={`/runs/${run.id}`}
            className={({ isActive }) =>
              cn("block rounded-md px-2 py-2 hover:bg-muted", isActive && "bg-muted")
            }
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-sm truncate">
                <span className="text-muted-foreground">#{run.id}</span> {run.playbook}
              </span>
              <StatusBadge run={run} />
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <SourceBadge run={run} />
              <ModeBadge run={run} />
              <CheckoutBadge run={run} />
              <span className="ml-auto text-xs text-muted-foreground">{runDuration(run, now)}</span>
            </div>
          </NavLink>
        ))}
      </div>
    </aside>
  );
}

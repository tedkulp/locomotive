import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { usePolling } from "@/hooks/usePolling";
import { api, type HousekeepingPass } from "@/lib/api";
import { formatBytes, formatWhen } from "@/lib/format";

export function StatusPage() {
  const { data: hk, error: hkError } = usePolling(() => api.housekeeping(), 10000);
  const { data: scheduler } = usePolling(() => api.scheduler(), 10000);
  const latest = hk?.passes.find((p) => p.finished_at);

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <h1 className="text-xl font-semibold">Status</h1>

      {scheduler && (
        <section>
          <h2 className="text-sm font-medium text-muted-foreground mb-2">Scheduler</h2>
          <Card className="p-4 text-sm grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <span className="text-muted-foreground">Schedules</span>
            <span>{scheduler.paused ? <Badge tone="amber">paused</Badge> : "running"}</span>
            <span className="text-muted-foreground">Time zone</span>
            <span>{scheduler.time_zone}</span>
            <span className="text-muted-foreground">Last tick</span>
            <span>{scheduler.last_tick_at ? formatWhen(scheduler.last_tick_at) : "not yet"}</span>
            <span className="text-muted-foreground">Scheduler Ping</span>
            <span>
              {!scheduler.ping_configured
                ? "not configured"
                : scheduler.last_ping_error
                  ? <span className="text-destructive">failing: {scheduler.last_ping_error}</span>
                  : scheduler.last_ping_at
                    ? `last sent ${formatWhen(scheduler.last_ping_at)}`
                    : "not sent yet"}
            </span>
          </Card>
        </section>
      )}

      <section>
        <h2 className="text-sm font-medium text-muted-foreground mb-2">Log housekeeping</h2>
        {hkError && <div className="text-sm text-destructive mb-2">{hkError}</div>}
        {hk && (
          <Card className="p-4 text-sm space-y-3">
            <div className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
              <span className="text-muted-foreground">Run logs</span>
              <span>
                {latest ? formatBytes(latest.total_bytes) : "…"} of {formatBytes(hk.budget_bytes)} budget
                {latest && latest.kept_bytes > 0 && (
                  <span className="text-muted-foreground"> ({formatBytes(latest.kept_bytes)} Kept)</span>
                )}
              </span>
              <span className="text-muted-foreground">Retention</span>
              <span>
                quiet scheduled Runs {hk.retention_days.quiet_scheduled} days, all others {hk.retention_days.other}{" "}
                days; Kept Runs never pruned
              </span>
              <span className="text-muted-foreground">Per-Run log cap</span>
              <span>{formatBytes(hk.log_cap_bytes)}</span>
              <span className="text-muted-foreground">Next pass</span>
              <span>
                {hk.next_pass_at ? formatWhen(hk.next_pass_at) : "at startup"}{" "}
                <span className="text-muted-foreground">(daily at 04:00 {hk.time_zone}, and at startup)</span>
              </span>
            </div>
            {latest?.warning && (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800">
                {latest.warning}
              </div>
            )}
          </Card>
        )}
      </section>

      {hk && (
        <section>
          <h2 className="text-sm font-medium text-muted-foreground mb-2">Housekeeping passes</h2>
          <Card className="p-2">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground border-b border-border">
                  {["When", "Trigger", "Pruned", "Freed", "Compressed", "Logs after", "Notes"].map((h) => (
                    <th key={h} className="py-2 px-2 font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {hk.passes.map((p) => (
                  <PassRow key={p.id} pass={p} />
                ))}
                {hk.passes.length === 0 && (
                  <tr>
                    <td colSpan={7} className="py-6 text-center text-muted-foreground">No passes yet.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </Card>
        </section>
      )}
    </div>
  );
}

function PassRow({ pass }: { pass: HousekeepingPass }) {
  return (
    <tr className="border-b border-border/60 align-top">
      <td className="py-2 px-2 whitespace-nowrap text-muted-foreground">{formatWhen(pass.started_at)}</td>
      <td className="px-2 py-2">{pass.trigger}</td>
      <td className="px-2 py-2">
        {!pass.finished_at ? (
          <span className="text-muted-foreground">running…</span>
        ) : pass.pruned.length === 0 ? (
          <span className="text-muted-foreground">nothing</span>
        ) : (
          <details>
            <summary className="cursor-pointer">
              {pass.pruned.length} Run{pass.pruned.length === 1 ? "" : "s"}
            </summary>
            <ul className="mt-1 space-y-0.5 text-xs">
              {pass.pruned.map((p) => (
                <li key={p.run_id}>
                  <Link to={`/runs/${p.run_id}`} className="underline">
                    #{p.run_id}
                  </Link>{" "}
                  <span className="font-mono">{p.playbook}</span>{" "}
                  <span className="text-muted-foreground">
                    ({p.reason === "age" ? "age" : "budget"}, {formatBytes(p.bytes)})
                  </span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </td>
      <td className="px-2 py-2">{pass.finished_at ? formatBytes(pass.freed_bytes) : ""}</td>
      <td className="px-2 py-2">{pass.finished_at ? pass.compressed : ""}</td>
      <td className="px-2 py-2 whitespace-nowrap">{pass.finished_at ? formatBytes(pass.total_bytes) : ""}</td>
      <td className="px-2 py-2 text-xs">
        {pass.error && <div className="text-destructive">{pass.error}</div>}
        {pass.warning && (
          <div className="text-amber-700">
            Kept logs over budget{pass.warning_sent ? " (ntfy warning sent)" : ""}
          </div>
        )}
      </td>
    </tr>
  );
}

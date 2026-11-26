import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { CheckoutBadge, ModeBadge, SourceBadge, StatusBadge } from "@/components/RunBadges";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useNow, usePolling } from "@/hooks/usePolling";
import { api, type Run } from "@/lib/api";
import { formatWhen, runDuration } from "@/lib/format";

const PAGE = 50;
/** History entries whose reason is worth a column: why it was skipped, missed, cut off or failed. */
const WITH_REASON = new Set(["skipped", "missed", "interrupted", "failed", "canceled"]);

export function HistoryPage() {
  const navigate = useNavigate();
  // The newest page stays live; older pages are loaded on demand and kept.
  const { data: head, error } = usePolling(() => api.runsPage(PAGE), 5000);
  const [older, setOlder] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<number | null | undefined>(undefined); // undefined: not paged yet
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const now = useNow();

  // New Runs push the live page down: fetch whatever slid out between it and the older pages.
  const headOldest = head?.runs.at(-1)?.id;
  const olderNewest = older[0]?.id;
  useEffect(() => {
    if (headOldest == null || olderNewest == null || headOldest - olderNewest <= 1) return;
    api.runsPage(Math.min(500, headOldest - olderNewest - 1), headOldest).then(
      (page) => setOlder((rows) => [...page.runs.filter((r) => r.id > olderNewest), ...rows]),
      (err: Error) => setLoadError(err.message),
    );
  }, [headOldest, olderNewest]);

  const nextCursor = cursor === undefined ? head?.next_before : cursor;
  const loadOlder = () => {
    const before = older.at(-1)?.id ?? nextCursor;
    if (before == null) return;
    setLoading(true);
    setLoadError(null);
    api
      .runsPage(PAGE, before)
      .then(
        (page) => {
          setOlder((rows) => [...rows, ...page.runs]);
          setCursor(page.next_before);
        },
        (err: Error) => setLoadError(err.message),
      )
      .finally(() => setLoading(false));
  };

  const seen = new Set<number>();
  const runs = [...(head?.runs ?? []), ...older].filter((run) => !seen.has(run.id) && seen.add(run.id));
  const columns = ["#", "Playbook", "Source", "Mode", "Checkout", "Result", "Reason", "Duration", "When"];
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">Run history</h1>
      {(error || loadError) && <div className="text-sm text-destructive mb-2">{error ?? loadError}</div>}
      <Card className="p-2">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b border-border">
              {columns.map((h) => (
                <th key={h} className="py-2 px-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.id}
                onClick={() => navigate(`/runs/${run.id}`)}
                className="border-b border-border/60 hover:bg-muted cursor-pointer"
              >
                <td className="py-2 px-2 text-muted-foreground font-mono">{run.id}</td>
                <td className="px-2 font-mono">{run.playbook}</td>
                <td className="px-2"><SourceBadge run={run} /></td>
                <td className="px-2"><ModeBadge run={run} /></td>
                <td className="px-2"><CheckoutBadge run={run} /></td>
                <td className="px-2"><StatusBadge run={run} /></td>
                <td className="px-2 text-muted-foreground max-w-80 truncate" title={run.failure_reason ?? undefined}>
                  {WITH_REASON.has(run.status) ? run.failure_reason : null}
                </td>
                <td className="px-2 text-muted-foreground">{runDuration(run, now)}</td>
                <td className="px-2 text-muted-foreground whitespace-nowrap">{formatWhen(run.created_at)}</td>
              </tr>
            ))}
            {head && runs.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="py-6 text-center text-muted-foreground">No Runs yet.</td>
              </tr>
            )}
          </tbody>
        </table>
        {nextCursor != null && (
          <div className="flex justify-center py-2">
            <Button variant="outline" disabled={loading} onClick={loadOlder}>
              {loading ? "Loading…" : "Load older Runs"}
            </Button>
          </div>
        )}
      </Card>
    </div>
  );
}

import { useNavigate } from "react-router";

import { CheckoutBadge, ModeBadge, SourceBadge, StatusBadge } from "@/components/RunBadges";
import { Card } from "@/components/ui/card";
import { useNow, usePolling } from "@/hooks/usePolling";
import { api } from "@/lib/api";
import { formatWhen, runDuration } from "@/lib/format";

export function HistoryPage() {
  const navigate = useNavigate();
  const { data: runs, error } = usePolling(() => api.runs(500), 5000);
  const now = useNow();
  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold mb-4">Run history</h1>
      {error && <div className="text-sm text-destructive mb-2">{error}</div>}
      <Card className="p-2">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b border-border">
              {["#", "Playbook", "Source", "Mode", "Checkout", "Result", "Duration", "When"].map((h) => (
                <th key={h} className="py-2 px-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {runs?.map((run) => (
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
                <td className="px-2 text-muted-foreground">{runDuration(run, now)}</td>
                <td className="px-2 text-muted-foreground whitespace-nowrap">{formatWhen(run.created_at)}</td>
              </tr>
            ))}
            {runs?.length === 0 && (
              <tr>
                <td colSpan={8} className="py-6 text-center text-muted-foreground">No Runs yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

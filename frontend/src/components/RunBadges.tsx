import { CalendarClock } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { Run } from "@/lib/api";
import { shortSha, statusTone } from "@/lib/format";

export function StatusBadge({ run }: { run: Run }) {
  return (
    <Badge tone={statusTone(run.status)}>
      {run.status}
      {run.stage ? ` · ${run.stage}` : ""}
    </Badge>
  );
}

export function ModeBadge({ run }: { run: Run }) {
  return run.check ? <Badge>check</Badge> : <Badge tone="red">real</Badge>;
}

export function SourceBadge({ run }: { run: Run }) {
  const tone = run.source === "schedule" ? "violet" : run.source.startsWith("mcp") ? "sky" : "zinc";
  return (
    <Badge tone={tone} title={run.schedule ? `Schedule: ${run.schedule.name}` : undefined}>
      {run.schedule ? (
        <>
          <CalendarClock className="size-3" /> {run.schedule.name}
        </>
      ) : (
        run.source
      )}
    </Badge>
  );
}

export function CheckoutBadge({ run }: { run: Run }) {
  if (run.checkout === "working_copy")
    return (
      <Badge tone="amber" title={`Working Copy Run on ${run.branch}${run.dirty ? " with uncommitted changes" : ""}`}>
        WC{" "}
        <span className="font-mono">
          {shortSha(run.sha)}
          {run.dirty ? "+dirty" : ""}
        </span>
      </Badge>
    );
  return (
    <Badge>
      {run.branch} <span className="font-mono">{shortSha(run.sha)}</span>
    </Badge>
  );
}

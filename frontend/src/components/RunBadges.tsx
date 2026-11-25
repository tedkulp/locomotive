import { Badge } from "@/components/ui/badge";
import type { Run } from "@/lib/api";
import { shortSha, statusTone } from "@/lib/format";

export function StatusBadge({ run }: { run: Run }) {
  return <Badge tone={statusTone(run.status)}>{run.status}</Badge>;
}

export function ModeBadge({ run }: { run: Run }) {
  return run.check ? <Badge>check</Badge> : <Badge tone="red">real</Badge>;
}

export function SourceBadge({ run }: { run: Run }) {
  const tone = run.source === "schedule" ? "violet" : run.source.startsWith("mcp") ? "sky" : "zinc";
  return <Badge tone={tone}>{run.source}</Badge>;
}

export function CheckoutBadge({ run }: { run: Run }) {
  if (run.checkout === "working_copy") return <Badge tone="amber">working copy</Badge>;
  return (
    <Badge>
      {run.branch} <span className="font-mono">{shortSha(run.sha)}</span>
    </Badge>
  );
}

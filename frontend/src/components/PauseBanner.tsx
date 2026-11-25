import { PauseCircle } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useScheduler } from "@/hooks/useScheduler";

/** Shown on every page while all Schedules are paused. */
export function PauseBanner() {
  const { status, setPaused } = useScheduler();
  if (!status?.paused) return null;
  return (
    <div className="flex items-center gap-3 border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-900">
      <PauseCircle className="size-4 shrink-0" />
      <span>
        <strong>All Schedules are paused.</strong> Firings are dropped without a record until you resume.
      </span>
      <Button size="sm" variant="outline" className="ml-auto" onClick={() => setPaused(false)}>
        Resume Schedules
      </Button>
    </div>
  );
}

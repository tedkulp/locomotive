import { useCallback, useEffect, useState } from "react";

import { api, type SchedulerStatus } from "@/lib/api";

const listeners = new Set<(s: SchedulerStatus) => void>();

/** Scheduler state (pause all, ping), polled; every user of the hook sees a change at once. */
export function useScheduler(ms = 5000) {
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const tick = () => api.scheduler().then((s) => alive && setStatus(s), () => {});
    listeners.add(setStatus);
    tick();
    const id = setInterval(tick, ms);
    return () => {
      alive = false;
      listeners.delete(setStatus);
      clearInterval(id);
    };
  }, [ms]);
  const setPaused = useCallback(async (paused: boolean) => {
    const next = await api.setPaused(paused);
    listeners.forEach((l) => l(next));
  }, []);
  return { status, setPaused };
}

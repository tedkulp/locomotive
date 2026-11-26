import { useEffect, useState } from "react";

import { api, type Grid, type LogLine, type Run } from "@/lib/api";

/** Follows a Run over SSE: its log lines, the latest grid + recap, and the final Run when it ends. */
export function useRunLog(runId: number) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [grid, setGrid] = useState<Grid | null>(null);
  const [finalRun, setFinalRun] = useState<Run | null>(null);
  useEffect(() => {
    setLines([]);
    setGrid(null);
    setFinalRun(null);
    const source = new EventSource(api.logStreamUrl(runId));
    source.addEventListener("log", (e) => {
      const line = JSON.parse((e as MessageEvent).data) as LogLine;
      setLines((prev) => (prev.length && prev[prev.length - 1].seq >= line.seq ? prev : [...prev, line]));
    });
    source.addEventListener("grid", (e) => {
      setGrid(JSON.parse((e as MessageEvent).data) as Grid);
    });
    source.addEventListener("end", (e) => {
      setFinalRun(JSON.parse((e as MessageEvent).data) as Run);
      source.close();
    });
    return () => source.close();
  }, [runId]);
  return { lines, grid, finalRun };
}

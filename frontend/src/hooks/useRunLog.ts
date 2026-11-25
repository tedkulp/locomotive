import { useEffect, useState } from "react";

import { api, type LogLine, type Run } from "@/lib/api";

/** Follows a Run's log over SSE; resolves the final Run when the stream ends. */
export function useRunLog(runId: number) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [finalRun, setFinalRun] = useState<Run | null>(null);
  useEffect(() => {
    setLines([]);
    setFinalRun(null);
    const source = new EventSource(api.logStreamUrl(runId));
    source.addEventListener("log", (e) => {
      const line = JSON.parse((e as MessageEvent).data) as LogLine;
      setLines((prev) => (prev.length && prev[prev.length - 1].seq >= line.seq ? prev : [...prev, line]));
    });
    source.addEventListener("end", (e) => {
      setFinalRun(JSON.parse((e as MessageEvent).data) as Run);
      source.close();
    });
    return () => source.close();
  }, [runId]);
  return { lines, finalRun };
}

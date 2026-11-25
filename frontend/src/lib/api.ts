export type RunStatus = "running" | "succeeded" | "failed" | "canceled" | "interrupted" | "queued";

export interface Run {
  id: number;
  playbook: string;
  status: RunStatus;
  source: string;
  checkout: "clean" | "working_copy";
  branch: string;
  sha: string | null;
  check: boolean;
  requirements_hash: string | null;
  ansible_core_version: string | null;
  rc: number | null;
  failure_reason: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  recap: Recap | null;
  recap_source: RecapSource | null;
  stop_requested: boolean;
}

export type RecapSource = "ansible" | "locomotive";

export interface HostRecap {
  ok: number;
  changed: number;
  skipped: number;
  failed: number;
  unreachable: number;
}

export type Recap = Record<string, HostRecap>;

export type CellStatus =
  | "running"
  | "ok"
  | "changed"
  | "skipped"
  | "failed"
  | "ignored"
  | "unreachable"
  | "interrupted";

export interface GridTask {
  id: string;
  name: string;
  play: string | null;
  included_from: string | null;
  results: Record<string, CellStatus>;
}

export interface Grid {
  version: number;
  hosts: string[];
  tasks: GridTask[];
  recap: Recap;
  recap_source: RecapSource | null;
}

export interface PlaybookList {
  branch: string;
  sha: string;
  playbooks: { name: string; path: string }[];
}

export interface LogLine {
  seq: number;
  text: string;
  kind: "ansible" | "locomotive";
  host: string | null;
}

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  playbooks: (branch?: string) =>
    request<PlaybookList>(`/api/playbooks${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`),
  runs: (limit = 50) => request<{ runs: Run[] }>(`/api/runs?limit=${limit}`).then((r) => r.runs),
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  startRun: (playbook: string) =>
    request<Run>("/api/runs", { method: "POST", body: JSON.stringify({ playbook }) }),
  grid: (id: number) => request<Grid>(`/api/runs/${id}/grid`),
  stopRun: (id: number) => request<Run>(`/api/runs/${id}/stop`, { method: "POST" }),
  killRun: (id: number) => request<Run>(`/api/runs/${id}/kill`, { method: "POST" }),
  runAgain: (id: number) => request<Run>(`/api/runs/${id}/again`, { method: "POST" }),
  logStreamUrl: (id: number) => `/api/runs/${id}/log/stream`,
};

export const isFinished = (run: Run) => !["running", "queued"].includes(run.status);

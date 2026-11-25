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
  logStreamUrl: (id: number) => `/api/runs/${id}/log/stream`,
};

export const isFinished = (run: Run) => !["running", "queued"].includes(run.status);

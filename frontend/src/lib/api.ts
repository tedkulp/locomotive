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
  diff: boolean;
  verbosity: number;
  limit: string | null;
  hosts: string[] | null;
  tags: string[];
  extra_vars: Record<string, unknown>;
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
  /** While queued: the Run it waits for (holding, or queued ahead for, one of its hosts). */
  waiting_for: { id: number; playbook: string } | null;
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

export interface LaunchOptions {
  playbook: string;
  limit: string | null;
  tags: string[];
  check: boolean;
  diff: boolean;
  verbosity: number;
  extra_vars: Record<string, unknown>;
}

export interface PreviewTask {
  name: string;
  role: string | null;
  action: string | null;
  tags: string[];
  selected: boolean;
  include: {
    kind: "include_tasks" | "include_role" | null;
    target: string | null;
    expandable: boolean;
    reason: string | null;
    task_count: number | null;
    tasks: PreviewTask[] | null;
  } | null;
}

export interface Preview {
  branch: string;
  sha: string;
  command: string;
  hosts: {
    hosts: string[];
    all_hosts: string[];
    unmatched: string[];
    plays: { name: string; pattern: string; hosts: string[]; all_hosts: string[] }[];
  };
  tasks: {
    label: string;
    plays: { name: string; pattern: string; tags: string[]; tasks: PreviewTask[] }[];
  };
  tags: string[];
  inventory: { groups: { name: string; children: string[]; hosts: string[] }[] };
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
  startRun: (options: LaunchOptions) =>
    request<Run>("/api/runs", { method: "POST", body: JSON.stringify(options) }),
  preview: (
    options: LaunchOptions & { expand_includes: boolean; sha?: string },
    signal?: AbortSignal,
  ) => request<Preview>("/api/preview", { method: "POST", body: JSON.stringify(options), signal }),
  grid: (id: number) => request<Grid>(`/api/runs/${id}/grid`),
  stopRun: (id: number) => request<Run>(`/api/runs/${id}/stop`, { method: "POST" }),
  cancelRun: (id: number) => request<Run>(`/api/runs/${id}/cancel`, { method: "POST" }),
  killRun: (id: number) => request<Run>(`/api/runs/${id}/kill`, { method: "POST" }),
  runAgain: (id: number) => request<Run>(`/api/runs/${id}/again`, { method: "POST" }),
  logStreamUrl: (id: number) => `/api/runs/${id}/log/stream`,
};

export const isFinished = (run: Run) => !["running", "queued"].includes(run.status);

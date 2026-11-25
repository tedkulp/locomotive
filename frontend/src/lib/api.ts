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
  logStreamUrl: (id: number) => `/api/runs/${id}/log/stream`,
};

export const isFinished = (run: Run) => !["running", "queued"].includes(run.status);

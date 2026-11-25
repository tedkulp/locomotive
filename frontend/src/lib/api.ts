export type RunStatus =
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "interrupted"
  | "queued"
  /** History entries a Schedule leaves instead of a Run; the reason is in failure_reason. */
  | "skipped"
  | "missed";

export interface Run {
  id: number;
  playbook: string;
  status: RunStatus;
  source: string;
  checkout: CheckoutKind;
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
  /** The Schedule that fired this Run (or left this skipped/missed entry). */
  schedule: { id: number | null; name: string } | null;
  /** Working Copy Runs only (null for clean Runs). */
  dirty: boolean | null;
  untracked: string[] | null;
  diff_truncated: boolean | null;
}

export type CheckoutKind = "clean" | "working_copy";

export type WorkingCopyStatus =
  | { available: false; path?: string; reason: string }
  | {
      available: true;
      path: string;
      branch: string | null;
      head_sha: string;
      dirty: boolean;
      changed: string[];
      untracked: string[];
      upstream: string | null;
      ahead: number | null;
      behind: number | null;
    };

export interface WorkingCopyDiff {
  sha: string | null;
  dirty: boolean | null;
  diff: string | null;
  truncated: boolean | null;
  untracked: string[] | null;
}

/** Which checkout a launch, listing or preview uses: a clean branch, or the Working Copy in place. */
export type CheckoutChoice = { checkout: "clean"; branch: string } | { checkout: "working_copy" };

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
  checkout: CheckoutKind;
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
  checkout: CheckoutKind;
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
      else if (Array.isArray(body.detail))
        detail = body.detail.map((e: { loc?: unknown[]; msg?: string }) => `${(e.loc ?? []).slice(1).join(".")}: ${e.msg}`).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export interface ScheduleSettings {
  name: string;
  playbook: string;
  branch: string | null;
  limit: string | null;
  tags: string[];
  check: boolean;
  diff: boolean;
  verbosity: number;
  extra_vars: Record<string, unknown>;
  cron: string;
  enabled: boolean;
  notify_on_changes: boolean;
}

export interface Schedule extends ScheduleSettings {
  id: number;
  last_fired_at: string;
  next_fire_at: string | null;
  created_at: string;
  last_run: { id: number; status: RunStatus } | null;
}

export interface SchedulerStatus {
  paused: boolean;
  time_zone: string;
  last_tick_at: string | null;
  ping_configured: boolean;
  last_ping_at: string | null;
  last_ping_error: string | null;
}

export const api = {
  playbooks: (choice: CheckoutChoice) =>
    request<PlaybookList>(
      choice.checkout === "working_copy"
        ? "/api/playbooks?checkout=working_copy"
        : `/api/playbooks?branch=${encodeURIComponent(choice.branch)}`,
    ),
  branches: () => request<{ default: string; branches: string[] }>("/api/branches"),
  workingCopy: () => request<WorkingCopyStatus>("/api/working-copy"),
  workingCopyDiff: (id: number) => request<WorkingCopyDiff>(`/api/runs/${id}/working-copy-diff`),
  runs: (limit = 50) => request<{ runs: Run[] }>(`/api/runs?limit=${limit}`).then((r) => r.runs),
  run: (id: number) => request<Run>(`/api/runs/${id}`),
  startRun: (options: LaunchOptions & CheckoutChoice) =>
    request<Run>("/api/runs", { method: "POST", body: JSON.stringify(options) }),
  preview: (
    options: LaunchOptions & CheckoutChoice & { expand_includes: boolean; sha?: string },
    signal?: AbortSignal,
  ) => request<Preview>("/api/preview", { method: "POST", body: JSON.stringify(options), signal }),
  grid: (id: number) => request<Grid>(`/api/runs/${id}/grid`),
  stopRun: (id: number) => request<Run>(`/api/runs/${id}/stop`, { method: "POST" }),
  cancelRun: (id: number) => request<Run>(`/api/runs/${id}/cancel`, { method: "POST" }),
  killRun: (id: number) => request<Run>(`/api/runs/${id}/kill`, { method: "POST" }),
  runAgain: (id: number) => request<Run>(`/api/runs/${id}/again`, { method: "POST" }),
  logStreamUrl: (id: number) => `/api/runs/${id}/log/stream`,
  schedules: () => request<SchedulerStatus & { schedules: Schedule[] }>("/api/schedules"),
  createSchedule: (settings: ScheduleSettings) =>
    request<Schedule>("/api/schedules", { method: "POST", body: JSON.stringify(settings) }),
  updateSchedule: (id: number, fields: Partial<ScheduleSettings>) =>
    request<Schedule>(`/api/schedules/${id}`, { method: "PATCH", body: JSON.stringify(fields) }),
  deleteSchedule: (id: number) => request<void>(`/api/schedules/${id}`, { method: "DELETE" }),
  scheduler: () => request<SchedulerStatus>("/api/scheduler"),
  setPaused: (paused: boolean) =>
    request<SchedulerStatus>("/api/scheduler", { method: "PUT", body: JSON.stringify({ paused }) }),
};

export const isFinished = (run: Run) => !["running", "queued"].includes(run.status);

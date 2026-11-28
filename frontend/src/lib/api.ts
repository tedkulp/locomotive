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
  /** While queued: preparing (fetching, installing its toolchain, resolving hosts) or waiting (for hosts). */
  stage: "preparing" | "waiting" | null;
  /** The Schedule that fired this Run (or left this skipped/missed entry). */
  schedule: { id: number | null; name: string } | null;
  /** The first task that failed on some host, set when the Run ends. */
  first_failed_task: { name: string; hosts: string[] } | null;
  /** ntfy delivery of this Run's notification; null when it wasn't notable. Never changes the Run's status. */
  notification: { status: "pending" | "sent" | "failed"; attempts: number | null; error: string | null } | null;
  /** Working Copy Runs only (null for clean Runs). */
  dirty: boolean | null;
  untracked: string[] | null;
  diff_truncated: boolean | null;
  /** Keep exempts the Run's log from pruning. */
  keep: boolean;
  /** live / complete / truncated (stopped at the per-Run cap) / pruned (log, events and grid deleted). */
  log_state: "live" | "complete" | "truncated" | "pruned";
  log_truncated: boolean;
  log_pruned: boolean;
  log_pruned_at: string | null;
  /** Disk used by the Run's log, events and grid once finished. */
  log_bytes: number | null;
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
  /** seq of the task's `TASK [...]` banner in the Run log; null if it isn't in the log (truncated, or an older grid). */
  log_seq: number | null;
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
  /** ``description`` is the Playbook's header comment (plain text, paragraphs split by a blank line);
   *  ``summary`` its first paragraph. Both null when it has none. */
  playbooks: { name: string; path: string; description: string | null; summary: string | null }[];
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
  /** Descriptions from the repo's locomotive.yml, for the tags above that have one. */
  tag_descriptions: Record<string, string>;
  /** Set when part of locomotive.yml had to be skipped (never blocks a Run). */
  tag_descriptions_warning: string | null;
  inventory: { groups: { name: string; children: string[]; hosts: string[] }[] };
}

export interface LogLine {
  seq: number;
  text: string;
  kind: "ansible" | "locomotive";
  host: string | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    /** Seconds to wait before asking again, when the server says it is still working (Retry-After). */
    readonly retryAfter?: number,
  ) {
    super(message);
  }
}

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
    const retryAfter = Number(resp.headers.get("Retry-After"));
    throw new ApiError(detail, resp.status, retryAfter > 0 ? retryAfter : undefined);
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

export interface HousekeepingPass {
  id: number;
  trigger: "startup" | "daily";
  started_at: string;
  finished_at: string | null;
  pruned: { run_id: number; playbook: string; reason: "age" | "budget"; bytes: number }[];
  compressed: number;
  freed_bytes: number;
  total_bytes: number;
  kept_bytes: number;
  budget_bytes: number;
  /** Set when Kept logs alone exceed the budget (nothing is pruned for it). */
  warning: string | null;
  warning_sent: boolean;
  error: string | null;
}

export interface HousekeepingStatus {
  time_zone: string;
  budget_bytes: number;
  log_cap_bytes: number;
  retention_days: { quiet_scheduled: number; other: number };
  last_check_at: string | null;
  next_pass_at: string | null;
  passes: HousekeepingPass[];
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
  /** One page of history, newest first, older than Run `before`; `next_before` is the next page's cursor. */
  runsPage: (limit: number, before?: number | null) =>
    request<{ runs: Run[]; next_before: number | null }>(
      `/api/runs?limit=${limit}${before != null ? `&before=${before}` : ""}`,
    ),
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
  setKeep: (id: number, keep: boolean) =>
    request<Run>(`/api/runs/${id}/keep`, { method: "PUT", body: JSON.stringify({ keep }) }),
  housekeeping: () => request<HousekeepingStatus>("/api/housekeeping"),
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

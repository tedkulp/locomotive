import type { Run } from "./api";

export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function runDuration(run: Run, now: number): string | null {
  if (run.duration_seconds != null) return formatDuration(run.duration_seconds);
  if (run.started_at && run.status === "running")
    return formatDuration((now - Date.parse(run.started_at)) / 1000);
  return null;
}

export function formatWhen(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `today ${time}`;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

export const shortSha = (sha: string | null) => (sha ? sha.slice(0, 7) : "…");

export const statusTone = (status: string) =>
  (
    ({
      succeeded: "green",
      failed: "red",
      canceled: "zinc",
      interrupted: "zinc",
      running: "sky",
      queued: "violet",
    }) as const
  )[status] ?? "zinc";

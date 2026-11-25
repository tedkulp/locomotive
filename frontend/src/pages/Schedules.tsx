import { Pause, Pencil, Play, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { usePolling } from "@/hooks/usePolling";
import { useScheduler } from "@/hooks/useScheduler";
import { api, type Schedule, type ScheduleSettings } from "@/lib/api";
import { formatWhen, statusTone } from "@/lib/format";
import { cn } from "@/lib/utils";

const INPUT = "w-full rounded-md border border-border bg-card px-2 py-1.5 text-sm";

const BLANK: ScheduleSettings = {
  name: "",
  playbook: "",
  branch: null,
  limit: null,
  tags: [],
  check: true,
  diff: false,
  verbosity: 0,
  extra_vars: {},
  cron: "0 3 * * *",
  enabled: true,
  notify_on_changes: true,
};

export function SchedulesPage() {
  const [version, setVersion] = useState(0);
  const { data, error } = usePolling(() => api.schedules(), 5000, [version]);
  const { status, setPaused } = useScheduler();
  const [editing, setEditing] = useState<Schedule | "new" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const refresh = () => setVersion((v) => v + 1);

  const act = async (fn: () => Promise<unknown>) => {
    setActionError(null);
    try {
      await fn();
      refresh();
    } catch (err) {
      setActionError((err as Error).message);
    }
  };

  const paused = status?.paused ?? data?.paused ?? false;

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold">Schedules</h1>
        {data && <span className="text-xs text-muted-foreground">cron times in {data.time_zone}</span>}
        <div className="ml-auto flex gap-2">
          <Button variant="outline" onClick={() => act(() => setPaused(!paused))}>
            {paused ? <Play /> : <Pause />}
            {paused ? "Resume all Schedules" : "Pause all Schedules"}
          </Button>
          <Button onClick={() => setEditing("new")}>
            <Plus /> New Schedule
          </Button>
        </div>
      </div>
      {(error || actionError) && <div className="text-sm text-destructive">{actionError ?? error}</div>}

      {editing && (
        <ScheduleForm
          initial={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}

      <Card className="p-2">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground border-b border-border">
              {["Enabled", "Name", "Playbook", "Cron", "Next firing", "Last Run", "Notify on changes", ""].map((h) => (
                <th key={h} className="py-2 px-2 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.schedules.map((s) => (
              <tr key={s.id} className={cn("border-b border-border/60", !s.enabled && "text-muted-foreground")}>
                <td className="py-2 px-2">
                  <Toggle
                    checked={s.enabled}
                    label={`Enable ${s.name}`}
                    onChange={(enabled) => act(() => api.updateSchedule(s.id, { enabled }))}
                  />
                </td>
                <td className="px-2 font-medium">{s.name}</td>
                <td className="px-2">
                  <span className="font-mono">{s.playbook}</span>
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {s.check ? <Badge>check</Badge> : <Badge tone="red">real</Badge>}
                    <Badge>{s.branch ?? "default branch"}</Badge>
                    {s.limit && <Badge title="limit">{s.limit}</Badge>}
                    {s.tags.length > 0 && <Badge title="tags">{s.tags.join(",")}</Badge>}
                  </div>
                </td>
                <td className="px-2 font-mono text-xs">{s.cron}</td>
                <td className="px-2 whitespace-nowrap">
                  {s.next_fire_at ? (
                    <span className={cn(paused && "line-through text-muted-foreground")}>
                      {formatWhen(s.next_fire_at)}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-2">
                  {s.last_run ? (
                    <Link to={`/runs/${s.last_run.id}`} className="inline-flex items-center gap-1">
                      <span className="text-muted-foreground font-mono">#{s.last_run.id}</span>
                      <Badge tone={statusTone(s.last_run.status)}>{s.last_run.status}</Badge>
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">never</span>
                  )}
                </td>
                <td className="px-2">
                  <Toggle
                    checked={s.notify_on_changes}
                    label={`Notify on changes for ${s.name}`}
                    onChange={(notify_on_changes) => act(() => api.updateSchedule(s.id, { notify_on_changes }))}
                  />
                </td>
                <td className="px-2 whitespace-nowrap text-right">
                  <Button size="sm" variant="ghost" title="Edit" onClick={() => setEditing(s)}>
                    <Pencil />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Delete"
                    onClick={() => {
                      if (confirm(`Delete Schedule "${s.name}"? Its Runs stay in history.`))
                        act(() => api.deleteSchedule(s.id));
                    }}
                  >
                    <Trash2 />
                  </Button>
                </td>
              </tr>
            ))}
            {data?.schedules.length === 0 && (
              <tr>
                <td colSpan={8} className="py-6 text-center text-muted-foreground">
                  No Schedules yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>
      {status?.ping_configured && (
        <p className="text-xs text-muted-foreground">
          Scheduler Ping:{" "}
          {status.last_ping_error
            ? `failing (${status.last_ping_error})`
            : status.last_ping_at
              ? `last sent ${formatWhen(status.last_ping_at)}`
              : "not sent yet"}
        </p>
      )}
    </div>
  );
}

function Toggle({ checked, label, onChange }: { checked: boolean; label: string; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors",
        checked ? "bg-primary" : "bg-zinc-300",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 size-4 rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

function ScheduleForm({
  initial,
  onCancel,
  onSaved,
}: {
  initial: Schedule | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<ScheduleSettings>(() => (initial ? { ...BLANK, ...initial } : BLANK));
  const [tags, setTags] = useState(form.tags.join(", "));
  const [extraVars, setExtraVars] = useState(
    Object.keys(form.extra_vars).length ? JSON.stringify(form.extra_vars, null, 2) : "",
  );
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { data: branches } = usePolling(() => api.branches(), 60_000);
  const branch = form.branch ?? branches?.default ?? "main";
  const { data: playbooks } = usePolling(() => api.playbooks({ checkout: "clean", branch }), 60_000, [branch]);
  const set = <K extends keyof ScheduleSettings>(key: K, value: ScheduleSettings[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  useEffect(() => {
    if (!form.playbook && playbooks?.playbooks.length) set("playbook", playbooks.playbooks[0].name);
  }, [playbooks, form.playbook]);

  const save = async () => {
    setError(null);
    let extra: Record<string, unknown> = {};
    if (extraVars.trim()) {
      try {
        extra = JSON.parse(extraVars);
      } catch {
        setError("Extra vars must be a JSON object");
        return;
      }
    }
    const body: ScheduleSettings = {
      ...form,
      limit: form.limit?.trim() || null,
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      extra_vars: extra,
    };
    setSaving(true);
    try {
      if (initial) await api.updateSchedule(initial.id, body);
      else await api.createSchedule(body);
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="p-4 space-y-3">
      <h2 className="font-medium">{initial ? `Edit ${initial.name}` : "New Schedule"}</h2>
      <p className="text-xs text-muted-foreground">
        Each firing runs a clean export of the branch (never the Working Copy).
      </p>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Field label="Name">
          <input className={INPUT} value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="nightly drift" />
        </Field>
        <Field label="Cron (minute hour day month weekday)">
          <input className={cn(INPUT, "font-mono")} value={form.cron} onChange={(e) => set("cron", e.target.value)} />
        </Field>
        <Field label="Branch">
          <select
            className={INPUT}
            value={form.branch ?? ""}
            onChange={(e) => set("branch", e.target.value || null)}
          >
            <option value="">default branch{branches ? ` (${branches.default})` : ""}</option>
            {branches?.branches
              .filter((b) => b !== branches.default || form.branch === b)
              .map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            {form.branch && branches && !branches.branches.includes(form.branch) && (
              <option value={form.branch}>{form.branch} (not on the remote)</option>
            )}
          </select>
        </Field>
        <Field label="Playbook">
          <select className={cn(INPUT, "font-mono")} value={form.playbook} onChange={(e) => set("playbook", e.target.value)}>
            {form.playbook && !playbooks?.playbooks.some((p) => p.name === form.playbook) && (
              <option value={form.playbook}>{form.playbook}</option>
            )}
            {playbooks?.playbooks.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Hosts (limit; empty = every host the Playbook targets)">
          <input
            className={cn(INPUT, "font-mono")}
            value={form.limit ?? ""}
            onChange={(e) => set("limit", e.target.value)}
            placeholder="servers:!pve1"
          />
        </Field>
        <Field label="Tags (comma separated)">
          <input className={cn(INPUT, "font-mono")} value={tags} onChange={(e) => setTags(e.target.value)} />
        </Field>
        <Field label="Extra vars (JSON)">
          <textarea
            className={cn(INPUT, "font-mono text-xs h-20")}
            value={extraVars}
            onChange={(e) => setExtraVars(e.target.value)}
            placeholder='{"key": "value"}'
          />
        </Field>
        <div className="space-y-2 text-sm">
          <Check label="Check mode (no changes made)" checked={form.check} onChange={(v) => set("check", v)} />
          <Check label="Diff" checked={form.diff} onChange={(v) => set("diff", v)} />
          <Check label="Enabled" checked={form.enabled} onChange={(v) => set("enabled", v)} />
          <Check label="Notify on changes" checked={form.notify_on_changes} onChange={(v) => set("notify_on_changes", v)} />
          <label className="flex items-center gap-2">
            Verbosity
            <select
              className="rounded-md border border-border bg-card px-1 py-0.5 text-sm"
              value={form.verbosity}
              onChange={(e) => set("verbosity", Number(e.target.value))}
            >
              {[0, 1, 2, 3, 4].map((v) => (
                <option key={v} value={v}>
                  {v === 0 ? "normal" : `-${"v".repeat(v)}`}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {!form.check && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          Real Run: each firing changes hosts unattended.
        </div>
      )}
      {error && <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      <div className="flex gap-2">
        <Button onClick={save} disabled={saving || !form.name.trim() || !form.playbook}>
          {initial ? "Save" : "Create Schedule"}
        </Button>
        <Button variant="outline" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { usePolling } from "@/hooks/usePolling";
import { useRememberedState } from "@/hooks/useRememberedState";
import {
  api,
  ApiError,
  type CheckoutChoice,
  type LaunchOptions,
  type PlaybookList,
  type Preview,
  type PreviewTask,
  type WorkingCopyStatus,
} from "@/lib/api";
import { parseExtraVars } from "@/lib/extraVars";
import { shortSha } from "@/lib/format";
import { cn } from "@/lib/utils";

const PREVIEW_DEBOUNCE_MS = 300;
const WORKING_COPY_POLL_MS = 5000;
const HIDDEN_GROUPS = new Set(["all", "ungrouped"]);


export function LaunchPage() {
  const navigate = useNavigate();
  const [listing, setListing] = useState<PlaybookList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playbook, setPlaybook] = useState<string | null>(null);
  const [limit, setLimit] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [check, setCheck] = useState(true);
  const [diff, setDiff] = useState(false);
  const [verbosity, setVerbosity] = useState(0);
  const [extraText, setExtraText] = useState("");
  const [expandIncludes, setExpandIncludes] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // Set while the server is still preparing the Ansible runtime for the preview (we keep asking).
  const [previewPreparing, setPreviewPreparing] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  // Remembered across page changes and reloads: the checkout choice is what Ted flips most while developing.
  const [fromWorkingCopy, setFromWorkingCopy] = useRememberedState("launch.fromWorkingCopy", false);
  const [branch, setBranch] = useRememberedState<string | null>("launch.branch", null);
  const [branches, setBranches] = useState<{ default: string; branches: string[] } | null>(null);
  const { data: wc } = usePolling(api.workingCopy, WORKING_COPY_POLL_MS);

  useEffect(() => {
    api.branches().then(setBranches, (e: Error) => setError(e.message));
  }, []);

  // A remembered branch that the remote no longer has falls back to the default.
  useEffect(() => {
    if (branches && branch !== null && !branches.branches.includes(branch)) setBranch(null);
  }, [branches, branch, setBranch]);

  const wcAvailable = wc?.available === true;
  // The switch is never overridden: if the Working Copy goes away while it is on, the form says so and
  // Start is blocked (never a silent fall back to a clean Run).
  const useWc = fromWorkingCopy;
  const wcLost = fromWorkingCopy && wc != null && !wcAvailable;
  const cleanBranch = branch ?? branches?.default ?? "main";
  const choice: CheckoutChoice = useMemo(
    () => (useWc ? { checkout: "working_copy" } : { checkout: "clean", branch: cleanBranch }),
    [useWc, cleanBranch],
  );
  // What the Working Copy looks like now: when it changes, Playbooks and previews are read again.
  const wcStamp = wc?.available ? JSON.stringify([wc.head_sha, wc.branch, wc.changed, wc.untracked]) : "";
  const choiceKey = JSON.stringify([choice, useWc ? wcStamp : null]);

  // Read from the chosen checkout every time it changes: Playbooks are never frozen.
  useEffect(() => {
    let alive = true;
    api.playbooks(choice).then(
      (l) => {
        if (!alive) return;
        setListing(l);
        setError(null);
        setPlaybook((current) =>
          current && l.playbooks.some((p) => p.name === current) ? current : (l.playbooks[0]?.name ?? null),
        );
      },
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
  }, [choiceKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const extra = useMemo(() => parseExtraVars(extraText), [extraText]);
  const options: LaunchOptions | null = useMemo(
    () =>
      playbook && "vars" in extra
        ? { playbook, limit: limit.trim() || null, tags, check, diff, verbosity, extra_vars: extra.vars }
        : null,
    [playbook, limit, tags, check, diff, verbosity, extra],
  );
  const optionsKey = options ? JSON.stringify([options, expandIncludes, choiceKey]) : null;

  // Preview hosts, tasks and the command for exactly these options (debounced, latest wins). While the
  // server is still preparing the Ansible runtime it answers 503 + Retry-After: keep asking until it's ready.
  useEffect(() => {
    if (!options || !optionsKey) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const ask = () =>
      api
        .preview(
          {
            ...options,
            ...choice,
            expand_includes: expandIncludes,
            // A clean listing's commit, so the preview doesn't fetch again; the Working Copy is read as is.
            sha:
              choice.checkout === "clean" && listing?.checkout === "clean" && listing.branch === choice.branch
                ? listing.sha
                : undefined,
          },
          controller.signal,
        )
        .then(
          (p) => {
            setPreview(p);
            setPreviewKey(optionsKey);
            setPreviewError(null);
            setPreviewPreparing(null);
          },
          (e: Error) => {
            if (controller.signal.aborted) return;
            if (e instanceof ApiError && e.status === 503 && e.retryAfter) {
              setPreviewPreparing(e.message);
              setPreviewError(null);
              timer = setTimeout(ask, e.retryAfter * 1000);
              return;
            }
            setPreviewPreparing(null);
            setPreviewKey(optionsKey);
            setPreviewError(e.message);
          },
        );
    timer = setTimeout(ask, PREVIEW_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [optionsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickPlaybook = (name: string) => {
    setPlaybook(name);
    setLimit("");
    setTags([]);
    setPreview(null);
    setPreviewError(null);
  };

  const current = previewKey === optionsKey && !previewError ? preview : null;
  const loading = previewKey !== optionsKey;
  const universe = preview?.hosts.all_hosts ?? [];
  const selected = new Set(current?.hosts.hosts ?? preview?.hosts.hosts ?? []);
  const groups = (preview?.inventory.groups ?? []).filter(
    (g) => !HIDDEN_GROUPS.has(g.name) && g.hosts.some((h) => universe.includes(h)),
  );

  const toggleHost = (host: string, on: boolean) => {
    const next = universe.filter((h) => (h === host ? on : selected.has(h)));
    setLimit(next.length === universe.length ? "" : next.join(","));
  };
  const toggleTag = (tag: string, on: boolean) =>
    setTags((t) => (on ? [...t, tag] : t.filter((x) => x !== tag)));

  const blocked =
    wcLost || !options || !current || current.hosts.hosts.length === 0 || current.hosts.unmatched.length > 0;

  const start = async () => {
    if (!options || blocked) return;
    setStarting(true);
    setError(null);
    try {
      const run = await api.startRun({ ...options, ...choice });
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  return (
    <div className="min-h-full flex flex-col">
      <div className="p-6 max-w-6xl w-full flex-1">
        <h1 className="text-xl font-semibold">Launch a Run</h1>

        <CheckoutBanner
          on={useWc}
          setOn={setFromWorkingCopy}
          wc={wc}
          listing={listing}
          branch={cleanBranch}
          branches={branches?.branches ?? null}
          setBranch={setBranch}
        />

        {error && (
          <div className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </div>
        )}

        <div className="mt-6 grid grid-cols-[240px_1fr] gap-6">
          <Card className="p-2 h-fit">
            <div className="px-2 py-1 text-xs font-medium text-muted-foreground">Playbook</div>
            {listing?.playbooks.map((p) => (
              <button
                key={p.name}
                type="button"
                onClick={() => pickPlaybook(p.name)}
                className={cn(
                  "w-full text-left rounded-md px-2 py-1.5 font-mono text-sm",
                  playbook === p.name ? "bg-primary text-primary-foreground" : "hover:bg-muted",
                )}
              >
                {p.name}
              </button>
            ))}
            {listing && listing.playbooks.length === 0 && (
              <div className="px-2 py-1 text-sm text-muted-foreground">No Playbooks in playbooks/.</div>
            )}
          </Card>

          <div className="space-y-4 min-w-0">
            <HostsCard
              universe={universe}
              selected={selected}
              groups={groups.map((g) => g.name)}
              limit={limit}
              setLimit={setLimit}
              toggleHost={toggleHost}
              preview={current}
              loading={loading}
            />

            <div className="grid grid-cols-2 gap-4">
              <Card className="p-4">
                <div className="font-medium mb-2">Tags</div>
                <div className="flex flex-wrap gap-x-4 gap-y-2">
                  {(preview?.tags ?? []).length ? (
                    preview?.tags.map((t) => (
                      <CheckBox key={t} on={tags.includes(t)} onChange={(v) => toggleTag(t, v)} label={t} />
                    ))
                  ) : (
                    <span className="text-sm text-muted-foreground">No tags in this Playbook</span>
                  )}
                </div>
                <div className="text-xs text-muted-foreground mt-2">None checked = run everything</div>
              </Card>
              <Card className="p-4 space-y-2">
                <div className="font-medium">Options</div>
                <CheckBox on={check} onChange={setCheck} label="Check mode" sub="--check" />
                <CheckBox on={diff} onChange={setDiff} label="Show diff" sub="--diff" />
                <label className="flex items-center gap-2 text-sm">
                  Verbosity
                  <select
                    value={verbosity}
                    onChange={(e) => setVerbosity(Number(e.target.value))}
                    className="rounded-md border border-border bg-card px-1 py-0.5 text-sm"
                  >
                    {[0, 1, 2, 3, 4].map((v) => (
                      <option key={v} value={v}>
                        {v === 0 ? "normal" : `-${"v".repeat(v)}`}
                      </option>
                    ))}
                  </select>
                </label>
                <textarea
                  value={extraText}
                  onChange={(e) => setExtraText(e.target.value)}
                  placeholder='Extra vars: {"key": "value"} or key=value'
                  className="w-full font-mono text-xs rounded-md border border-border bg-card p-2 h-16"
                />
                {"error" in extra && <div className="text-xs text-red-700">{extra.error}</div>}
              </Card>
            </div>

            <Card className="p-4">
              <TaskPreview
                preview={current ?? preview}
                stale={!current}
                expand={expandIncludes}
                setExpand={setExpandIncludes}
              />
            </Card>

            {previewPreparing && !previewError && (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 flex items-center gap-2">
                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-amber-600 border-t-transparent" />
                {previewPreparing}
              </div>
            )}

            {previewError && (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                Preview failed: {previewError}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="sticky bottom-0 border-t border-border bg-background/90 backdrop-blur px-6 py-3 flex items-center justify-end gap-3">
        <span className="font-mono text-xs text-muted-foreground truncate select-all">
          {current?.command ?? (loading ? "…" : "")}
        </span>
        <Button onClick={start} disabled={blocked || starting} variant={check ? "default" : "destructive"}>
          {starting ? "Starting…" : check ? "Start check Run" : "Start REAL Run"}
        </Button>
      </div>
    </div>
  );
}

/** The full-width "run from Working Copy" switch: amber with the uncommitted files when on, the branch
 *  dropdown for a clean export when off; the Working Copy's branch and ahead/behind either way. */
function CheckoutBanner({
  on,
  setOn,
  wc,
  listing,
  branch,
  branches,
  setBranch,
}: {
  on: boolean;
  setOn: (v: boolean) => void;
  wc: WorkingCopyStatus | null;
  listing: PlaybookList | null;
  branch: string;
  branches: string[] | null;
  setBranch: (v: string) => void;
}) {
  const available = wc?.available === true;
  const files = wc?.available
    ? [...wc.changed.map((f) => ({ f, untracked: false })), ...wc.untracked.map((f) => ({ f, untracked: true }))]
    : [];
  return (
    <div
      className={cn(
        "mt-4 rounded-lg border-2 p-4 flex items-start gap-4 transition-colors",
        on ? "border-amber-400 bg-amber-50" : "border-border bg-card",
      )}
    >
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label="Run from the Working Copy"
        disabled={!available && !on}
        onClick={() => setOn(!on)}
        title={available ? "Run from the Working Copy, in place, uncommitted edits included" : wc?.reason}
        className={cn(
          "mt-0.5 relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-40 disabled:cursor-not-allowed",
          on ? "bg-amber-500" : "bg-zinc-300",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform",
            on && "translate-x-5",
          )}
        />
      </button>
      <div className="flex-1 min-w-0">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <div className="font-semibold">
            {on ? "Running from the Working Copy" : <>Running a clean export of </>}
          </div>
          {!on && (
            <select
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              aria-label="Branch"
              className="rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-sm"
            >
              {(branches ?? [branch]).map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          )}
          <WorkingCopyState wc={wc} />
        </div>
        <div className="mt-1 text-sm text-muted-foreground">
          {on && wc?.available ? (
            <>
              In place at <span className="font-mono">{wc.path}</span>, HEAD{" "}
              <span className="font-mono">{shortSha(wc.head_sha)}</span>.{" "}
              {wc.dirty ? "Uncommitted state this Run will use:" : "No uncommitted changes."} locomotive never
              commits, pulls or pushes it.
            </>
          ) : listing?.checkout === "clean" ? (
            <>
              Fetched from the remote at <span className="font-mono">{shortSha(listing.sha)}</span>; the Run fetches
              again before it starts.
            </>
          ) : (
            "Fetching from the remote…"
          )}
        </div>
        {on && wc && !wc.available && (
          <div className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            The Working Copy is unavailable: {wc.reason}. Nothing can start from it until it is back; turn the
            switch off to run a clean export instead.
          </div>
        )}
        {on && files.length > 0 && (
          <ul className="mt-2 max-h-40 overflow-auto font-mono text-xs text-amber-900 space-y-0.5">
            {files.map(({ f, untracked }) => (
              <li key={f}>
                <span className="inline-block w-5 text-amber-600">{untracked ? "??" : "M"}</span>
                {f}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** The Working Copy's branch, dirty state and ahead/behind its upstream (as Ted last fetched it). */
function WorkingCopyState({ wc }: { wc: WorkingCopyStatus | null }) {
  if (!wc) return null;
  if (!wc.available)
    return <span className="text-xs text-muted-foreground">Working Copy unavailable: {wc.reason}</span>;
  const files = wc.changed.length + wc.untracked.length;
  return (
    <span className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-muted-foreground">Working Copy</span>
      <Badge>
        <span className="font-mono">{wc.branch ?? "detached HEAD"}</span>
      </Badge>
      {wc.dirty ? (
        <Badge tone="amber">
          {files} uncommitted file{files === 1 ? "" : "s"}
        </Badge>
      ) : (
        <Badge tone="green">clean</Badge>
      )}
      {wc.upstream ? (
        <Badge
          tone={wc.ahead || wc.behind ? "amber" : "zinc"}
          title={`Against ${wc.upstream} as last fetched in the Working Copy`}
        >
          ↑{wc.ahead ?? 0} ↓{wc.behind ?? 0} {wc.upstream}
        </Badge>
      ) : (
        <Badge>no upstream</Badge>
      )}
    </span>
  );
}

function CheckBox({
  on,
  onChange,
  label,
  sub,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  sub?: string;
}) {
  return (
    <label className="flex items-center gap-2 text-sm cursor-pointer select-none">
      <input
        type="checkbox"
        checked={on}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 accent-zinc-900"
      />
      <span>{label}</span>
      {sub && <span className="text-xs text-muted-foreground font-mono">{sub}</span>}
    </label>
  );
}

function HostsCard({
  universe,
  selected,
  groups,
  limit,
  setLimit,
  toggleHost,
  preview,
  loading,
}: {
  universe: string[];
  selected: Set<string>;
  groups: string[];
  limit: string;
  setLimit: (v: string) => void;
  toggleHost: (host: string, on: boolean) => void;
  preview: Preview | null;
  loading: boolean;
}) {
  const count = preview?.hosts.hosts.length ?? 0;
  return (
    <Card className="p-4">
      <div className="flex justify-between items-center gap-2">
        <div className="font-medium">
          Hosts{" "}
          <span className="text-muted-foreground font-normal">
            {selected.size} of {universe.length}
          </span>
        </div>
        <div className="flex flex-wrap gap-1 justify-end">
          {groups.map((g) => (
            <button
              key={g}
              type="button"
              onClick={() => setLimit(g)}
              className="text-xs rounded border border-border px-2 py-0.5 hover:bg-muted"
            >
              @{g}
            </button>
          ))}
          <button type="button" onClick={() => setLimit("")} className="text-xs rounded px-2 py-0.5 hover:bg-muted">
            all
          </button>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2">
        {universe.map((h) => (
          <label
            key={h}
            className={cn(
              "flex items-center gap-2 rounded-md border px-2 py-1.5 text-sm cursor-pointer",
              selected.has(h) ? "border-zinc-900" : "border-border text-muted-foreground",
            )}
          >
            <input
              type="checkbox"
              className="accent-zinc-900"
              checked={selected.has(h)}
              onChange={(e) => toggleHost(h, e.target.checked)}
            />
            {h}
          </label>
        ))}
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">Limit</span>
        <input
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
          placeholder="every host the Playbook targets (patterns, & and ! allowed)"
          className="flex-1 font-mono text-xs rounded-md border border-border bg-card px-2 py-1"
        />
      </label>
      <div className="mt-2 text-xs text-muted-foreground">
        {loading ? (
          "Resolving hosts…"
        ) : preview ? (
          <>
            Exact: the Run touches{" "}
            <b>
              {count} host{count === 1 ? "" : "s"}
            </b>
            {preview.hosts.plays.length > 1 &&
              ` (${preview.hosts.plays.map((p) => `${p.name}: ${p.hosts.length}`).join(", ")})`}
          </>
        ) : null}
      </div>
      {preview && preview.hosts.unmatched.length > 0 && (
        <div className="mt-2 text-xs text-red-700">
          No host or group matches: <span className="font-mono">{preview.hosts.unmatched.join(", ")}</span>
        </div>
      )}
      {preview && count === 0 && <div className="mt-2 text-xs text-red-700">This limit leaves no hosts to run on.</div>}
    </Card>
  );
}

function TaskPreview({
  preview,
  stale,
  expand,
  setExpand,
}: {
  preview: Preview | null;
  stale: boolean;
  expand: boolean;
  setExpand: (v: boolean) => void;
}) {
  const hostsByPlay = preview?.hosts.plays ?? [];
  return (
    <div className={cn("space-y-3", stale && "opacity-60")}>
      <div className="flex items-center justify-between">
        <div className="text-xs text-muted-foreground">
          <b>Tasks that may run</b> · ignores <span className="font-mono">when:</span> and the host limit
        </div>
        <CheckBox on={expand} onChange={setExpand} label={<span className="text-xs">Expand includes one level</span>} />
      </div>
      {!preview && <div className="text-sm text-muted-foreground">Loading…</div>}
      {preview?.tasks.plays.map((play, i) => {
        const hosts = hostsByPlay[i]?.hosts ?? [];
        return (
          <div key={`${i}-${play.name}`} className={cn(!hosts.length && "opacity-40")}>
            <div className="text-xs font-semibold">
              PLAY {play.name}{" "}
              <span className="font-mono font-normal text-muted-foreground">hosts: {play.pattern}</span>{" "}
              {!hosts.length && <Badge>no selected hosts</Badge>}
            </div>
            <ol className="mt-1 border-l border-border ml-1">
              {play.tasks.map((task, j) => (
                <TaskLine key={j} task={task} expand={expand} />
              ))}
              {play.tasks.length === 0 && <li className="pl-3 py-0.5 text-sm text-muted-foreground">no tasks</li>}
            </ol>
          </div>
        );
      })}
    </div>
  );
}

function TaskLine({ task, expand }: { task: PreviewTask; expand: boolean }) {
  const inc = task.include;
  return (
    <li className={cn("pl-3 py-0.5 text-sm", !task.selected && "line-through text-zinc-300")}>
      <span className="font-mono text-[13px]">{task.name}</span>
      {task.tags.map((t) => (
        <span key={t} className="ml-1 text-[10px] text-muted-foreground">
          #{t}
        </span>
      ))}
      {inc && (
        <>
          <span className="ml-2">
            <Badge tone="violet">
              expands at run time
              {inc.task_count != null && ` · ${inc.task_count} task${inc.task_count === 1 ? "" : "s"}`}
            </Badge>
          </span>
          {!inc.expandable && inc.reason && (
            <span className="ml-1 text-[10px] text-muted-foreground">({inc.reason})</span>
          )}
          {expand && inc.tasks && (
            <ol className="ml-3 border-l border-dashed border-fuchsia-300">
              {inc.tasks.map((child, k) => (
                <li
                  key={k}
                  className={cn(
                    "pl-3 font-mono text-[12px] text-fuchsia-800",
                    !child.selected && "line-through opacity-50",
                  )}
                >
                  {child.name}
                  {child.include && (
                    <span className="ml-2">
                      <Badge tone="violet">expands at run time</Badge>
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
    </li>
  );
}

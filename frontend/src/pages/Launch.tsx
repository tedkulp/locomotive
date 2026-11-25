import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type PlaybookList } from "@/lib/api";
import { shortSha } from "@/lib/format";
import { cn } from "@/lib/utils";

export function LaunchPage() {
  const navigate = useNavigate();
  const [listing, setListing] = useState<PlaybookList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playbook, setPlaybook] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // Read from the checkout every time the page opens: Playbooks are never frozen.
  useEffect(() => {
    api.playbooks().then(
      (l) => {
        setListing(l);
        setPlaybook((current) => current ?? l.playbooks[0]?.name ?? null);
      },
      (e: Error) => setError(e.message),
    );
  }, []);

  const start = async () => {
    if (!playbook) return;
    setStarting(true);
    setError(null);
    try {
      const run = await api.startRun(playbook);
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError((e as Error).message);
      setStarting(false);
    }
  };

  const command = playbook ? `ansible-playbook playbooks/${playbook} --check` : "";

  return (
    <div className="min-h-full flex flex-col">
      <div className="p-6 max-w-5xl w-full flex-1">
        <h1 className="text-xl font-semibold">Launch a Run</h1>

        <div className="mt-4 rounded-lg border-2 border-border bg-card p-4">
          <div className="font-semibold">Running a clean export of {listing?.branch ?? "main"}</div>
          <div className="text-sm text-muted-foreground">
            {listing ? (
              <>
                Fetched from the remote at <span className="font-mono">{shortSha(listing.sha)}</span>; the
                Run fetches again before it starts.
              </>
            ) : (
              "Fetching from the remote…"
            )}
          </div>
        </div>

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
                onClick={() => setPlaybook(p.name)}
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
          <div className="space-y-4">
            <Card className="p-4">
              <div className="font-medium">Hosts</div>
              <div className="mt-1 text-sm text-muted-foreground">
                All hosts the Playbook targets. Choosing hosts comes later.
              </div>
            </Card>
            <Card className="p-4 space-y-2">
              <div className="font-medium">Options</div>
              <div className="flex items-center gap-2 text-sm">
                <Badge>check mode</Badge>
                <span className="text-muted-foreground">Runs with --check; nothing is changed on the hosts.</span>
              </div>
            </Card>
          </div>
        </div>
      </div>

      <div className="sticky bottom-0 border-t border-border bg-background/90 backdrop-blur px-6 py-3 flex items-center justify-end gap-3">
        <span className="font-mono text-xs text-muted-foreground truncate">{command}</span>
        <Button onClick={start} disabled={!playbook || starting}>
          {starting ? "Starting…" : "Start check Run"}
        </Button>
      </div>
    </div>
  );
}

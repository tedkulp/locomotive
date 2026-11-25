"""Stub ``ansible-runner run``: writes scripted job_events, stdout, rc and status like the real one.

Scenario key ``runner``: {"events": [...], "rc": 0, "status": "successful", "gate_after": N, "gate": "gate", "ignore_sigint": false}
With ``gate_after`` it blocks after writing N events until the file named by ``gate`` (default ``gate``) exists.
``runner_by_limit`` maps a Run's ``--limit`` to a ``runner`` scenario used instead, so Runs can differ.

Like the real pair it stands in for:
- SIGINT is ansible-runner's cancel: it kills Ansible and exits 254 with status ``canceled``
  (``ignore_sigint`` makes it a runner that hangs instead, so only SIGKILL ends it);
- when locomotive's ``locomotive_stop`` callback plugin is on ``ANSIBLE_CALLBACK_PLUGINS`` and the file
  named by ``LOCOMOTIVE_STOP_FILE`` exists as a task starts, Ansible ends with "User interrupted
  execution" (exit 99) before that task runs on any host. No stats are written in either case.
"""
import argparse
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())

parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("private_data_dir")
parser.add_argument("-p", "--playbook")
parser.add_argument("--cmdline", default="")
parser.add_argument("--artifact-dir")
parser.add_argument("-i", "--ident", default="stub")
parser.add_argument("--project-dir")
parser.add_argument("-q", "--quiet", action="store_true")
args = parser.parse_args()

project = Path(args.project_dir or Path(args.private_data_dir) / "project")
listing = sorted(str(p.relative_to(project)) for p in project.rglob("*") if p.is_file())
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({
        "tool": "ansible-runner",
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "playbook": args.playbook,
        "cmdline": args.cmdline,
        "private_data_dir": args.private_data_dir,
        "project_files": listing,
        "env": {k: os.environ.get(k) for k in ("ANSIBLE_COLLECTIONS_PATH",)},
    }) + "\n")

def _limit(cmdline):
    import shlex
    words = shlex.split(cmdline)
    return words[words.index("--limit") + 1] if "--limit" in words else ""


conf = scenario.get("runner_by_limit", {}).get(_limit(args.cmdline), scenario.get("runner", {}))


def record_signal(signum, _frame):
    with open(ROOT / "calls.jsonl", "a") as f:
        f.write(json.dumps({"tool": "signal", "signal": signal.Signals(signum).name}) + "\n")
    if conf.get("ignore_sigint"):
        return
    finish(254, "canceled")


def finish(rc, status):
    (art / "rc").write_text(str(rc))
    (art / "status").write_text(status)
    sys.exit(rc)


def stop_requested():
    plugin_dirs = [d for d in os.environ.get("ANSIBLE_CALLBACK_PLUGINS", "").split(os.pathsep) if d]
    loaded = any((Path(d) / "locomotive_stop.py").is_file() for d in plugin_dirs)
    stop_file = os.environ.get("LOCOMOTIVE_STOP_FILE")
    return loaded and bool(stop_file) and Path(stop_file).exists()

events = conf.get("events") or [
    {"event": "playbook_on_start", "stdout": ""},
    {"event": "playbook_on_play_start", "stdout": "\nPLAY [servers] *****"},
    {"event": "playbook_on_task_start", "stdout": "\nTASK [ping] *****"},
    {"event": "runner_on_ok", "stdout": "ok: [alpha]", "event_data": {"host": "alpha"}},
    {"event": "playbook_on_stats", "stdout": "\nPLAY RECAP *****\nalpha : ok=1 changed=0 unreachable=0 failed=0"},
]
art = Path(args.artifact_dir or Path(args.private_data_dir) / "artifacts") / args.ident
(art / "job_events").mkdir(parents=True, exist_ok=True)
signal.signal(signal.SIGINT, record_signal)
stdout = open(art / "stdout", "w")
line = 0
for counter, ev in enumerate(events, start=1):
    if conf.get("gate_after") == counter - 1:
        while not (ROOT / conf.get("gate", "gate")).exists():
            time.sleep(0.02)
    text = ev.get("stdout", "")
    n = len(text.splitlines()) if text else 0
    body = {
        "uuid": str(uuid.uuid4()),
        "counter": counter,
        "event": ev["event"],
        "event_data": ev.get("event_data", {}),
        "stdout": text,
        "start_line": line,
        "end_line": line + n,
    }
    line += n
    tmp = art / "job_events" / f"{counter}-{body['uuid']}.json.tmp"
    tmp.write_text(json.dumps(body))
    tmp.rename(tmp.with_suffix(""))
    if text:
        stdout.write(text + "\n")
        stdout.flush()
    if ev["event"] in ("playbook_on_task_start", "playbook_on_handler_task_start") and stop_requested():
        print("ERROR! User interrupted execution", file=sys.stderr)
        finish(99, "failed")
    time.sleep(conf.get("delay", 0))
rc = conf.get("rc", 0)
finish(rc, conf.get("status", "successful" if rc == 0 else "failed"))

"""Stub ``ansible-runner run``: writes scripted job_events, stdout, rc and status like the real one.

Scenario key ``runner``: {"events": [...], "rc": 0, "status": "successful", "gate_after": N}
With ``gate_after`` it blocks after writing N events until the ``gate`` file exists.
"""
import argparse
import json
import os
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

conf = scenario.get("runner", {})
events = conf.get("events") or [
    {"event": "playbook_on_start", "stdout": ""},
    {"event": "playbook_on_play_start", "stdout": "\nPLAY [servers] *****"},
    {"event": "playbook_on_task_start", "stdout": "\nTASK [ping] *****"},
    {"event": "runner_on_ok", "stdout": "ok: [alpha]", "event_data": {"host": "alpha"}},
    {"event": "playbook_on_stats", "stdout": "\nPLAY RECAP *****\nalpha : ok=1 changed=0 unreachable=0 failed=0"},
]
art = Path(args.artifact_dir or Path(args.private_data_dir) / "artifacts") / args.ident
(art / "job_events").mkdir(parents=True, exist_ok=True)
stdout = open(art / "stdout", "w")
line = 0
for counter, ev in enumerate(events, start=1):
    if conf.get("gate_after") == counter - 1:
        while not (ROOT / "gate").exists():
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
    time.sleep(conf.get("delay", 0))
rc = conf.get("rc", 0)
(art / "rc").write_text(str(rc))
(art / "status").write_text(conf.get("status", "successful" if rc == 0 else "failed"))
sys.exit(rc)

"""Stub for the Python interpreter of the checkout's ansible-core, as used to run the preview helper.

``python -I <helper> < request.json``: records the request and answers with scenario key ``preview``
(the helper's JSON reply; ``previews`` maps a request's limit, "" for none, to a reply used instead),
``preview_rc`` / ``preview_stderr`` to fail, ``preview_delay`` to be slow.
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())
request = json.loads(sys.stdin.read() or "{}")
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({
        "tool": "ansible-python",
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "request": request,
        "env": {k: os.environ.get(k) for k in ("ANSIBLE_COLLECTIONS_PATH",)},
    }) + "\n")

time.sleep(scenario.get("preview_delay", 0))
if scenario.get("preview_rc"):
    print(scenario.get("preview_stderr", "Traceback (most recent call last): boom"), file=sys.stderr)
    sys.exit(scenario["preview_rc"])

hosts = ["alpha", "beta"]
default = {
    "ok": True,
    "plays": [{"name": "Servers", "pattern": "servers", "hosts": hosts, "all_hosts": hosts,
               "tags": [], "tasks": [{"name": "ping", "role": None, "action": "ping", "tags": [],
                                      "selected": True, "include": None}]}],
    "hosts": hosts,
    "all_hosts": hosts,
    "unmatched": [],
    "tags": [],
    "groups": [{"name": "servers", "children": [], "hosts": hosts}],
}
reply = scenario.get("previews", {}).get(request.get("limit") or "", scenario.get("preview", default))
print(json.dumps(reply))

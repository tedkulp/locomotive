"""Stub ``ansible-galaxy collection install -r FILE -p DIR``."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())
argv = sys.argv[1:]
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({"tool": "ansible-galaxy", "argv": argv, "cwd": os.getcwd()}) + "\n")

rc = scenario.get("galaxy_rc", 0)
if rc:
    print(scenario.get("galaxy_output", "ERROR! Failed to resolve the requested dependencies map."), file=sys.stderr)
    sys.exit(rc)
target = Path(argv[argv.index("-p") + 1])
(target / "ansible_collections" / "community" / "general").mkdir(parents=True, exist_ok=True)
print("community.general was installed successfully")

"""Stub ``ansible``: only ``--version``."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({"tool": "ansible", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
version = scenario.get("ansible_core_version", "2.22.0")
print(f"ansible [core {version}]\n  config file = None\n  python version = 3.13.0")

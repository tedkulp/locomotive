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
python = ROOT / "bin" / "ansible-python"
print(
    f"ansible [core {version}]\n  config file = None\n"
    f"  python version = 3.13.0 (main, Jan 1 2026, 00:00:00) [GCC 14] ({python})\n"
    "  jinja version = 3.1.6"
)

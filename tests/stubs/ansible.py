"""Stub ``ansible``: only ``--version``.

Reports scenario key ``ansible_core_version``; with ``version_from_mise_toml`` it reports the
``ansible-core`` pin in the ``mise.toml`` of the directory it runs in, as mise would provide it.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({"tool": "ansible", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
version = scenario.get("ansible_core_version", "2.22.0")
if scenario.get("version_from_mise_toml"):
    import re
    pin = re.search(r'ansible-core"?\s*=\s*"([^"]+)"', Path("mise.toml").read_text())
    version = pin.group(1) if pin else version
python = ROOT / "bin" / "ansible-python"
print(
    f"ansible [core {version}]\n  config file = None\n"
    f"  python version = 3.13.0 (main, Jan 1 2026, 00:00:00) [GCC 14] ({python})\n"
    "  jinja version = 3.1.6"
)

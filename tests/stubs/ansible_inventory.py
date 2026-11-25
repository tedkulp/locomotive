"""Stub ``ansible-inventory``: locomotive must never call it (``--list`` prints decrypted vault values)."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({"tool": "ansible-inventory", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
print(json.dumps({"_meta": {"hostvars": {"alpha": {"vault_secret": "hunter2"}}}}))

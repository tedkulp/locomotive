"""Stub ``mise``: ``install`` follows the scenario; ``exec [-C dir] -- cmd`` runs cmd with the stubs on PATH."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
scenario = json.loads((ROOT / "scenario.json").read_text())
argv = sys.argv[1:]
with open(ROOT / "calls.jsonl", "a") as f:
    f.write(json.dumps({"tool": "mise", "argv": argv, "cwd": os.getcwd()}) + "\n")

if argv[:1] == ["install"]:
    print(scenario.get("mise_install_output", "mise all tools are installed"))
    sys.exit(scenario.get("mise_install_rc", 0))

if argv[:1] == ["exec"]:
    rest = argv[1:]
    cwd = None
    while rest and rest[0] != "--":
        if rest[0] in ("-C", "--cd"):
            cwd = rest[1]
            rest = rest[2:]
        else:
            rest = rest[1:]
    cmd = rest[1:]
    if cmd[0] in scenario.get("missing_tools", []):
        print(f'mise ERROR "{cmd[0]}" couldn\'t exec process: No such file or directory', file=sys.stderr)
        print("mise ERROR Version: 2026.11.5 linux-x64", file=sys.stderr)
        print("mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information", file=sys.stderr)
        sys.exit(1)
    if cwd:
        os.chdir(cwd)
    env = {**os.environ, "PATH": f"{ROOT / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    try:
        os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError:
        print(f"mise ERROR {cmd[0]}: command not found", file=sys.stderr)
        sys.exit(127)

print(f"stub mise: unsupported {argv}", file=sys.stderr)
sys.exit(2)

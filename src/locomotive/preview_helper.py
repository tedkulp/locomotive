"""locomotive's preview helper. NEVER imported by locomotive itself.

It runs as a subprocess with the Python interpreter of the checkout's own ansible-core (``python -I
preview_helper.py``, cwd = the checkout), reads one JSON request on stdin and writes one JSON reply on
stdout. It uses ansible-core's own loader the way ``ansible-playbook --list-hosts/--list-tasks`` does
(PlaybookCLI argument parsing, InventoryManager, Playbook.load), so limits, tags and config mean exactly
what they mean to a Run.

Request: {"playbook": "playbooks/x.yml", "limit": str|null, "tags": [str], "extra_vars": {},
          "expand_includes": bool}
Reply:   {"ok": true, "plays": [...], "hosts": [...], "all_hosts": [...], "unmatched": [...],
          "tags": [...], "groups": [...]}  or  {"ok": false, "error": str}

What it emits is names only: groups, hosts, play/task names (raw, never templated), patterns, tags.
It never loads or emits host vars, and never calls ``ansible-inventory`` (whose ``--list`` prints
decrypted vault values). Vault-encrypted files it must read to parse a Playbook (e.g. role vars) are
decrypted in this process only and never written out.

It relies on ansible-core internals; seam 2 (tests/test_smoke_real_ansible.py) guards it per pin.
"""

from __future__ import annotations

import json
import os
import sys

SPECIAL_TAGS = {"all", "always", "never", "tagged", "untagged"}
MAX_ERROR = 2000


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    # Only our JSON goes to the real stdout; anything Ansible displays goes to stderr.
    reply_out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    try:
        reply = preview(request)
    except BaseException as exc:  # noqa: BLE001 - every failure becomes a reply
        reply = {"ok": False, "error": _error(exc)}
    reply_out.write(json.dumps(reply))
    reply_out.flush()
    return 0


def _error(exc: BaseException) -> str:
    # ``message`` (not str()) leaves out the source-file excerpt, which could be decrypted content.
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    text = message if type(exc).__name__.startswith("Ansible") else f"{type(exc).__name__}: {message}"
    return ("ERROR! " + text)[:MAX_ERROR]


def preview(request: dict) -> dict:
    from ansible import constants as C
    from ansible import context
    from ansible.cli import CLI
    from ansible.cli.playbook import PlaybookCLI
    from ansible.playbook import Playbook
    from ansible.playbook.block import Block
    from ansible.plugins.loader import add_all_plugin_dirs
    from ansible.utils.collection_loader import AnsibleCollectionConfig

    try:
        from ansible._internal._templating._engine import TemplateEngine as Templar
    except ImportError:  # older ansible-core
        from ansible.template import Templar  # type: ignore[no-redef]

    playbook = request["playbook"]
    limit = request.get("limit") or None
    tags = list(request.get("tags") or [])
    extra_vars = request.get("extra_vars") or {}
    expand = bool(request.get("expand_includes"))

    argv = ["ansible-playbook", "--list-hosts", "--list-tasks"]
    if limit:
        argv += ["--limit", limit]
    if tags:
        argv += ["--tags", ",".join(tags)]
    if extra_vars:
        argv += ["--extra-vars", json.dumps(extra_vars)]
    argv.append(playbook)

    cli = PlaybookCLI(argv)
    CLI.run(cli)  # parse args into context.CLIARGS and initialise the plugin loader, like the CLI does

    if not os.path.isfile(playbook):
        raise FileNotFoundError(f"the playbook: {playbook} could not be found")
    b_playbook_dir = os.path.dirname(os.path.abspath(playbook)).encode()
    add_all_plugin_dirs(b_playbook_dir)
    AnsibleCollectionConfig.playbook_paths = [b_playbook_dir]

    loader, inventory, variable_manager = cli._play_prereqs()
    only_tags = context.CLIARGS["tags"]
    skip_tags = context.CLIARGS["skip_tags"]

    # --limit, exactly as ansible-playbook applies it; patterns that match nothing are reported.
    inventory.subset(limit)
    unmatched = []
    for pattern in inventory._subset or []:
        if pattern and pattern.lstrip("&!") not in inventory._inventory.hosts and not inventory._match_one_pattern(pattern):
            unmatched.append(pattern.lstrip("&!"))

    def walk_blocks(blocks, play, play_tags, all_vars, depth):
        out = []
        for block in blocks:
            for task in list(block.block) + list(block.rescue) + list(block.always):
                if isinstance(task, Block):
                    out += walk_blocks([task], play, play_tags, all_vars, depth)
                    continue
                if task.action in C._ACTION_META and task.implicit:
                    continue
                out.append(describe(task, play, play_tags, all_vars, depth))
        return out

    def describe(task, play, play_tags, all_vars, depth):
        try:
            selected = bool(task.evaluate_tags(only_tags, skip_tags, all_vars))
        except Exception:  # noqa: BLE001 - templated tags: say it may run
            selected = True
        info = None
        if task.action in C._ACTION_INCLUDE_TASKS:
            info = include_tasks(task, play, play_tags, all_vars, depth)
        elif task.action in C._ACTION_INCLUDE_ROLE:
            info = include_role(task, play, play_tags, all_vars, depth)
        return {
            "name": str(task.get_name()) if task.name else str(task.action),
            "role": str(task._role.get_name()) if task._role else None,
            "action": str(task.action),
            "tags": sorted({str(t) for t in play_tags} | {str(t) for t in task.tags}),
            "selected": selected,
            "include": info,
        }

    def children_of(load, target, depth, play, play_tags, all_vars):
        info = {"target": target, "expandable": False, "reason": None, "task_count": None, "tasks": None}
        if "{{" in target:
            info["reason"] = "templated; resolved at run time"
            return info
        if depth > 0:  # one level only
            info["reason"] = "nested include"
            return info
        try:
            blocks = load()
        except Exception:  # noqa: BLE001
            info["reason"] = "could not be loaded statically"
            return info
        if blocks is None:
            info["reason"] = "file not found"
            return info
        children = walk_blocks(blocks, play, play_tags, all_vars, depth + 1)
        info.update(expandable=True, task_count=len(children), tasks=children if expand else None)
        return info

    def include_tasks(task, play, play_tags, all_vars, depth):
        from ansible.playbook.helpers import load_list_of_blocks

        target = str(task.args.get("_raw_params") or task.args.get("file") or "")

        def load():
            if task._role:
                path = loader.path_dwim_relative(task._role._role_path, "tasks", target)
            else:
                path = loader.path_dwim_relative_stack(task.get_search_path(), "tasks", target)
            if not path or not os.path.isfile(path):
                return None
            data = loader.load_from_file(path) or []
            return load_list_of_blocks(
                data, play=play, task_include=task, role=task._role,
                variable_manager=variable_manager, loader=loader,
            )

        return {"kind": "include_tasks", **children_of(load, target, depth, play, play_tags, all_vars)}

    def include_role(task, play, play_tags, all_vars, depth):
        name = str(task._role_name or "")
        tasks_from = task._from_files.get("tasks")
        target = f"{name} : {tasks_from}" if tasks_from else name

        def load():
            blocks, _handlers = task.copy().get_block_list(
                play=play, variable_manager=variable_manager, loader=loader
            )
            return blocks

        return {"kind": "include_role", **children_of(load, target, depth, play, play_tags, all_vars)}

    pb = Playbook.load(playbook, variable_manager=variable_manager, loader=loader)
    plays_out = []
    all_tags: set[str] = set()
    for play in pb.get_plays():
        loader.set_basedir(play._included_path if play._included_path is not None else pb._basedir)
        inventory.remove_restriction()

        raw_name = str(play.get_name())
        raw_pattern = play.hosts if isinstance(play.hosts, str) else ",".join(str(h) for h in play.hosts or [])

        # As PlaybookExecutor does for listings: prompts get their defaults, then play-level templating.
        for var in play.vars_prompt or []:
            if var["name"] not in variable_manager.extra_vars:
                play.vars[var["name"]] = var.get("default")
        all_vars = variable_manager.get_vars(play=play)
        play.post_validate(Templar(loader=loader, variables=all_vars))

        hosts = [h.name for h in inventory.get_hosts(play.hosts)]
        universe = [h.name for h in inventory.get_hosts(play.hosts, ignore_limits=True)]
        play_tags = list(play.tags or [])
        all_vars = variable_manager.get_vars(play=play)
        tasks = walk_blocks(play.compile(), play, play_tags, all_vars, 0)
        all_tags.update(play_tags)
        for t in tasks:
            all_tags.update(t["tags"])
        plays_out.append({
            "name": raw_name,
            "pattern": str(raw_pattern),
            "hosts": hosts,
            "all_hosts": universe,
            "tags": sorted(str(t) for t in play_tags),
            "tasks": tasks,
        })

    def union(key):
        seen = {}
        for p in plays_out:
            for h in p[key]:
                seen.setdefault(h, None)
        return list(seen)

    groups = [
        {
            "name": str(name),
            "children": [str(c.name) for c in group.child_groups],
            "hosts": [str(h.name) for h in group.get_hosts()],
        }
        for name, group in inventory.groups.items()
    ]
    return {
        "ok": True,
        "plays": plays_out,
        "hosts": union("hosts"),
        "all_hosts": union("all_hosts"),
        "unmatched": unmatched,
        "tags": sorted(t for t in all_tags if t not in SPECIAL_TAGS),
        "groups": groups,
    }


if __name__ == "__main__":
    sys.exit(main())

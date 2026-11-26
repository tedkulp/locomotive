"""Seam 2: the real preview helper and one real ansible-runner Run, with the pinned ansible-core.

Small on purpose: it guards against ansible-core internals the helper relies on changing on a pin bump.
Everything runs against a fixture repo whose only reachable host is ``localhost`` (connection local);
no other host is ever contacted (``--list-hosts`` and the helper only read files).

Needs ``mise`` on PATH. The pinned runtime is installed by mise on first use (then cached).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import Remote, wait_for_run, wait_until

MISE = shutil.which("mise")
pytestmark = pytest.mark.skipif(MISE is None, reason="seam 2 needs mise to install the pinned ansible-core")

# The pinned runtime: ansible-core exactly, with ansible-runner in the same env (as the repo's mise.toml).
PINNED_MISE_TOML = (
    '[tools]\n"pipx:ansible-core" = { version = "2.21.0", '
    'uvx_args = "--python 3.13 --with-executables-from ansible-runner" }\n'
)
SECRET = "hunter2-vaulted-value"
ROLE_SECRET = "role-vars-vaulted-value"
VAULT_PASSWORD = "smoke-test-vault-password"

INVENTORY = """\
[servers]
alpha ansible_host=192.0.2.10
beta ansible_host=192.0.2.11
gamma ansible_host=192.0.2.12

[docker]
beta
gamma

[nfs_client]
gamma

[local]
localhost ansible_connection=local ansible_python_interpreter="{{ ansible_playbook_python }}"
"""

TREE = {
    "ansible.cfg": "[defaults]\ninventory=hosts/inventory\nroles_path=roles\ngathering=explicit\n",
    "hosts/inventory": INVENTORY,
    "mise.toml": PINNED_MISE_TOML,
    "playbooks/site.yml": """\
- name: Configure servers
  hosts: servers
  tags: [base]
  roles:
    - app
    - vaulted

- name: Shared storage on docker hosts
  hosts: docker:&nfs_client
  tasks:
    - name: Mount the share
      ansible.builtin.debug:
        msg: mount
      tags: [nfs]
    - name: Refresh everything
      ansible.builtin.debug:
        msg: refresh
      tags: [never, refresh]
""",
    "playbooks/smoke.yml": """\
- name: Smoke
  hosts: local
  tasks:
    - name: Ping localhost
      ansible.builtin.ping:
    - name: Say hello
      ansible.builtin.debug:
        msg: "hello from {{ inventory_hostname }}"
      tags: [hello]
    - name: Make a change
      ansible.builtin.command: "true"
      changed_when: true
      tags: [change]
""",
    "playbooks/stop.yml": """\
- name: Notify, then wait for the stop
  hosts: local
  tasks:
    - name: Change and notify
      ansible.builtin.command: "true"
      changed_when: true
      notify: Record the handler
    - name: Wait for the stop
      ansible.builtin.wait_for:
        path: "{{ lookup('ansible.builtin.env', 'LOCOMOTIVE_STOP_FILE') }}"
        timeout: 60
    - name: After the stop
      ansible.builtin.debug:
        msg: ran-after-the-stop
  handlers:
    - name: Record the handler
      ansible.builtin.debug:
        msg: the handler ran

- name: A later play
  hosts: local
  tasks:
    - name: Later play task
      ansible.builtin.debug:
        msg: ran-in-the-later-play
""",
    "roles/app/tasks/main.yml": """\
- name: Install packages
  ansible.builtin.debug:
    msg: packages
  tags: [packages]
- name: Imported tasks
  ansible.builtin.import_tasks: imported.yml
- name: Extra tasks
  ansible.builtin.include_tasks: extra.yml
- name: Helper install
  ansible.builtin.include_role:
    name: helper
    tasks_from: install
- name: Per-host hook
  ansible.builtin.include_tasks: "{{ hook_name }}.yml"
""",
    "roles/app/tasks/imported.yml": "- name: Imported one\n  ansible.builtin.debug:\n    msg: one\n",
    "roles/app/tasks/extra.yml": """\
- name: Extra one
  ansible.builtin.debug:
    msg: one
- name: Extra two
  ansible.builtin.debug:
    msg: two
  tags: [extra]
""",
    "roles/helper/tasks/main.yml": "- name: Helper main\n  ansible.builtin.debug:\n    msg: main\n",
    "roles/helper/tasks/install.yml": "- name: Helper install task\n  ansible.builtin.debug:\n    msg: install\n",
    "roles/vaulted/tasks/main.yml": "- name: Use the role secret\n  ansible.builtin.debug:\n    msg: \"{{ role_secret | length }}\"\n",
}


def _mise_env(project: Path) -> dict[str, str]:
    return {**os.environ, "MISE_TRUSTED_CONFIG_PATHS": str(project), "MISE_YES": "1"}


def _vault(project: Path, password_file: Path, content: str, dest: str) -> str:
    path = project / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(
        [MISE, "exec", "-C", str(project), "--", "ansible-vault", "encrypt", str(path)],
        cwd=project, env={**_mise_env(project), "ANSIBLE_VAULT_PASSWORD_FILE": str(password_file)},
        check=True, capture_output=True,
    )
    return path.read_text()


@pytest.fixture
def smoke(remote: Remote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_app):
    password_file = tmp_path / "vault_pass.txt"
    password_file.write_text(VAULT_PASSWORD + "\n")
    monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("ANSIBLE_HOME", str(tmp_path / "ansible-home"))
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(tmp_path / "ansible-tmp"))

    scratch = tmp_path / "scratch"
    for path, content in TREE.items():
        (scratch / path).parent.mkdir(parents=True, exist_ok=True)
        (scratch / path).write_text(content)
    files: dict[str, str | None] = {path: None for path in ("hosts/inventory", "collections/requirements.yml",
                                                            "playbooks/servers.yml", "playbooks/docker.yml")}
    files.update(TREE)
    files["hosts/host_vars/alpha.yml"] = _vault(scratch, password_file, f"vault_secret: {SECRET}\n", "hosts/host_vars/alpha.yml")
    files["roles/vaulted/vars/main.yml"] = _vault(scratch, password_file, f"role_secret: {ROLE_SECRET}\n", "roles/vaulted/vars/main.yml")
    remote.commit(files, message="smoke fixture")
    client = start_app(mise_bin=MISE, hidden_playbooks=["mac.yml", "locomotive.yml"], preview_timeout_seconds=60)
    return client, remote, scratch


def list_hosts(project: Path, limit: str | None) -> list[list[str]]:
    """The oracle: ``ansible-playbook --list-hosts --limit`` (text), hosts per play."""
    cmd = [MISE, "exec", "-C", str(project), "--", "ansible-playbook", "--list-hosts", "playbooks/site.yml"]
    if limit:
        cmd += ["--limit", limit]
    out = subprocess.run(cmd, cwd=project, env=_mise_env(project), capture_output=True, text=True)
    if out.returncode != 0:
        return []
    plays: list[list[str]] = []
    for line in out.stdout.splitlines():
        if re.match(r"\s+hosts \(\d+\):", line):
            plays.append([])
        elif plays and re.match(r"^\s{6}\S", line):
            plays[-1].append(line.strip())
    return [sorted(p) for p in plays]


@pytest.mark.parametrize("limit", [None, "alpha", "docker", "servers:!beta", "servers:&docker", "alpha,nosuch", "nosuch"])
def test_host_preview_matches_list_hosts_limit(smoke, limit):
    client, remote, scratch = smoke

    resp = client.post("/api/preview", json={"playbook": "site.yml", "limit": limit})

    assert resp.status_code == 200, resp.text
    hosts = resp.json()["hosts"]
    expected = list_hosts(scratch, limit)
    if expected:
        assert [sorted(p["hosts"]) for p in hosts["plays"]] == expected
    else:  # ansible-playbook refuses a limit that leaves no hosts at all
        assert hosts["hosts"] == []
    assert sorted(hosts["all_hosts"]) == ["alpha", "beta", "gamma"]
    assert hosts["unmatched"] == (["nosuch"] if limit and "nosuch" in limit else [])


def test_host_preview_known_answers(smoke):
    client, *_ = smoke

    def hosts(limit):
        return client.post("/api/preview", json={"playbook": "site.yml", "limit": limit}).json()["hosts"]

    assert hosts("servers:!beta")["hosts"] == ["alpha", "gamma"]
    assert hosts("servers:&docker")["hosts"] == ["beta", "gamma"]
    assert [p["hosts"] for p in hosts("servers:&docker")["plays"]] == [["beta", "gamma"], ["gamma"]]
    assert hosts("alpha,nosuch") | {"plays": None} == {
        "hosts": ["alpha"], "all_hosts": ["alpha", "beta", "gamma"], "unmatched": ["nosuch"], "plays": None,
    }


def test_task_preview_lists_tasks_that_may_run_with_includes_collapsed_and_expandable(smoke):
    client, *_ = smoke

    collapsed = client.post("/api/preview", json={"playbook": "site.yml"}).json()
    expanded = client.post("/api/preview", json={"playbook": "site.yml", "expand_includes": True}).json()

    assert collapsed["tasks"]["label"] == "tasks that may run"
    play = collapsed["tasks"]["plays"][0]
    by_name = {t["name"]: t for t in play["tasks"]}
    assert list(by_name) == [
        "app : Install packages", "app : Imported one", "app : Extra tasks", "Helper install",
        "app : Per-host hook", "vaulted : Use the role secret",
    ]
    assert by_name["app : Install packages"]["tags"] == ["base", "packages"]
    assert by_name["app : Imported one"]["include"] is None  # imports are expanded statically
    extra = by_name["app : Extra tasks"]["include"]
    assert extra | {"tasks": None} == {
        "kind": "include_tasks", "target": "extra.yml", "expandable": True, "reason": None,
        "task_count": 2, "tasks": None,
    }
    assert by_name["Helper install"]["include"]["kind"] == "include_role"
    assert by_name["Helper install"]["include"]["target"] == "helper : install"
    hook = by_name["app : Per-host hook"]["include"]
    assert hook["expandable"] is False and hook["target"] == "{{ hook_name }}.yml"

    exp = {t["name"]: t for t in expanded["tasks"]["plays"][0]["tasks"]}
    assert [c["name"] for c in exp["app : Extra tasks"]["include"]["tasks"]] == ["app : Extra one", "app : Extra two"]
    assert [c["name"] for c in exp["Helper install"]["include"]["tasks"]] == ["helper : Helper install task"]
    assert exp["app : Per-host hook"]["include"]["tasks"] is None


def test_tags_are_listed_and_filter_the_task_preview(smoke):
    client, *_ = smoke

    body = client.post("/api/preview", json={"playbook": "site.yml", "tags": ["refresh"]}).json()

    assert body["tags"] == ["base", "nfs", "packages", "refresh"]
    storage = {t["name"]: t["selected"] for t in body["tasks"]["plays"][1]["tasks"]}
    assert storage == {"Mount the share": False, "Refresh everything": True}
    default = client.post("/api/preview", json={"playbook": "site.yml"}).json()
    storage = {t["name"]: t["selected"] for t in default["tasks"]["plays"][1]["tasks"]}
    assert storage == {"Mount the share": True, "Refresh everything": False}  # never-tagged


def test_inventory_graph_has_groups_and_hosts_only(smoke):
    client, *_ = smoke

    groups = {g["name"]: g for g in client.post("/api/preview", json={"playbook": "site.yml"}).json()["inventory"]["groups"]}

    assert groups["docker"] == {"name": "docker", "children": [], "hosts": ["beta", "gamma"]}
    assert "servers" in groups["all"]["children"]


def test_no_preview_response_contains_vault_decrypted_values(smoke):
    client, *_ = smoke

    texts = [
        client.post("/api/preview", json={"playbook": "site.yml", "expand_includes": True, "limit": limit}).text
        for limit in (None, "alpha")
    ]

    for text in texts:
        assert SECRET not in text
        assert ROLE_SECRET not in text
        assert "192.0.2." not in text  # no host vars at all


def test_previews_answer_within_10s(smoke):
    client, *_ = smoke
    client.post("/api/preview", json={"playbook": "site.yml"}).raise_for_status()  # prepares the runtime

    started = time.monotonic()
    client.post("/api/preview", json={"playbook": "site.yml", "limit": "docker", "tags": ["nfs"]}).raise_for_status()

    assert time.monotonic() - started < 10


def test_a_real_run_against_localhost(smoke):
    client, *_ = smoke

    resp = client.post("/api/runs", json={
        "playbook": "smoke.yml", "check": False, "diff": True, "verbosity": 1,
        "tags": ["hello", "change"], "extra_vars": {"greeting": "hi"},
    })
    run = wait_for_run(client, resp.json()["id"], timeout=120)

    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["hosts"] == ["localhost"]
    assert run["ansible_core_version"] == "2.21.0"
    log = "\n".join(line["text"] for line in client.get(f"/api/runs/{run['id']}/log").json()["lines"])
    assert "hello from localhost" in log
    assert "TASK [Ping localhost]" not in log  # not tagged
    assert "changed=1" in log
    assert SECRET not in log


def test_stop_after_this_task_still_runs_the_handlers_already_notified(smoke):
    client, *_ = smoke
    run_id = client.post("/api/runs", json={"playbook": "stop.yml", "check": False}).json()["id"]

    def waiting():
        rows = {t["name"]: t["results"] for t in client.get(f"/api/runs/{run_id}/grid").json()["tasks"]}
        return rows.get("Wait for the stop", {}).get("localhost") == "running"

    wait_until(waiting, timeout=120)
    client.post(f"/api/runs/{run_id}/stop").raise_for_status()
    run = wait_for_run(client, run_id, timeout=60)

    assert run["status"] == "canceled", run["failure_reason"]
    assert run["failure_reason"] == "Stopped after a task"
    rows = {t["name"]: t["results"] for t in client.get(f"/api/runs/{run_id}/grid").json()["tasks"]}
    assert rows["Wait for the stop"] == {"localhost": "ok"}
    assert rows["Record the handler"] == {"localhost": "ok"}
    assert not rows.get("After the stop")
    assert not rows.get("Later play task")
    log = "\n".join(line["text"] for line in client.get(f"/api/runs/{run_id}/log").json()["lines"])
    assert "the handler ran" in log
    assert "ran-after-the-stop" not in log and "ran-in-the-later-play" not in log

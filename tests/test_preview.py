"""Previews: the exact hosts a Run will touch, the tasks that may run, tags and the inventory graph."""

import json

from conftest import wait_for_run, wait_until

PLAYS = [
    {
        "name": "Servers",
        "pattern": "servers",
        "hosts": ["alpha"],
        "all_hosts": ["alpha", "beta"],
        "tags": ["base"],
        "tasks": [
            {"name": "common : Install packages", "role": "common", "action": "ansible.builtin.apt",
             "tags": ["base", "packages"], "selected": True, "include": None},
            {"name": "Emulators", "role": None, "action": "ansible.builtin.include_tasks",
             "tags": ["base"], "selected": True,
             "include": {"kind": "include_tasks", "target": "emulator.yml", "expandable": True,
                         "reason": None, "task_count": 2,
                         "tasks": [{"name": "Download core", "role": None, "action": "get_url",
                                    "tags": [], "selected": True, "include": None},
                                   {"name": "Hook", "role": None, "action": "include_tasks",
                                    "tags": [], "selected": True,
                                    "include": {"kind": "include_tasks", "target": "{{ hook }}",
                                                "expandable": False, "reason": "templated path",
                                                "task_count": None, "tasks": None}}]}},
        ],
    },
    {"name": "Docker", "pattern": "docker", "hosts": [], "all_hosts": ["gamma"], "tags": [], "tasks": []},
]

REPLY = {
    "ok": True,
    "plays": PLAYS,
    "hosts": ["alpha"],
    "all_hosts": ["alpha", "beta", "gamma"],
    "unmatched": ["nosuch"],
    "tags": ["base", "packages"],
    "groups": [
        {"name": "all", "children": ["servers", "docker", "ungrouped"], "hosts": ["alpha", "beta", "gamma"]},
        {"name": "servers", "children": [], "hosts": ["alpha", "beta"]},
        {"name": "docker", "children": [], "hosts": ["gamma"]},
    ],
}


def test_preview_returns_hosts_tasks_tags_inventory_and_the_command(client, stubs, remote):
    stubs.script(preview=REPLY)

    resp = client.post("/api/preview", json={
        "playbook": "servers.yml",
        "limit": "alpha,nosuch",
        "tags": ["base"],
        "check": False,
        "diff": True,
        "verbosity": 1,
        "extra_vars": {"a": 1},
        "expand_includes": True,
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["branch"] == "main"
    assert body["sha"] == remote.sha("main")
    assert body["command"] == (
        "ansible-playbook playbooks/servers.yml --diff --limit alpha,nosuch --tags base -v --extra-vars '{\"a\": 1}'"
    )
    assert body["hosts"] == {
        "hosts": ["alpha"],
        "all_hosts": ["alpha", "beta", "gamma"],
        "unmatched": ["nosuch"],
        "plays": [
            {"name": "Servers", "pattern": "servers", "hosts": ["alpha"], "all_hosts": ["alpha", "beta"]},
            {"name": "Docker", "pattern": "docker", "hosts": [], "all_hosts": ["gamma"]},
        ],
    }
    assert body["tasks"]["label"] == "tasks that may run"
    assert body["tasks"]["plays"][0]["tasks"] == PLAYS[0]["tasks"]
    assert body["tags"] == ["base", "packages"]
    assert body["inventory"]["groups"][1] == {"name": "servers", "children": [], "hosts": ["alpha", "beta"]}
    [call] = stubs.calls("ansible-python")
    assert call["request"] == {
        "playbook": "playbooks/servers.yml",
        "limit": "alpha,nosuch",
        "tags": ["base"],
        "extra_vars": {"a": 1},
        "expand_includes": True,
    }


def test_preview_runs_the_helper_in_the_checkouts_runtime_with_its_collections(client, stubs):
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()

    [call] = stubs.calls("ansible-python")
    assert call["argv"][0] == "-I"
    assert call["argv"][1].endswith("preview_helper.py")
    assert call["env"]["ANSIBLE_COLLECTIONS_PATH"]
    assert [c["argv"][:1] for c in stubs.calls("mise")].count(["install"]) == 1


def test_repeated_previews_of_a_commit_reuse_its_prepared_runtime(client, stubs, remote):
    sha = remote.sha("main")
    for limit in ("alpha", "beta", "alpha,beta"):
        resp = client.post("/api/preview", json={"playbook": "servers.yml", "limit": limit, "sha": sha})
        assert resp.status_code == 200
        assert resp.json()["sha"] == sha

    assert len(stubs.calls("ansible-python")) == 3
    assert [c["argv"][:1] for c in stubs.calls("mise")].count(["install"]) == 1
    assert len(stubs.calls("ansible-galaxy")) == 1


def test_preview_of_a_new_commit_uses_the_new_tree(client, stubs, remote):
    first = client.post("/api/preview", json={"playbook": "servers.yml"}).json()["sha"]
    second_sha = remote.commit({"playbooks/servers.yml": "- hosts: all\n  tasks: []\n"})

    body = client.post("/api/preview", json={"playbook": "servers.yml"}).json()

    assert body["sha"] == second_sha != first
    cwds = {c["cwd"] for c in stubs.calls("ansible-python")}
    assert len(cwds) == 2


def test_preview_of_an_unknown_sha_fetches_the_branch_instead(client, stubs, remote):
    body = client.post("/api/preview", json={"playbook": "servers.yml", "sha": "0" * 40}).json()

    assert body["sha"] == remote.sha("main")


def test_hidden_playbooks_cannot_be_previewed(client, stubs):
    resp = client.post("/api/preview", json={"playbook": "mac.yml"})

    assert resp.status_code == 422
    assert "hidden" in resp.json()["detail"]
    assert stubs.calls("ansible-python") == []


def test_preview_of_a_playbook_missing_from_the_checkout_is_404(client, stubs):
    resp = client.post("/api/preview", json={"playbook": "nope.yml"})

    assert resp.status_code == 404


def test_preview_rejects_options_it_cannot_honour(client):
    for body in (
        {"playbook": "servers.yml", "skip_tags": ["x"]},
        {"playbook": "servers.yml", "limit": "@retry"},
        {"playbook": "servers.yml", "sha": "not-a-sha"},
    ):
        assert client.post("/api/preview", json=body).status_code == 422, body


def test_a_helper_error_is_reported_as_the_preview_error(client, stubs):
    stubs.script(preview={"ok": False, "error": "ERROR! couldn't resolve module/action 'nope.x'"})

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "ERROR! couldn't resolve module/action 'nope.x'"


def test_a_crashing_helper_is_a_502_without_its_output(client, stubs):
    stubs.script(preview_rc=1, preview_stderr="Traceback: vault_secret=hunter2")

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 502
    assert "hunter2" not in resp.text


def test_a_slow_helper_times_out_within_the_preview_budget(start_app, stubs):
    client = start_app(preview_timeout_seconds=1)
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()  # runtime now prepared
    stubs.script(preview_delay=5)

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 504
    assert resp.elapsed.total_seconds() < 3


def test_a_preview_while_the_runtime_is_still_preparing_says_retry_and_keeps_preparing(start_app, stubs):
    client = start_app(preview_timeout_seconds=1)
    stubs.script(mise_install_gate=True)

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "2"
    assert "still preparing" in resp.json()["detail"].lower()
    assert resp.elapsed.total_seconds() < 3

    stubs.release_gate()  # the install finishes in the background; the next ask gets the preview
    wait_until(lambda: client.post("/api/preview", json={"playbook": "servers.yml"}).status_code == 200)
    assert len([c for c in stubs.calls("mise") if c["argv"][:1] == ["install"]]) == 1


def test_previews_only_pass_on_known_fields_never_host_vars(client, stubs):
    leaky = json.loads(json.dumps(REPLY))
    leaky["hostvars"] = {"alpha": {"vault_secret": "hunter2"}}
    leaky["plays"][0]["vars"] = {"vault_secret": "hunter2"}
    leaky["plays"][0]["tasks"][0]["args"] = {"password": "hunter2"}
    leaky["groups"][1]["vars"] = {"vault_secret": "hunter2"}
    stubs.script(preview=leaky)

    resp = client.post("/api/preview", json={"playbook": "servers.yml", "expand_includes": True})

    assert resp.status_code == 200
    assert "hunter2" not in resp.text


def test_ansible_inventory_is_never_used_for_previews_or_runs(client, stubs):
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()
    wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert stubs.calls("ansible-inventory") == []
    assert stubs.calls("ansible-python")

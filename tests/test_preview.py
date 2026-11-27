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

    # A new request (the limit changed), so the helper is asked rather than a remembered reply used.
    resp = client.post("/api/preview", json={"playbook": "servers.yml", "limit": "alpha"})

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


# ---------------------------------------------------------------------- remembered replies


def test_an_identical_preview_of_a_commit_is_answered_without_asking_the_helper_again(client, stubs):
    stubs.script(preview=REPLY)
    request = {"playbook": "servers.yml", "limit": "alpha", "tags": ["base"], "extra_vars": {"a": 1}}

    first = client.post("/api/preview", json=request)
    second = client.post("/api/preview", json=request)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(stubs.calls("ansible-python")) == 1


def test_switching_back_to_a_playbook_reuses_its_remembered_preview(client, stubs, remote):
    sha = remote.sha("main")
    for playbook in ("servers.yml", "docker.yml", "servers.yml"):
        client.post("/api/preview", json={"playbook": playbook, "sha": sha}).raise_for_status()

    assert [c["request"]["playbook"] for c in stubs.calls("ansible-python")] == [
        "playbooks/servers.yml", "playbooks/docker.yml",
    ]


def test_changing_what_the_helper_is_asked_asks_it_again(client, stubs):
    base = {"playbook": "servers.yml"}
    for change in ({}, {"limit": "alpha"}, {"tags": ["base"]}, {"extra_vars": {"a": 1}}, {"expand_includes": True}):
        client.post("/api/preview", json={**base, **change}).raise_for_status()

    assert len(stubs.calls("ansible-python")) == 5


def test_changing_only_check_diff_or_verbosity_reuses_the_reply_with_the_new_command(client, stubs):
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()

    body = client.post("/api/preview", json={"playbook": "servers.yml", "check": True, "diff": True, "verbosity": 2})

    assert body.status_code == 200
    assert body.json()["command"] == "ansible-playbook playbooks/servers.yml --check --diff -vv"
    assert len(stubs.calls("ansible-python")) == 1


def test_the_same_preview_at_a_new_commit_asks_the_helper_again(client, stubs, remote):
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()
    remote.commit({"README.md": "changed\n"})

    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()

    assert len(stubs.calls("ansible-python")) == 2


def test_a_failed_preview_is_not_remembered(client, stubs):
    stubs.script(preview={"ok": False, "error": "ERROR! broken"})
    assert client.post("/api/preview", json={"playbook": "servers.yml"}).status_code == 422
    stubs.script(preview_rc=1)
    assert client.post("/api/preview", json={"playbook": "servers.yml"}).status_code == 502
    stubs.script(preview=REPLY, preview_rc=0)

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 200
    assert resp.json()["tags"] == ["base", "packages"]
    assert len(stubs.calls("ansible-python")) == 3


def test_a_remembered_preview_still_carries_no_host_vars(client, stubs):
    leaky = json.loads(json.dumps(REPLY))
    leaky["hostvars"] = {"alpha": {"vault_secret": "hunter2"}}
    leaky["plays"][0]["vars"] = {"vault_secret": "hunter2"}
    stubs.script(preview=leaky)
    client.post("/api/preview", json={"playbook": "servers.yml"}).raise_for_status()

    resp = client.post("/api/preview", json={"playbook": "servers.yml"})

    assert resp.status_code == 200
    assert "hunter2" not in resp.text
    assert len(stubs.calls("ansible-python")) == 1


def test_a_commit_remembers_only_its_most_recent_previews(client, stubs, remote):
    from locomotive.preview import KEEP_REPLIES

    sha = remote.sha("main")
    ask = lambda limit: client.post(  # noqa: E731
        "/api/preview", json={"playbook": "servers.yml", "sha": sha, "limit": limit}
    ).raise_for_status()
    ask("host0")
    for n in range(1, KEEP_REPLIES + 1):
        ask(f"host{n}")
    ask(f"host{KEEP_REPLIES}")  # the newest is still remembered
    assert len(stubs.calls("ansible-python")) == KEEP_REPLIES + 1

    ask("host0")  # the oldest was dropped to stay within the bound

    assert len(stubs.calls("ansible-python")) == KEEP_REPLIES + 2

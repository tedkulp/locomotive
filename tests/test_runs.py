"""Starting a clean check-mode Run of main against all its hosts."""

import hashlib

from conftest import DEFAULT_TREE, wait_for_run


def test_a_clean_check_run_succeeds_and_records_what_ran(client, remote, stubs, clock):
    stubs.script(ansible_core_version="2.22.0")

    resp = client.post("/api/runs", json={"playbook": "servers.yml"})

    assert resp.status_code == 201
    run = wait_for_run(client, resp.json()["id"])
    requirements = DEFAULT_TREE["collections/requirements.yml"].encode()
    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["playbook"] == "servers.yml"
    assert run["sha"] == remote.sha("main")
    assert run["source"] == "ui"
    assert run["checkout"] == "clean"
    assert run["branch"] == "main"
    assert run["check"] is True
    assert run["requirements_hash"] == hashlib.sha256(requirements).hexdigest()
    assert run["ansible_core_version"] == "2.22.0"
    assert run["created_at"] == "2026-11-25T12:00:00Z"
    assert run["started_at"] == "2026-11-25T12:00:00Z"
    assert run["finished_at"] == "2026-11-25T12:00:00Z"
    assert run["failure_reason"] is None


def test_the_run_executes_ansible_runner_in_check_mode_on_an_export_of_main(client, remote, stubs):
    sha = remote.commit({"roles/new/tasks/main.yml": "- debug: msg=hi\n"})

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["sha"] == sha
    [runner] = stubs.calls("ansible-runner")
    assert runner["playbook"] == "playbooks/servers.yml"
    assert runner["cmdline"] == "--check"
    assert "roles/new/tasks/main.yml" in runner["project_files"]
    assert runner["env"]["ANSIBLE_COLLECTIONS_PATH"].endswith(run["requirements_hash"])
    [install] = [c for c in stubs.calls("mise") if c["argv"][:1] == ["install"]]
    assert install["cwd"] == runner["cwd"]
    [galaxy] = stubs.calls("ansible-galaxy")
    assert galaxy["argv"][:4] == ["collection", "install", "-r", "collections/requirements.yml"]


def test_collections_are_installed_once_per_requirements_hash(client, remote, stubs):
    first = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])
    second = wait_for_run(client, client.post("/api/runs", json={"playbook": "docker.yml"}).json()["id"])
    assert first["status"] == second["status"] == "succeeded"
    assert len(stubs.calls("ansible-galaxy")) == 1

    remote.commit({"collections/requirements.yml": "---\ncollections:\n  - name: ansible.posix\n"})
    third = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert third["requirements_hash"] != first["requirements_hash"]
    assert len(stubs.calls("ansible-galaxy")) == 2
    runner_paths = [c["env"]["ANSIBLE_COLLECTIONS_PATH"] for c in stubs.calls("ansible-runner")]
    assert runner_paths[0] == runner_paths[1] != runner_paths[2]


def test_a_failed_fetch_fails_the_run_with_a_clear_reason(start_app, remote, stubs, tmp_path):
    client = start_app()
    client.get("/api/playbooks").raise_for_status()  # mirror exists, then the remote goes away
    remote.bare.rename(tmp_path / "gone.git")

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"].startswith("Fetch failed")
    assert run["sha"] is None
    assert stubs.calls("ansible-runner") == []


def test_a_failed_collections_install_fails_the_run_with_a_clear_reason(client, stubs):
    stubs.script(galaxy_rc=1, galaxy_output="ERROR! Galaxy is down")

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == "Collections install failed: ansible-galaxy exited 1: ERROR! Galaxy is down"
    assert stubs.calls("ansible-runner") == []


def test_a_failed_collections_install_is_not_cached(client, stubs):
    stubs.script(galaxy_rc=1)
    wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])
    stubs.script(galaxy_rc=0)

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "succeeded"
    assert len(stubs.calls("ansible-galaxy")) == 2


def test_a_failed_runtime_install_fails_the_run_with_a_clear_reason(client, stubs):
    stubs.script(mise_install_rc=1, mise_install_output="mise ERROR failed to install pipx:ansible-core")

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == (
        "Runtime install failed: mise install exited 1: mise ERROR failed to install pipx:ansible-core"
    )
    assert stubs.calls("ansible-runner") == []


def test_a_failing_playbook_fails_the_run(client, stubs):
    stubs.script(runner={
        "events": [
            {"event": "playbook_on_task_start", "stdout": "TASK [ping] ***"},
            {"event": "runner_on_failed", "stdout": "fatal: [alpha]: FAILED!", "event_data": {"host": "alpha"}},
        ],
        "rc": 2,
    })

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["rc"] == 2
    assert run["failure_reason"] == "ansible-playbook failed (exit code 2)"


def test_a_playbook_missing_at_the_resolved_sha_fails_the_run(client, remote):
    remote.commit({"playbooks/docker.yml": None})

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "docker.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"].startswith("Playbook playbooks/docker.yml does not exist at ")


def test_options_locomotive_cannot_honour_are_rejected_at_launch(client, stubs):
    for body in (
        {"playbook": "servers.yml", "skip_tags": ["x"]},
        {"playbook": "servers.yml", "start_at_task": "ping"},
        {"playbook": "servers.yml", "forks": 50},
        {"playbook": "servers.yml", "verbosity": 5},
        {"playbook": "servers.yml", "limit": "@/etc/retry"},
        {"playbook": "servers.yml", "limit": "alpha:!@hosts.txt"},
        {"playbook": "servers.yml", "limit": "alpha\nbeta"},
        {"playbook": "servers.yml", "tags": ["two words"]},
        {"playbook": "servers.yml", "tags": ["a,b"]},
        {"playbook": "servers.yml", "extra_vars": ["not", "a", "mapping"]},
        {"playbook": "servers.yml", "extra_vars": "x=1"},
        {"playbook": "servers.yml", "check": "maybe"},
        {"playbook": "../etc/passwd"},
        {},
    ):
        resp = client.post("/api/runs", json=body)
        assert resp.status_code == 422, body
    assert client.get("/api/runs").json()["runs"] == []


def test_hidden_playbooks_cannot_be_started(client):
    resp = client.post("/api/runs", json={"playbook": "mac.yml"})

    assert resp.status_code == 422
    assert "hidden" in resp.json()["detail"]


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/999").status_code == 404


def test_a_checkout_without_ansible_runner_fails_the_run_with_a_clear_reason(client, stubs):
    stubs.script(missing_tools=["ansible-runner"])

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == (
        "ansible-runner failed to start (exit code 1): "
        "mise ERROR \"ansible-runner\" couldn't exec process: No such file or directory"
    )

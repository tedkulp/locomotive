"""Launching a Run with hosts, tags, check, diff, verbosity and extra vars."""

from conftest import wait_for_run


def preview_for(hosts: list[str], unmatched: list[str] | None = None) -> dict:
    """What the preview helper reports for a limit: one play hitting ``hosts``."""
    return {
        "ok": True,
        "plays": [{
            "name": "Servers",
            "pattern": "servers",
            "hosts": hosts,
            "all_hosts": ["alpha", "beta"],
            "tags": [],
            "tasks": [],
        }],
        "hosts": hosts,
        "all_hosts": ["alpha", "beta"],
        "unmatched": unmatched or [],
        "tags": [],
        "groups": [{"name": "servers", "children": [], "hosts": ["alpha", "beta"]}],
    }


def test_a_real_run_records_and_honours_every_launch_option(client, stubs):
    stubs.script(preview=preview_for(["alpha"]))

    resp = client.post("/api/runs", json={
        "playbook": "servers.yml",
        "limit": "servers:!beta",
        "tags": ["docker", "users"],
        "check": False,
        "diff": True,
        "verbosity": 2,
        "extra_vars": {"release": "1.2", "flags": {"x": True}},
    })

    assert resp.status_code == 201, resp.text
    run = wait_for_run(client, resp.json()["id"])
    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["check"] is False
    assert run["diff"] is True
    assert run["verbosity"] == 2
    assert run["limit"] == "servers:!beta"
    assert run["hosts"] == ["alpha"]
    assert run["tags"] == ["docker", "users"]
    assert run["extra_vars"] == {"release": "1.2", "flags": {"x": True}}
    [runner] = stubs.calls("ansible-runner")
    assert runner["cmdline"] == (
        "--diff --limit 'servers:!beta' --tags docker,users -vv "
        "--extra-vars '{\"release\": \"1.2\", \"flags\": {\"x\": true}}'"
    )
    [helper] = stubs.calls("ansible-python")
    assert helper["request"]["limit"] == "servers:!beta"
    assert helper["request"]["playbook"] == "playbooks/servers.yml"


def test_a_check_run_without_options_runs_every_targeted_host_and_records_them(client, stubs):
    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["hosts"] == ["alpha", "beta"]
    assert (run["limit"], run["tags"], run["diff"], run["verbosity"], run["extra_vars"]) == (None, [], False, 0, {})
    [runner] = stubs.calls("ansible-runner")
    assert runner["cmdline"] == "--check"


def test_a_limit_with_a_pattern_that_matches_nothing_fails_the_run_before_ansible_starts(client, stubs):
    stubs.script(preview=preview_for(["alpha"], unmatched=["alhpa"]))

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml", "limit": "alpha,alhpa"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == "The limit has patterns that match no hosts: alhpa"
    assert stubs.calls("ansible-runner") == []


def test_a_limit_that_leaves_no_hosts_fails_the_run(client, stubs):
    stubs.script(preview=preview_for([]))

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml", "limit": "servers:&docker"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"].startswith("No hosts to run on")
    assert stubs.calls("ansible-runner") == []


def test_a_preview_helper_error_fails_the_run_with_its_message(client, stubs):
    stubs.script(preview={"ok": False, "error": "ERROR! the role 'nope' was not found"})

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == "Host preview failed: ERROR! the role 'nope' was not found"


def test_a_crashing_preview_helper_fails_the_run_without_leaking_its_output(client, stubs):
    stubs.script(preview_rc=1, preview_stderr="Traceback: vault_secret=hunter2")

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["status"] == "failed"
    assert run["failure_reason"] == "Host preview failed: Preview helper failed (exit code 1)"
    assert "hunter2" not in client.get(f"/api/runs/{run['id']}/log").text

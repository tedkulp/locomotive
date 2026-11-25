"""The Run page's task x host grid and per-host recap, built from ansible-runner's events."""

import json

from conftest import sse_events, wait_for_run, wait_until


def task(name, uuid, path="playbooks/servers.yml:3", play="servers"):
    return {
        "event": "playbook_on_task_start",
        "stdout": f"\nTASK [{name}] *****",
        "event_data": {"task": name, "task_uuid": uuid, "task_path": path, "play": play},
    }


def result(event, host, uuid, **data):
    text = {
        "runner_on_ok": f"ok: [{host}]",
        "runner_on_failed": f"fatal: [{host}]: FAILED!",
        "runner_on_skipped": f"skipping: [{host}]",
        "runner_on_unreachable": f"fatal: [{host}]: UNREACHABLE!",
        "runner_on_start": "",
    }[event]
    return {"event": event, "stdout": text, "event_data": {"host": host, "task_uuid": uuid, **data}}


def changed(host, uuid):
    return result("runner_on_ok", host, uuid, res={"changed": True})


def stats(**per_host):
    """per_host: host -> (ok, changed, skipped, failed, unreachable)"""
    keys = ("ok", "changed", "skipped", "failures", "dark")
    data = {key: {h: v[i] for h, v in per_host.items() if v[i]} for i, key in enumerate(keys)}
    return {"event": "playbook_on_stats", "stdout": "\nPLAY RECAP *****", "event_data": data}


MIXED = [
    {"event": "playbook_on_start", "stdout": ""},
    {"event": "playbook_on_play_start", "stdout": "\nPLAY [servers] *****", "event_data": {"play": "servers"}},
    task("Gathering Facts", "t1"),
    result("runner_on_start", "alpha", "t1"),
    result("runner_on_ok", "alpha", "t1"),
    result("runner_on_unreachable", "beta", "t1"),
    task("Install packages", "t2"),
    changed("alpha", "t2"),
    task("Configure", "t3"),
    result("runner_on_failed", "alpha", "t3"),
    task("Optional", "t4"),
    result("runner_on_skipped", "alpha", "t4"),
    stats(alpha=(2, 1, 1, 1, 0), beta=(0, 0, 0, 0, 1)),
]


def start(client, playbook="servers.yml"):
    return client.post("/api/runs", json={"playbook": playbook}).json()["id"]


def test_the_grid_has_a_row_per_task_and_a_cell_per_host(client, stubs):
    stubs.script(runner={"events": MIXED, "rc": 2})
    run_id = start(client)
    wait_for_run(client, run_id)

    grid = client.get(f"/api/runs/{run_id}/grid").json()

    assert grid["hosts"] == ["alpha", "beta"]
    assert [(t["name"], t["results"]) for t in grid["tasks"]] == [
        ("Gathering Facts", {"alpha": "ok", "beta": "unreachable"}),
        ("Install packages", {"alpha": "changed"}),
        ("Configure", {"alpha": "failed"}),
        ("Optional", {"alpha": "skipped"}),
    ]
    assert {t["play"] for t in grid["tasks"]} == {"servers"}
    assert all(t["included_from"] is None for t in grid["tasks"])


def test_the_recap_of_a_completed_run_is_ansibles_own(client, stubs):
    stubs.script(runner={"events": MIXED, "rc": 2})
    run_id = start(client)
    run = wait_for_run(client, run_id)

    expected = {
        "alpha": {"ok": 2, "changed": 1, "skipped": 1, "failed": 1, "unreachable": 0},
        "beta": {"ok": 0, "changed": 0, "skipped": 0, "failed": 0, "unreachable": 1},
    }
    assert run["recap"] == expected
    assert run["recap_source"] == "ansible"
    grid = client.get(f"/api/runs/{run_id}/grid").json()
    assert grid["recap"] == expected
    assert grid["recap_source"] == "ansible"


def test_tasks_from_an_include_are_marked_with_the_included_file(client, stubs):
    inc = "/work/project/roles/docker/tasks/swarm.yml"
    stubs.script(runner={"events": [
        task("docker : include_tasks", "t1", path="/work/project/roles/docker/tasks/main.yml:4"),
        result("runner_on_ok", "alpha", "t1"),
        {"event": "playbook_on_include", "stdout": f"included: {inc} for alpha",
         "event_data": {"included_file": inc, "hosts": ["alpha"]}},
        task("docker : Init swarm", "t2", path=f"{inc}:1"),
        changed("alpha", "t2"),
        task("docker : Join workers", "t3", path=f"{inc}:9"),
        result("runner_on_skipped", "alpha", "t3"),
        task("docker : after the include", "t4", path="/work/project/roles/docker/tasks/main.yml:12"),
        result("runner_on_ok", "alpha", "t4"),
    ]})
    run_id = start(client)
    wait_for_run(client, run_id)

    tasks = client.get(f"/api/runs/{run_id}/grid").json()["tasks"]

    assert [(t["name"], t["included_from"]) for t in tasks] == [
        ("docker : include_tasks", None),
        ("docker : Init swarm", "swarm.yml"),
        ("docker : Join workers", "swarm.yml"),
        ("docker : after the include", None),
    ]


def test_a_run_that_never_reached_ansible_has_an_empty_grid(client, stubs):
    stubs.script(galaxy_rc=1)
    run_id = start(client)
    run = wait_for_run(client, run_id)

    grid = client.get(f"/api/runs/{run_id}/grid").json()

    assert grid["tasks"] == [] and grid["hosts"] == []
    assert run["recap"] == {}


def test_log_lines_name_their_host_and_the_log_can_be_filtered_to_one_host(client, stubs):
    stubs.script(runner={"events": MIXED, "rc": 2})
    run_id = start(client)
    wait_for_run(client, run_id)

    lines = client.get(f"/api/runs/{run_id}/log").json()["lines"]
    by_text = {line["text"]: line["host"] for line in lines}
    assert by_text["fatal: [beta]: UNREACHABLE!"] == "beta"
    assert by_text["TASK [Install packages] *****"] is None

    beta = [line["text"] for line in client.get(f"/api/runs/{run_id}/log", params={"host": "beta"}).json()["lines"]]
    assert "fatal: [beta]: UNREACHABLE!" in beta
    assert "TASK [Install packages] *****" in beta  # lines about no host stay, for context
    assert not any("[alpha]" in text for text in beta)


LIVE = {
    "events": [
        task("Gathering Facts", "t1"),
        result("runner_on_ok", "alpha", "t1"),
        result("runner_on_ok", "beta", "t1"),
        task("Install packages", "t2"),
        result("runner_on_start", "alpha", "t2"),
        result("runner_on_start", "beta", "t2"),
        changed("alpha", "t2"),
        # gate: beta is still on "Install packages"
        changed("beta", "t2"),
        stats(alpha=(2, 1, 0, 0, 0), beta=(2, 1, 0, 0, 0)),
    ],
    "gate_after": 7,
}


def test_the_grid_and_recap_fill_in_while_the_run_is_running(client, stubs):
    stubs.script(runner=LIVE)
    run_id = start(client)

    def grid():
        return client.get(f"/api/runs/{run_id}/grid").json()

    wait_until(lambda: len(grid()["tasks"]) == 2 and "alpha" in grid()["tasks"][1]["results"]
               and grid()["tasks"][1]["results"]["alpha"] == "changed")
    live = grid()
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
    assert live["tasks"][1]["results"] == {"alpha": "changed", "beta": "running"}
    assert live["recap"] == {
        "alpha": {"ok": 2, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0},
        "beta": {"ok": 1, "changed": 0, "skipped": 0, "failed": 0, "unreachable": 0},
    }
    assert live["recap_source"] == "locomotive"

    stubs.release_gate()
    wait_for_run(client, run_id)
    assert grid()["tasks"][1]["results"] == {"alpha": "changed", "beta": "changed"}


def test_the_stream_carries_grid_updates_and_log_hosts(client, stubs):
    stubs.script(runner=LIVE)
    run_id = start(client)

    with client.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        events = sse_events(resp.iter_lines())
        grids = []
        for event, _, data in events:
            if event == "grid":
                grids.append(json.loads(data))
                if grids[-1]["tasks"][-1:] and grids[-1]["tasks"][-1]["results"].get("alpha") == "changed":
                    break
        assert grids[-1]["tasks"][1]["results"]["beta"] == "running"
        stubs.release_gate()
        rest = list(events)

    final = [json.loads(d) for e, _, d in rest if e == "grid"][-1]
    assert final["recap_source"] == "ansible"
    assert final["tasks"][1]["results"]["beta"] == "changed"
    logs = [json.loads(d) for e, _, d in rest if e == "log"]
    assert next(line for line in logs if line["text"] == "ok: [beta]")["host"] == "beta"
    assert rest[-1][0] == "end"

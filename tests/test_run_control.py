"""Stop after this task, Kill, and what a restart does to a Run that was running."""

from conftest import wait_for_run, wait_until
from test_run_grid import changed, result, task

# Gated mid-"Install packages": alpha has its result, beta is still working.
TWO_TASKS = {
    "events": [
        task("Gathering Facts", "t1"),
        result("runner_on_ok", "alpha", "t1"),
        result("runner_on_ok", "beta", "t1"),
        task("Install packages", "t2"),
        result("runner_on_start", "alpha", "t2"),
        result("runner_on_start", "beta", "t2"),
        changed("alpha", "t2"),
        changed("beta", "t2"),
        task("Configure", "t3"),
        result("runner_on_ok", "alpha", "t3"),
        result("runner_on_ok", "beta", "t3"),
        {"event": "playbook_on_stats", "stdout": "PLAY RECAP", "event_data": {"ok": {"alpha": 3, "beta": 3}}},
    ],
    "gate_after": 7,
}


def start_gated(client, stubs, **runner):
    stubs.script(runner={**TWO_TASKS, **runner})
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_until(lambda: "alpha" in _results(client, run_id, 1) and _results(client, run_id, 1)["alpha"] == "changed")
    return run_id


def _results(client, run_id, index):
    tasks = client.get(f"/api/runs/{run_id}/grid").json()["tasks"]
    return tasks[index]["results"] if len(tasks) > index else {}


def test_stop_after_this_task_lets_the_current_task_finish_then_cancels_the_run(client, stubs):
    run_id = start_gated(client, stubs)

    resp = client.post(f"/api/runs/{run_id}/stop")

    assert resp.status_code == 200
    assert resp.json()["stop_requested"] is True
    assert resp.json()["status"] == "running"
    stubs.release_gate()
    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Stopped after a task"
    assert run["stop_requested"] is False
    grid = client.get(f"/api/runs/{run_id}/grid").json()
    # The task in progress finished on every host; the next one never ran anywhere.
    assert grid["tasks"][1]["results"] == {"alpha": "changed", "beta": "changed"}
    assert all(not t["results"] for t in grid["tasks"][2:])
    expected = {
        "alpha": {"ok": 2, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0},
        "beta": {"ok": 2, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0},
    }
    assert run["recap"] == grid["recap"] == expected
    assert run["recap_source"] == grid["recap_source"] == "locomotive"
    log = [line["text"] for line in client.get(f"/api/runs/{run_id}/log").json()["lines"]]
    assert "Stop requested: the Run ends when the current task finishes" in log
    assert log[-1] == "Run canceled: Stopped after a task"


def test_kill_ends_the_run_now_with_a_recap_built_by_locomotive(client, stubs):
    run_id = start_gated(client, stubs)

    resp = client.post(f"/api/runs/{run_id}/kill")

    assert resp.status_code == 200
    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Killed"
    assert [c["signal"] for c in stubs.calls("signal")] == ["SIGINT"]
    grid = client.get(f"/api/runs/{run_id}/grid").json()
    assert grid["tasks"][1]["results"] == {"alpha": "changed", "beta": "interrupted"}
    assert run["recap"] == {
        "alpha": {"ok": 2, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0},
        "beta": {"ok": 1, "changed": 0, "skipped": 0, "failed": 0, "unreachable": 0},
    }
    assert run["recap_source"] == "locomotive"
    log = [line["text"] for line in client.get(f"/api/runs/{run_id}/log").json()["lines"]]
    assert log[-1] == "Run canceled: Killed"


def test_kill_escalates_to_sigkill_when_sigint_is_not_enough(start_app, stubs):
    client = start_app(kill_grace_seconds=0.3)
    run_id = start_gated(client, stubs, ignore_sigint=True)

    client.post(f"/api/runs/{run_id}/kill").raise_for_status()

    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Killed"
    assert [c["signal"] for c in stubs.calls("signal")] == ["SIGINT"]  # SIGKILL can't be caught
    assert run["recap_source"] == "locomotive"


def test_a_stop_overtaken_by_a_kill_is_a_kill(client, stubs):
    run_id = start_gated(client, stubs)
    client.post(f"/api/runs/{run_id}/stop").raise_for_status()

    client.post(f"/api/runs/{run_id}/kill").raise_for_status()

    run = wait_for_run(client, run_id)
    assert run["failure_reason"] == "Killed"


def test_kill_before_ansible_starts_cancels_the_run_without_running_it(client, stubs):
    stubs.script(mise_install_gate=True)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_until(lambda: any(c["argv"][:1] == ["install"] for c in stubs.calls("mise")))

    client.post(f"/api/runs/{run_id}/kill").raise_for_status()

    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Killed"
    assert stubs.calls("ansible-runner") == []


def test_stop_before_ansible_starts_cancels_the_run_without_running_it(client, stubs):
    stubs.script(mise_install_gate=True)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_until(lambda: any(c["argv"][:1] == ["install"] for c in stubs.calls("mise")))

    client.post(f"/api/runs/{run_id}/stop").raise_for_status()
    stubs.release_gate()

    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Stopped after a task"
    assert stubs.calls("ansible-runner") == []


def test_a_finished_run_cannot_be_stopped_or_killed(client):
    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    for action in ("stop", "kill"):
        resp = client.post(f"/api/runs/{run['id']}/{action}")
        assert resp.status_code == 409
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "succeeded"


def test_stop_and_kill_of_an_unknown_run_are_404(client):
    assert client.post("/api/runs/999/stop").status_code == 404
    assert client.post("/api/runs/999/kill").status_code == 404


def test_a_run_cut_off_by_a_shutdown_is_interrupted_after_restart_and_not_retried(start_app, stop_app, stubs):
    first = start_app()
    run_id = start_gated(first, stubs)

    stop_app(first)
    second = start_app()

    run = second.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "interrupted"
    assert run["finished_at"] is not None
    assert run["recap"]["alpha"]["changed"] == 1
    assert run["recap_source"] == "locomotive"
    grid = second.get(f"/api/runs/{run_id}/grid").json()
    assert grid["tasks"][1]["results"] == {"alpha": "changed", "beta": "interrupted"}
    stubs.release_gate()
    assert len(stubs.calls("ansible-runner")) == 1
    assert second.get(f"/api/runs/{run_id}").json()["status"] == "interrupted"


def test_a_run_left_running_by_a_crash_is_marked_interrupted_at_startup(start_app, stubs):
    first = start_app()
    run_id = start_gated(first, stubs)

    # A second instance starting on the same data dir sees what a restart after a crash would:
    # a Run recorded as running with nothing supervising it.
    second = start_app()

    run = second.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "interrupted"
    assert run["failure_reason"] == "locomotive restarted during the Run"
    assert run["recap"]["alpha"] == {"ok": 2, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0}
    grid = second.get(f"/api/runs/{run_id}/grid").json()
    assert grid["tasks"][1]["results"]["beta"] == "interrupted"
    log = [line["text"] for line in second.get(f"/api/runs/{run_id}/log").json()["lines"]]
    assert log[-1] == "Run interrupted: locomotive restarted during the Run"
    seqs = [line["seq"] for line in second.get(f"/api/runs/{run_id}/log").json()["lines"]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len(stubs.calls("ansible-runner")) == 1
    stubs.release_gate()

"""Per-host queue: two Runs never touch the same host at once, whatever started them, check mode included."""

import pytest

from conftest import preview_for, wait_for_run, wait_until

# One Ansible task that blocks until its named gate opens: the Run holds its hosts until then.
BLOCKED = {
    "events": [
        {"event": "playbook_on_task_start", "stdout": "TASK [ping] ***"},
        {"event": "runner_on_ok", "stdout": "ok: [host]", "event_data": {"host": "alpha"}},
    ],
    "gate_after": 1,
}


def hosts_scenario(stubs, **gated_limits):
    """Host previews for limits alpha / beta / alpha,beta; each named limit's Run blocks on its own gate."""
    stubs.script(
        previews={
            "alpha": preview_for(["alpha"]),
            "beta": preview_for(["beta"]),
            "alpha,beta": preview_for(["alpha", "beta"]),
            "": preview_for(["alpha", "beta"]),
        },
        runner_by_limit={limit: {**BLOCKED, "gate": gate} for limit, gate in gated_limits.items()},
    )


def launch(client, limit=None, **options):
    body = {"playbook": "servers.yml", **options}
    if limit is not None:
        body["limit"] = limit
    resp = client.post("/api/runs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def get(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()


def wait_status(client, run_id, status):
    wait_until(lambda: get(client, run_id)["status"] == status)
    return get(client, run_id)


def wait_waiting(client, run_id):
    wait_until(lambda: get(client, run_id)["waiting_for"] is not None)
    return get(client, run_id)


def runner_limits(stubs):
    import shlex

    limits = []
    for call in stubs.calls("ansible-runner"):
        words = shlex.split(call["cmdline"])
        limits.append(words[words.index("--limit") + 1] if "--limit" in words else "")
    return limits


def test_a_run_overlapping_a_running_run_queues_and_shows_what_it_waits_for(client, stubs):
    hosts_scenario(stubs, alpha="a")
    first = launch(client, "alpha")
    wait_status(client, first, "running")

    # A real Run conflicts with a check Run just the same.
    second = launch(client, "alpha,beta", check=False)

    run = wait_waiting(client, second)
    assert run["status"] == "queued"
    assert run["waiting_for"] == {"id": first, "playbook": "servers.yml"}
    assert run["started_at"] is None
    log = [line["text"] for line in client.get(f"/api/runs/{second}/log").json()["lines"]]
    assert f"Waiting for Run #{first} (servers.yml): it holds alpha" in log
    assert runner_limits(stubs) == ["alpha"]

    stubs.release_gate("a")

    assert wait_for_run(client, first)["status"] == "succeeded"
    run = wait_for_run(client, second)
    assert run["status"] == "succeeded"
    assert run["waiting_for"] is None
    assert runner_limits(stubs) == ["alpha", "alpha,beta"]


def test_runs_on_separate_hosts_run_in_parallel(client, stubs):
    hosts_scenario(stubs, alpha="a", beta="b")
    first = launch(client, "alpha")
    second = launch(client, "beta")

    wait_status(client, first, "running")
    wait_status(client, second, "running")
    wait_until(lambda: sorted(runner_limits(stubs)) == ["alpha", "beta"])
    assert get(client, second)["waiting_for"] is None

    stubs.release_gate("b")
    assert wait_for_run(client, second)["status"] == "succeeded"
    assert get(client, first)["status"] == "running"
    stubs.release_gate("a")
    assert wait_for_run(client, first)["status"] == "succeeded"


def test_a_queued_run_never_overtakes_an_earlier_queued_run_sharing_a_host(client, stubs):
    hosts_scenario(stubs, alpha="a", **{"alpha,beta": "ab"})
    first = launch(client, "alpha")
    wait_status(client, first, "running")
    big = launch(client, "alpha,beta")
    wait_waiting(client, big)

    # beta is free, but the big Run queued earlier wants it: no starving the big Run.
    small = launch(client, "beta")

    run = wait_waiting(client, small)
    assert run["status"] == "queued"
    assert run["waiting_for"] == {"id": big, "playbook": "servers.yml"}
    log = [line["text"] for line in client.get(f"/api/runs/{small}/log").json()["lines"]]
    assert f"Waiting for Run #{big} (servers.yml): it is queued ahead for beta" in log

    stubs.release_gate("a")
    wait_status(client, big, "running")
    assert get(client, small)["status"] == "queued"
    assert get(client, small)["waiting_for"] == {"id": big, "playbook": "servers.yml"}

    stubs.release_gate("ab")
    assert wait_for_run(client, small)["status"] == "succeeded"
    assert runner_limits(stubs) == ["alpha", "alpha,beta", "beta"]


def test_a_queued_run_starts_ahead_of_earlier_ones_on_other_hosts(client, stubs):
    hosts_scenario(stubs, alpha="a")
    first = launch(client, "alpha")
    wait_status(client, first, "running")
    behind = launch(client, "alpha")
    wait_waiting(client, behind)

    other = launch(client, "beta")

    assert wait_for_run(client, other)["status"] == "succeeded"
    assert get(client, behind)["status"] == "queued"
    stubs.release_gate("a")
    assert wait_for_run(client, behind)["status"] == "succeeded"


def test_a_queued_run_can_be_cancelled_and_never_runs(client, stubs):
    hosts_scenario(stubs, alpha="a", **{"alpha,beta": "ab"})
    first = launch(client, "alpha")
    wait_status(client, first, "running")
    big = launch(client, "alpha,beta")
    wait_waiting(client, big)
    small = launch(client, "beta")
    wait_waiting(client, small)

    resp = client.post(f"/api/runs/{big}/cancel")

    assert resp.status_code == 200
    run = wait_for_run(client, big)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Canceled while queued"
    assert run["waiting_for"] is None
    assert run["started_at"] is None
    log = [line["text"] for line in client.get(f"/api/runs/{big}/log").json()["lines"]]
    assert log[-1] == "Run canceled: Canceled while queued"
    # The Run that was waiting behind it goes ahead now, while the first still holds alpha.
    assert wait_for_run(client, small)["status"] == "succeeded"
    assert get(client, first)["status"] == "running"
    stubs.release_gate("a")
    wait_for_run(client, first)
    assert runner_limits(stubs) == ["alpha", "beta"]


def test_a_run_still_resolving_its_hosts_can_be_cancelled(client, stubs):
    stubs.script(mise_install_gate=True)
    run_id = launch(client)
    wait_until(lambda: any(c["argv"][:1] == ["install"] for c in stubs.calls("mise")))

    client.post(f"/api/runs/{run_id}/cancel").raise_for_status()

    run = wait_for_run(client, run_id)
    assert run["status"] == "canceled"
    assert run["failure_reason"] == "Canceled while queued"
    assert stubs.calls("ansible-runner") == []


def test_only_queued_runs_can_be_cancelled(client, stubs):
    hosts_scenario(stubs, alpha="a")
    running = launch(client, "alpha")
    wait_status(client, running, "running")
    done = wait_for_run(client, launch(client, "beta"))

    assert client.post(f"/api/runs/{running}/cancel").status_code == 409
    assert client.post(f"/api/runs/{done['id']}/cancel").status_code == 409
    assert client.post("/api/runs/999/cancel").status_code == 404
    assert get(client, running)["status"] == "running"
    stubs.release_gate("a")
    assert wait_for_run(client, running)["status"] == "succeeded"


def test_a_queued_clean_run_keeps_the_sha_and_hosts_it_was_queued_with(client, remote, stubs):
    hosts_scenario(stubs, alpha="a")
    first = launch(client, "alpha")
    wait_status(client, first, "running")
    queued_sha = remote.sha("main")
    second = launch(client)
    run = wait_waiting(client, second)
    assert run["sha"] == queued_sha
    assert run["hosts"] == ["alpha", "beta"]
    previews = len(stubs.calls("ansible-python"))

    # While it waits, main moves on and the inventory now resolves the limit differently.
    remote.commit({"README.md": "newer\n"})
    stubs.script(previews={"": preview_for(["beta"])})
    stubs.release_gate("a")

    run = wait_for_run(client, second)
    assert run["status"] == "succeeded"
    assert run["sha"] == queued_sha
    assert run["hosts"] == ["alpha", "beta"]
    assert len(stubs.calls("ansible-python")) == previews


def test_a_failed_fetch_fails_the_run_at_once_instead_of_queueing(client, remote, stubs, tmp_path):
    hosts_scenario(stubs, alpha="a")
    first = launch(client, "alpha")
    wait_status(client, first, "running")
    remote.bare.rename(tmp_path / "gone.git")

    run = wait_for_run(client, launch(client, "alpha"))

    assert run["status"] == "failed"
    assert run["failure_reason"].startswith("Fetch failed")
    assert run["waiting_for"] is None
    assert get(client, first)["status"] == "running"
    stubs.release_gate("a")
    wait_for_run(client, first)


def test_queued_runs_stay_queued_across_a_restart(start_app, stop_app, remote, stubs):
    first_app = start_app()
    hosts_scenario(stubs, alpha="a")
    holder = launch(first_app, "alpha")
    wait_status(first_app, holder, "running")
    queued = launch(first_app)
    run = wait_waiting(first_app, queued)
    queued_sha, previews = run["sha"], len(stubs.calls("ansible-python"))

    stop_app(first_app)
    remote.commit({"README.md": "newer\n"})
    second_app = start_app()

    assert get(second_app, holder)["status"] == "interrupted"
    run = wait_for_run(second_app, queued)  # nothing holds its hosts any more, so it goes ahead
    assert run["status"] == "succeeded"
    assert run["sha"] == queued_sha
    assert run["hosts"] == ["alpha", "beta"]
    assert len(stubs.calls("ansible-python")) == previews
    assert runner_limits(stubs) == ["alpha", ""]
    log = [line["text"] for line in second_app.get(f"/api/runs/{queued}/log").json()["lines"]]
    assert "locomotive restarted; the Run is still queued" in log


def test_queued_runs_keep_their_order_across_a_restart(start_app, stop_app, stubs):
    first_app = start_app()
    hosts_scenario(stubs, alpha="a")
    holder = launch(first_app, "alpha")
    wait_status(first_app, holder, "running")
    big = launch(first_app, "alpha,beta")
    wait_waiting(first_app, big)
    small = launch(first_app, "beta")
    wait_waiting(first_app, small)

    stop_app(first_app)
    second_app = start_app()

    # Both stay queued in order; the big one runs first because it was launched first.
    assert wait_for_run(second_app, big)["status"] == "succeeded"
    assert wait_for_run(second_app, small)["status"] == "succeeded"
    assert runner_limits(stubs) == ["alpha", "alpha,beta", "beta"]


@pytest.mark.skip(reason="needs Working Copy Runs (tedkulp/ansible#22); adapt the launch body when it lands")
def test_a_working_copy_run_re_resolves_its_hosts_at_start_and_waits_again_on_a_new_overlap(client, stubs):
    hosts_scenario(stubs, alpha="a", beta="b")
    holder = launch(client, "alpha")
    wait_status(client, holder, "running")
    stubs.script(previews={**stubs.scenario["previews"], "servers": preview_for(["alpha"])})
    working = launch(client, "servers", checkout="working_copy")  # hypothetical #22 launch option
    run = wait_waiting(client, working)
    assert run["hosts"] == ["alpha"]
    other = launch(client, "beta")
    wait_status(client, other, "running")

    # While it waits, the Working Copy's inventory gains beta for this limit.
    stubs.script(previews={**stubs.scenario["previews"], "servers": preview_for(["alpha", "beta"])})
    stubs.release_gate("a")

    wait_until(lambda: get(client, working)["hosts"] == ["alpha", "beta"])
    run = wait_waiting(client, working)
    assert run["status"] == "queued"
    assert run["waiting_for"] == {"id": other, "playbook": "servers.yml"}
    stubs.release_gate("b")
    assert wait_for_run(client, working)["status"] == "succeeded"

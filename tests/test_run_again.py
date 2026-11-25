"""Run again: a new Run with the settings an earlier one recorded."""

from conftest import wait_for_run, wait_until


def test_run_again_starts_a_new_run_with_the_same_settings(client, remote, stubs):
    stubs.script(runner={"events": [], "rc": 2})
    first = wait_for_run(client, client.post("/api/runs", json={"playbook": "docker.yml"}).json()["id"])
    assert first["status"] == "failed"
    sha = remote.commit({"README.md": "newer\n"})
    stubs.script(runner={})

    resp = client.post(f"/api/runs/{first['id']}/again")

    assert resp.status_code == 201
    again = wait_for_run(client, resp.json()["id"])
    assert again["id"] != first["id"]
    assert again["status"] == "succeeded"
    for key in ("playbook", "check", "checkout", "branch"):
        assert again[key] == first[key], key
    assert again["source"] == "ui"
    assert again["sha"] == sha  # a clean Run of the branch fetches it afresh
    assert [c["playbook"] for c in stubs.calls("ansible-runner")] == ["playbooks/docker.yml"] * 2
    assert stubs.calls("ansible-runner")[1]["cmdline"] == "--check"


def test_an_interrupted_run_can_be_run_again(start_app, stop_app, stubs):
    stubs.script(runner={"events": [{"event": "playbook_on_start", "stdout": ""}] * 2, "gate_after": 1})
    first = start_app()
    run_id = first.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]

    wait_until(lambda: len(stubs.calls("ansible-runner")) == 1)
    stop_app(first)
    stubs.release_gate()
    second = start_app()
    assert second.get(f"/api/runs/{run_id}").json()["status"] == "interrupted"

    again = wait_for_run(second, second.post(f"/api/runs/{run_id}/again").json()["id"])

    assert again["status"] == "succeeded"
    assert again["playbook"] == "servers.yml"


def test_a_run_whose_playbook_is_now_hidden_cannot_be_run_again(start_app):
    client = start_app(hidden_playbooks=[])
    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "mac.yml"}).json()["id"])
    client = start_app()

    resp = client.post(f"/api/runs/{run['id']}/again")

    assert resp.status_code == 422
    assert "hidden" in resp.json()["detail"]


def test_run_again_of_an_unknown_run_is_404(client):
    assert client.post("/api/runs/999/again").status_code == 404

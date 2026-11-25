"""A Run's export dir lives only while the Run does; orphans are swept at startup.

The data dir is locomotive's own edge (like the remote); these tests look at it only to see
whether throwaway directories are gone.
"""

from pathlib import Path

from conftest import wait_for_run


def test_the_export_dir_is_deleted_when_the_run_ends(client, stubs):
    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    [runner] = stubs.calls("ansible-runner")
    assert run["status"] == "succeeded"
    assert runner["project_files"]  # it existed while ansible-runner ran
    assert not Path(runner["private_data_dir"]).exists()


def test_the_export_dir_is_deleted_when_the_run_fails(client, stubs):
    stubs.script(galaxy_rc=1)

    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    [galaxy] = stubs.calls("ansible-galaxy")
    assert run["status"] == "failed"
    assert not Path(galaxy["cwd"]).exists()


def test_orphan_run_dirs_are_swept_at_startup(start_app, data_dir):
    orphan = data_dir / "runs" / "41" / "project"
    orphan.mkdir(parents=True)
    (orphan / "site.yml").write_text("- hosts: all\n")

    client = start_app()

    assert client.get("/api/runs").status_code == 200
    assert not (data_dir / "runs" / "41").exists()


def test_history_survives_a_restart(start_app):
    first = start_app()
    run = wait_for_run(first, first.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    second = start_app()

    assert second.get(f"/api/runs/{run['id']}").json() == run
    lines = second.get(f"/api/runs/{run['id']}/log").json()["lines"]
    assert lines[-1]["text"] == "Run succeeded"

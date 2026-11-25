"""Clean Runs of any remote branch, chosen from a dropdown (default main)."""

from conftest import wait_for_run


def test_branches_lists_the_remotes_branches_fetched_now(client, remote):
    client.get("/api/branches").raise_for_status()
    remote.commit({"README.md": "x\n"}, branch="feature/new-role")
    remote.commit({"README.md": "y\n"}, branch="fix")

    body = client.get("/api/branches").json()

    assert body["default"] == "main"
    assert body["branches"] == ["feature/new-role", "fix", "main"]


def test_branches_with_an_unreachable_remote_is_a_clear_error(start_app, tmp_path):
    client = start_app(repo_url=(tmp_path / "missing.git").as_uri())

    resp = client.get("/api/branches")

    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("Fetch failed")


def test_a_clean_run_of_another_branch_runs_and_records_that_branch(client, remote, stubs):
    sha = remote.commit({"roles/wip/tasks/main.yml": "- debug: msg=wip\n"}, branch="feature")

    resp = client.post("/api/runs", json={"playbook": "servers.yml", "branch": "feature"})

    assert resp.status_code == 201
    run = wait_for_run(client, resp.json()["id"])
    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["checkout"] == "clean"
    assert run["branch"] == "feature"
    assert run["sha"] == sha
    assert run["dirty"] is None
    [runner] = stubs.calls("ansible-runner")
    assert "roles/wip/tasks/main.yml" in runner["project_files"]


def test_run_again_of_a_branch_run_fetches_that_branch_afresh(client, remote):
    remote.commit({"README.md": "1\n"}, branch="feature")
    first = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml", "branch": "feature"}).json()["id"])
    newer = remote.commit({"README.md": "2\n"}, branch="feature")

    again = wait_for_run(client, client.post(f"/api/runs/{first['id']}/again").json()["id"])

    assert (again["branch"], again["sha"]) == ("feature", newer)


def test_a_run_of_a_branch_that_does_not_exist_fails_with_a_clear_reason(client, stubs):
    run = wait_for_run(client, client.post("/api/runs", json={"playbook": "servers.yml", "branch": "nope"}).json()["id"])

    assert run["status"] == "failed"
    assert run["branch"] == "nope"
    assert "nope" in run["failure_reason"]
    assert stubs.calls("ansible-runner") == []


def test_an_invalid_branch_name_is_rejected_at_launch(client):
    resp = client.post("/api/runs", json={"playbook": "servers.yml", "branch": "--upload-pack=x"})

    assert resp.status_code == 422
    assert client.get("/api/runs").json()["runs"] == []


def test_previews_come_from_the_chosen_branch(client, remote, stubs):
    sha = remote.commit({"playbooks/branch-only.yml": "- hosts: all\n  tasks: []\n"}, branch="feature")

    resp = client.post("/api/preview", json={"playbook": "branch-only.yml", "branch": "feature"})
    on_main = client.post("/api/preview", json={"playbook": "branch-only.yml"})

    assert resp.status_code == 200, resp.text
    assert (resp.json()["checkout"], resp.json()["branch"], resp.json()["sha"]) == ("clean", "feature", sha)
    assert on_main.status_code == 404

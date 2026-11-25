"""Playbooks are listed from the checkout at launch time, never frozen."""


def test_lists_playbooks_of_main_without_hidden_ones(client, remote):
    resp = client.get("/api/playbooks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["branch"] == "main"
    assert body["sha"] == remote.sha("main")
    assert [p["name"] for p in body["playbooks"]] == ["docker.yml", "servers.yml"]


def test_a_playbook_pushed_after_startup_appears_on_the_next_listing(client, remote):
    client.get("/api/playbooks").raise_for_status()

    new_sha = remote.commit({"playbooks/monitoring.yml": "- hosts: monitoring\n  tasks: []\n"})

    body = client.get("/api/playbooks").json()
    assert body["sha"] == new_sha
    assert "monitoring.yml" in [p["name"] for p in body["playbooks"]]


def test_lists_playbooks_of_another_branch(client, remote):
    sha = remote.commit({"playbooks/feature.yml": "- hosts: all\n  tasks: []\n"}, branch="feature")

    body = client.get("/api/playbooks", params={"branch": "feature"}).json()

    assert body["sha"] == sha
    assert "feature.yml" in [p["name"] for p in body["playbooks"]]
    main = client.get("/api/playbooks").json()
    assert "feature.yml" not in [p["name"] for p in main["playbooks"]]


def test_unknown_branch_is_a_clear_error(client):
    resp = client.get("/api/playbooks", params={"branch": "nope"})

    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_unreachable_remote_is_a_clear_error(start_app, tmp_path):
    client = start_app(repo_url=(tmp_path / "missing.git").as_uri())

    resp = client.get("/api/playbooks")

    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("Fetch failed")

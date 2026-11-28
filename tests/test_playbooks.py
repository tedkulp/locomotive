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


# ---------------------------------------------------------------------- Descriptions

HEADED = "---\n# vim: ft=yaml.ansible\n# Base setup for servers.\n#\n# Installs packages.\n- hosts: servers\n  tasks: []\n"


def _by_name(body: dict) -> dict[str, dict]:
    return {p["name"]: p for p in body["playbooks"]}


def test_a_playbook_header_gives_its_description_and_summary(client, remote):
    remote.commit({"playbooks/servers.yml": HEADED})

    listed = _by_name(client.get("/api/playbooks").json())

    assert listed["servers.yml"]["summary"] == "Base setup for servers."
    assert listed["servers.yml"]["description"] == "Base setup for servers.\n\nInstalls packages."
    assert (listed["docker.yml"]["summary"], listed["docker.yml"]["description"]) == (None, None)


def test_descriptions_follow_the_branch(client, remote):
    remote.commit({"playbooks/servers.yml": "# On feature.\n- hosts: servers\n"}, branch="feature")
    remote.commit({"playbooks/servers.yml": "# On main.\n- hosts: servers\n"})

    main = _by_name(client.get("/api/playbooks").json())
    feature = _by_name(client.get("/api/playbooks", params={"branch": "feature"}).json())

    assert main["servers.yml"]["summary"] == "On main."
    assert feature["servers.yml"]["summary"] == "On feature."


def test_a_new_commit_changes_the_description(client, remote):
    remote.commit({"playbooks/servers.yml": "# Before.\n- hosts: servers\n"})
    assert _by_name(client.get("/api/playbooks").json())["servers.yml"]["summary"] == "Before."

    remote.commit({"playbooks/servers.yml": "# After.\n- hosts: servers\n"})

    assert _by_name(client.get("/api/playbooks").json())["servers.yml"]["summary"] == "After."


def test_working_copy_descriptions_are_read_live(start_app, working_copy):
    client = start_app(working_copy_path=working_copy.path)
    working_copy.write({"playbooks/servers.yml": "# Uncommitted.\n- hosts: servers\n"})
    params = {"checkout": "working_copy"}
    assert _by_name(client.get("/api/playbooks", params=params).json())["servers.yml"]["summary"] == "Uncommitted."

    working_copy.write({"playbooks/servers.yml": "# Edited again.\n- hosts: servers\n"})

    assert _by_name(client.get("/api/playbooks", params=params).json())["servers.yml"]["summary"] == "Edited again."

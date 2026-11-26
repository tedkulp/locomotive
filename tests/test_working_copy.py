"""Working Copy: its status next to the launch switch, Runs in place, and what such a Run records.

locomotive only reads the Working Copy and runs in it: it never commits, pulls or pushes it.
"""

import pytest

from conftest import wait_for_run


@pytest.fixture
def wc_client(start_app, working_copy):
    return start_app(working_copy_path=working_copy.path)


def test_status_shows_branch_head_dirty_and_untracked_files(wc_client, working_copy, remote):
    working_copy.write({
        "playbooks/servers.yml": "- hosts: servers\n  tasks: [{debug: {msg: hi}}]\n",
        "roles/new/tasks/main.yml": "- debug: msg=new\n",
    })

    resp = wc_client.get("/api/working-copy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["path"] == str(working_copy.path)
    assert body["branch"] == "main"
    assert body["head_sha"] == remote.sha("main")
    assert body["dirty"] is True
    assert body["changed"] == ["playbooks/servers.yml"]
    assert body["untracked"] == ["roles/new/tasks/main.yml"]
    assert body["upstream"] == "origin/main"
    assert (body["ahead"], body["behind"]) == (0, 0)


def test_status_shows_ahead_and_behind_origin_as_last_fetched(wc_client, working_copy, remote):
    working_copy.commit({"README.md": "local\n"})
    remote.commit({"README.md": "upstream 1\n"})
    remote.commit({"README.md": "upstream 2\n"})

    before_fetch = wc_client.get("/api/working-copy").json()
    working_copy.git("fetch", "-q", "origin")  # the user fetching; locomotive never does
    after_fetch = wc_client.get("/api/working-copy").json()

    assert (before_fetch["ahead"], before_fetch["behind"]) == (1, 0)
    assert (after_fetch["ahead"], after_fetch["behind"]) == (1, 2)
    assert after_fetch["dirty"] is False
    assert after_fetch["changed"] == after_fetch["untracked"] == []


def test_status_without_a_working_copy_says_it_is_unavailable(client):
    body = client.get("/api/working-copy").json()

    assert body["available"] is False
    assert "no Working Copy" in body["reason"]


def test_a_working_copy_run_runs_in_place_and_records_what_was_uncommitted(wc_client, working_copy, stubs):
    head = working_copy.head()
    working_copy.write({
        "playbooks/servers.yml": "- hosts: servers\n  tasks: [{debug: {msg: edited}}]\n",
        "roles/new/tasks/main.yml": "- debug: msg=new\n",
    })

    resp = wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"})

    assert resp.status_code == 201
    run = wait_for_run(wc_client, resp.json()["id"])
    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["checkout"] == "working_copy"
    assert run["branch"] == "main"
    assert run["sha"] == head
    assert run["dirty"] is True
    assert run["untracked"] == ["roles/new/tasks/main.yml"]
    assert run["diff_truncated"] is False
    [runner] = stubs.calls("ansible-runner")
    assert runner["cwd"] == str(working_copy.path)
    assert "roles/new/tasks/main.yml" in runner["project_files"]
    diff = wc_client.get(f"/api/runs/{run['id']}/working-copy-diff").json()
    assert "+  tasks: [{debug: {msg: edited}}]" in diff["diff"]
    assert diff["truncated"] is False
    assert working_copy.path.joinpath("roles/new/tasks/main.yml").exists()  # in place: nothing is cleaned up


def test_a_working_copy_run_uses_the_working_copys_uncommitted_mise_toml(wc_client, working_copy, stubs):
    stubs.script(version_from_mise_toml=True)
    working_copy.write({"mise.toml": '[tools]\n"pipx:ansible-core" = "2.23.1"\n'})

    run = wait_for_run(
        wc_client, wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"}).json()["id"]
    )

    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["ansible_core_version"] == "2.23.1"
    [install] = [c for c in stubs.calls("mise") if c["argv"][:1] == ["install"]]
    assert install["cwd"] == str(working_copy.path)


def test_locomotive_never_commits_pulls_fetches_or_pushes_the_working_copy(wc_client, working_copy, remote, stubs):
    local = working_copy.commit({"README.md": "local, unpushed\n"})
    working_copy.write({"playbooks/servers.yml": "- hosts: all\n  tasks: []\n", "scratch.txt": "wip\n"})
    upstream = remote.commit({"README.md": "upstream\n"})
    remote_refs = remote.git_refs()
    git_before = working_copy.git_state()
    status_before = working_copy.git("status", "--porcelain=v1", "--untracked-files=all")

    wc_client.get("/api/working-copy").raise_for_status()
    first = wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"}).json()
    wait_for_run(wc_client, first["id"])
    stubs.script(runner={"events": [], "rc": 2})
    again = wc_client.post(f"/api/runs/{first['id']}/again").json()
    assert wait_for_run(wc_client, again["id"])["status"] == "failed"
    wc_client.get("/api/working-copy").raise_for_status()

    assert working_copy.git_state() == git_before  # no commit, fetch, pull, index refresh or config change
    assert working_copy.head() == local
    assert working_copy.git("status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert remote.git_refs() == remote_refs  # nothing pushed
    assert remote.sha("main") == upstream


def test_the_recorded_diff_is_capped_at_about_a_megabyte(wc_client, working_copy):
    working_copy.write({"README.md": "".join(f"line {i:07d} of a big edit\n" for i in range(60_000))})  # ~1.5 MB

    run = wait_for_run(
        wc_client, wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"}).json()["id"]
    )

    assert run["diff_truncated"] is True
    diff = wc_client.get(f"/api/runs/{run['id']}/working-copy-diff").json()
    assert diff["truncated"] is True
    assert 900_000 <= len(diff["diff"].encode()) <= 1_000_000
    assert diff["diff"].startswith("diff --git a/README.md b/README.md")


def test_a_clean_working_copy_run_records_it_is_not_dirty(wc_client, working_copy):
    run = wait_for_run(
        wc_client, wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"}).json()["id"]
    )

    assert run["checkout"] == "working_copy"
    assert run["dirty"] is False
    assert run["untracked"] == []
    assert wc_client.get(f"/api/runs/{run['id']}/working-copy-diff").json()["diff"] == ""


def test_clean_runs_record_no_working_copy_state(wc_client):
    run = wait_for_run(wc_client, wc_client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"])

    assert run["checkout"] == "clean"
    assert run["dirty"] is None and run["untracked"] is None
    assert wc_client.get(f"/api/runs/{run['id']}/working-copy-diff").status_code == 404


def test_a_working_copy_run_without_a_working_copy_is_rejected(client):
    resp = client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"})

    assert resp.status_code == 422
    assert "no Working Copy" in resp.json()["detail"]
    assert client.get("/api/runs").json()["runs"] == []


def test_a_working_copy_run_with_a_branch_is_rejected(wc_client):
    resp = wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy", "branch": "main"})

    assert resp.status_code == 422
    assert wc_client.get("/api/runs").json()["runs"] == []


def test_a_working_copy_run_on_another_branch_records_that_branch(wc_client, working_copy):
    working_copy.git("checkout", "-q", "-b", "feature/x")

    run = wait_for_run(
        wc_client, wc_client.post("/api/runs", json={"playbook": "servers.yml", "checkout": "working_copy"}).json()["id"]
    )

    assert run["branch"] == "feature/x"
    assert wc_client.get("/api/working-copy").json()["branch"] == "feature/x"


def test_playbooks_are_listed_from_the_working_copy_as_it_is_on_disk(wc_client, working_copy):
    working_copy.write({"playbooks/wip.yml": "- hosts: all\n  tasks: []\n", "playbooks/docker.yml": None})

    body = wc_client.get("/api/playbooks", params={"checkout": "working_copy"}).json()

    assert body["checkout"] == "working_copy"
    assert body["branch"] == "main"
    assert body["sha"] == working_copy.head()
    assert [p["name"] for p in body["playbooks"]] == ["servers.yml", "wip.yml"]  # mac/locomotive stay hidden


def test_previews_from_the_working_copy_run_the_helper_in_place(wc_client, working_copy, stubs):
    working_copy.write({"playbooks/wip.yml": "- hosts: all\n  tasks: []\n"})

    resp = wc_client.post("/api/preview", json={"playbook": "wip.yml", "checkout": "working_copy"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["checkout"] == "working_copy"
    assert body["sha"] == working_copy.head()
    assert body["hosts"]["hosts"] == ["alpha", "beta"]
    [helper] = stubs.calls("ansible-python")
    assert helper["cwd"] == str(working_copy.path)
    assert helper["request"]["playbook"] == "playbooks/wip.yml"


def test_working_copy_previews_see_edits_made_since_the_last_preview(wc_client, working_copy, stubs):
    request = {"playbook": "servers.yml", "checkout": "working_copy"}
    installs = lambda: len([c for c in stubs.calls("mise") if c["argv"][:1] == ["install"]])  # noqa: E731
    wc_client.post("/api/preview", json=request).raise_for_status()
    wc_client.post("/api/preview", json=request).raise_for_status()
    assert installs() == 1  # the runtime is reused while the toolchain files are unchanged

    working_copy.write({
        "playbooks/late.yml": "- hosts: all\n  tasks: []\n",
        "mise.toml": '[tools]\n"pipx:ansible-core" = "2.23.1"\n',
    })
    resp = wc_client.post("/api/preview", json={**request, "playbook": "late.yml"})

    assert resp.status_code == 200, resp.text
    assert installs() == 2  # an uncommitted mise.toml edit gets its own runtime


def test_a_working_copy_preview_without_a_working_copy_is_rejected(client):
    resp = client.post("/api/preview", json={"playbook": "servers.yml", "checkout": "working_copy"})

    assert resp.status_code == 422
    assert "no Working Copy" in resp.json()["detail"]

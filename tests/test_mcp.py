"""The MCP server at /mcp, driven by the official MCP client over streamable HTTP."""

from __future__ import annotations

import hashlib
import threading
import time

import httpx
import pytest

from conftest import (
    IDENTITY,
    IDENTITY_HEADER,
    LXC_TOKEN,
    MAC_TOKEN,
    PUBLIC_HOSTNAME,
    McpCaller,
    preview_for,
    wait_for_run,
    wait_until,
)
from locomotive import api
from locomotive.app import create_app


def _has_route(path: str) -> bool:
    """Whether this build's HTTP API has ``path`` (some read tools adapt endpoints other tickets add)."""
    return any(getattr(route, "path", None) == path for route in api.router.routes)


# One Ansible task that blocks until its gate opens.
BLOCKED = {
    "events": [
        {"event": "playbook_on_task_start", "stdout": "TASK [ping] ***"},
        {"event": "runner_on_ok", "stdout": "ok: [alpha]", "event_data": {"host": "alpha"}},
    ],
    "gate_after": 1,
}


def test_requests_without_a_valid_bearer_token_get_401(client: httpx.Client, anonymous) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # The UI identity is no way in on /mcp.
    for headers in (
        {IDENTITY_HEADER: IDENTITY},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": f"Basic {LXC_TOKEN}"},
        {"Authorization": f"Bearer {hashlib.sha256(LXC_TOKEN.encode()).hexdigest()}"},  # the hash isn't the token
    ):
        response = anonymous(client, headers).post("/mcp", json=body)
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_a_valid_token_reaches_the_tools(mcp: McpCaller) -> None:
    names = mcp.tool_names()
    assert {"list_playbooks", "start_run", "get_run", "tail_run_output", "wait_for_run"} <= names


@pytest.mark.parametrize("entry", ["lxc", "lxc=not-a-hash", f"Bad Name={'a' * 64}", f"={'a' * 64}"])
def test_a_malformed_token_entry_stops_startup(make_settings, entry) -> None:
    with pytest.raises(ValueError, match="LOCOMOTIVE_MCP_TOKENS"):
        create_app(make_settings(mcp_tokens=[entry]))


def test_without_configured_tokens_no_one_gets_in(start_app, anonymous) -> None:
    client = start_app(mcp_tokens=[])
    response = anonymous(client, {"Authorization": "Bearer anything"}).post("/mcp", json={})
    assert response.status_code == 401


# ---------------------------------------------------------------------- transport security


def test_the_configured_public_hostname_is_allowed_and_others_are_not(client: httpx.Client) -> None:
    assert "list_runs" in McpCaller(str(client.base_url), LXC_TOKEN, host=PUBLIC_HOSTNAME).tool_names()
    assert "list_runs" in McpCaller(str(client.base_url), LXC_TOKEN, host=None).tool_names()  # localhost

    # DNS rebinding: a request naming some other host is refused, token or not.
    response = httpx.post(
        f"{client.base_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {LXC_TOKEN}", "Host": "evil.example.invalid"},
    )
    assert response.status_code == 421


# ---------------------------------------------------------------------- start_run


def test_start_run_returns_the_run_id_and_status_immediately(client, mcp, stubs) -> None:
    stubs.script(runner={**BLOCKED, "gate": "hold"})

    started = time.monotonic()
    run = mcp.ok("start_run", playbook="servers.yml")

    assert time.monotonic() - started < 5
    assert run["status"] in ("queued", "running")
    assert client.get(f"/api/runs/{run['id']}").json()["status"] in ("queued", "running")
    stubs.release_gate("hold")
    assert wait_for_run(client, run["id"])["status"] == "succeeded"


def test_runs_started_over_mcp_are_labelled_with_the_tokens_source(client, mcp) -> None:
    by_lxc = mcp.ok("start_run", playbook="servers.yml")
    by_mac = McpCaller(str(client.base_url), MAC_TOKEN).ok("start_run", playbook="docker.yml")
    by_ui = client.post("/api/runs", json={"playbook": "servers.yml"}).json()

    history = {run["id"]: run["source"] for run in client.get("/api/runs").json()["runs"]}
    assert history[by_lxc["id"]] == "mcp:lxc"
    assert history[by_mac["id"]] == "mcp:mac"
    assert history[by_ui["id"]] == "ui"
    for run_id in history:
        wait_for_run(client, run_id)


def test_a_real_run_requires_confirm(client, mcp, stubs) -> None:
    error = mcp.error("start_run", playbook="servers.yml", check=False)
    assert "confirm: true" in error
    assert client.get("/api/runs").json()["runs"] == []

    run = mcp.ok("start_run", playbook="servers.yml", check=False, confirm=True)
    done = wait_for_run(client, run["id"])
    assert done["check"] is False
    [runner] = stubs.calls("ansible-runner")
    assert "--check" not in runner["cmdline"]


def test_start_run_passes_every_launch_option(client, mcp, stubs) -> None:
    stubs.script(previews={"alpha": preview_for(["alpha"])})
    run = mcp.ok(
        "start_run",
        playbook="servers.yml",
        hosts=["alpha"],
        tags=["web", "db"],
        diff=True,
        extra_vars={"release": "1.2"},
        verbosity=2,
    )

    done = wait_for_run(client, run["id"])
    assert done["status"] == "succeeded", done["failure_reason"]
    assert (done["limit"], done["hosts"], done["tags"], done["diff"], done["verbosity"], done["extra_vars"]) == (
        "alpha", ["alpha"], ["web", "db"], True, 2, {"release": "1.2"},
    )
    [runner] = stubs.calls("ansible-runner")
    assert runner["cmdline"] == '--check --diff --limit alpha --tags web,db -vv --extra-vars \'{"release": "1.2"}\''


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"playbook": "mac.yml"}, "hidden"),
        ({"playbook": "servers.yml", "limit": "@hosts.txt"}, "limit files"),
        ({"playbook": "servers.yml", "verbosity": 7}, "verbosity"),
        ({"playbook": "servers.yml", "hosts": ["alpha"], "limit": "beta"}, "not both"),
        ({"playbook": "servers.yml", "source": "elsewhere"}, "source"),
    ],
)
def test_options_locomotive_cant_honour_fail_the_call(client, mcp, options, message) -> None:
    assert message in mcp.error("start_run", **options)
    assert client.get("/api/runs").json()["runs"] == []


# ---------------------------------------------------------------------- following a Run


def test_tail_run_output_pages_through_the_log_with_a_cursor(client, mcp, stubs) -> None:
    stubs.script(runner={"events": [
        {"event": "playbook_on_task_start", "stdout": f"TASK [step {n}] ***"} for n in range(7)
    ]})
    run = mcp.ok("start_run", playbook="servers.yml")
    wait_for_run(client, run["id"])

    texts, cursor, pages = [], 0, 0
    while True:
        page = mcp.ok("tail_run_output", run_id=run["id"], cursor=cursor, max_lines=3)
        assert len(page["lines"]) <= 3
        texts += [line["text"] for line in page["lines"]]
        cursor = page["cursor"]
        pages += 1
        if page["finished"] and not page["more"]:
            break

    full = client.get(f"/api/runs/{run['id']}/log").json()["lines"]
    assert texts == [line["text"] for line in full]
    assert pages >= 3
    assert "TASK [step 6] ***" in texts
    # Nothing new past the end: the same cursor comes back with no lines.
    again = mcp.ok("tail_run_output", run_id=run["id"], cursor=cursor)
    assert (again["lines"], again["cursor"], again["status"]) == ([], cursor, "succeeded")


def test_tail_run_output_follows_a_run_still_going(client, mcp, stubs) -> None:
    stubs.script(runner={**BLOCKED, "gate": "hold"})
    run = mcp.ok("start_run", playbook="servers.yml")
    wait_until(lambda: "TASK [ping] ***" in [l["text"] for l in client.get(f"/api/runs/{run['id']}/log").json()["lines"]])

    first = mcp.ok("tail_run_output", run_id=run["id"])
    assert first["finished"] is False
    assert "TASK [ping] ***" in [line["text"] for line in first["lines"]]

    stubs.release_gate("hold")
    wait_for_run(client, run["id"])
    rest = mcp.ok("tail_run_output", run_id=run["id"], cursor=first["cursor"])
    assert rest["finished"] is True
    assert rest["lines"][0]["seq"] == first["cursor"] + 1
    assert rest["lines"][-1]["text"] == "Run succeeded"


def test_wait_for_run_returns_after_max_seconds_while_the_run_is_going(client, mcp, stubs) -> None:
    stubs.script(runner={**BLOCKED, "gate": "hold"})
    run = mcp.ok("start_run", playbook="servers.yml")

    started = time.monotonic()
    waited = mcp.ok("wait_for_run", run_id=run["id"], max_seconds=1)

    assert 1 <= time.monotonic() - started < 5
    assert waited["finished"] is False
    assert waited["run"]["status"] in ("queued", "running")
    stubs.release_gate("hold")
    wait_for_run(client, run["id"])


def test_wait_for_run_returns_early_when_the_run_ends(client, mcp, stubs) -> None:
    stubs.script(runner={**BLOCKED, "gate": "hold"})
    run = mcp.ok("start_run", playbook="servers.yml")
    threading.Timer(1.0, stubs.release_gate, args=("hold",)).start()

    started = time.monotonic()
    waited = mcp.ok("wait_for_run", run_id=run["id"], max_seconds=30)

    assert time.monotonic() - started < 10
    assert waited["finished"] is True
    assert waited["run"]["status"] == "succeeded"


def test_wait_for_run_waits_at_most_30_seconds(mcp) -> None:
    assert "max_seconds" in mcp.error("wait_for_run", run_id=1, max_seconds=31)


def test_an_unknown_run_is_a_tool_error(mcp) -> None:
    for tool in ("get_run", "tail_run_output", "wait_for_run"):
        assert "Run 999 not found" in mcp.error(tool, run_id=999)


# ---------------------------------------------------------------------- read tools


def test_read_tools_give_what_the_ui_sees(client, mcp) -> None:
    run = mcp.ok("start_run", playbook="servers.yml")
    wait_for_run(client, run["id"])

    assert mcp.ok("list_playbooks") == client.get("/api/playbooks").json()
    assert mcp.ok("list_runs") == client.get("/api/runs?limit=20").json()
    assert mcp.ok("get_run", run_id=run["id"]) == client.get(f"/api/runs/{run['id']}").json()


def test_previews_inventory_and_tags(client, mcp, stubs) -> None:
    reply = preview_for(["alpha"])
    reply["tags"] = ["web", "db"]
    reply["plays"][0]["tasks"] = [{"name": "install nginx", "action": "apt", "tags": ["web"], "selected": True}]
    stubs.script(previews={"alpha": reply, "": reply})

    hosts = mcp.ok("preview_hosts", playbook="servers.yml", hosts=["alpha"])
    assert hosts["hosts"]["hosts"] == ["alpha"]
    assert hosts["command"] == "ansible-playbook playbooks/servers.yml --check --limit alpha"
    assert mcp.ok("preview_hosts", playbook="servers.yml", limit="alpha")["hosts"]["hosts"] == ["alpha"]

    tasks = mcp.ok("preview_tasks", playbook="servers.yml", limit="alpha", tags=["web"])
    assert tasks["tasks"]["label"] == "tasks that may run"
    assert [t["name"] for t in tasks["tasks"]["plays"][0]["tasks"]] == ["install nginx"]

    assert mcp.ok("list_tags", playbook="servers.yml")["tags"] == ["web", "db"]
    inventory = mcp.ok("get_inventory", playbook="servers.yml")
    assert inventory["groups"] == [{"name": "servers", "children": [], "hosts": ["alpha", "beta"]}]


def test_previews_fail_loudly_like_the_ui(mcp) -> None:
    assert "hidden" in mcp.error("preview_hosts", playbook="mac.yml")
    assert "does not exist" in mcp.error("preview_tasks", playbook="nope.yml")


def test_branches_and_working_copy_status(start_app, working_copy, remote) -> None:
    remote.commit({"README.md": "feature\n"}, branch="feature")
    working_copy.write({"roles/new/tasks/main.yml": "- debug: msg=new\n"})
    client = start_app(working_copy_path=working_copy.path)
    mcp = McpCaller(str(client.base_url), LXC_TOKEN)

    branches = mcp.ok("list_branches")
    assert branches == client.get("/api/branches").json()
    assert {"main", "feature"} <= set(branches["branches"])

    status = mcp.ok("working_copy_status")
    assert status == client.get("/api/working-copy").json()
    assert (status["available"], status["untracked"]) == (True, ["roles/new/tasks/main.yml"])

    listed = mcp.ok("list_playbooks", source="working_copy")
    assert listed == client.get("/api/playbooks?checkout=working_copy").json()
    assert listed["checkout"] == "working_copy"


def test_start_run_runs_a_branch_or_the_working_copy(start_app, working_copy, remote) -> None:
    feature = remote.commit({"README.md": "feature\n"}, branch="feature")
    working_copy.write({"README.md": "uncommitted\n"})
    client = start_app(working_copy_path=working_copy.path)
    mcp = McpCaller(str(client.base_url), LXC_TOKEN)

    clean = wait_for_run(client, mcp.ok("start_run", playbook="servers.yml", branch="feature")["id"])
    wc = wait_for_run(client, mcp.ok("start_run", playbook="servers.yml", source="working_copy")["id"])
    default = wait_for_run(client, mcp.ok("start_run", playbook="servers.yml")["id"])

    assert (clean["checkout"], clean["branch"], clean["sha"]) == ("clean", "feature", feature)
    assert (wc["checkout"], wc["dirty"], wc["source"]) == ("working_copy", True, "mcp:lxc")
    assert (default["checkout"], default["branch"]) == ("clean", "main")
    # A Working Copy Run runs whatever branch the Working Copy is on: no branch choice.
    assert "branch" in mcp.error("start_run", playbook="servers.yml", source="working_copy", branch="feature")


@pytest.mark.skipif(not _has_route("/schedules"), reason="needs Schedules (#25)")
def test_list_schedules(client, mcp) -> None:
    assert mcp.ok("list_schedules") == client.get("/api/schedules").json()


# ---------------------------------------------------------------------- the 10s budget


def test_every_tool_returns_within_10_seconds(client, mcp, stubs) -> None:
    stubs.script(runner={**BLOCKED, "gate": "hold"})
    run = mcp.ok("start_run", playbook="servers.yml")
    calls = {
        "list_playbooks": {},
        "list_branches": {},
        "working_copy_status": {},
        "list_schedules": {},
        "list_runs": {},
        "get_run": {"run_id": run["id"]},
        "get_inventory": {"playbook": "servers.yml"},
        "list_tags": {"playbook": "servers.yml"},
        "preview_hosts": {"playbook": "servers.yml"},
        "preview_tasks": {"playbook": "servers.yml"},
        "start_run": {"playbook": "docker.yml"},
        "tail_run_output": {"run_id": run["id"]},
        "wait_for_run": {"run_id": run["id"]},
    }
    assert set(calls) == mcp.tool_names()
    for tool, arguments in calls.items():
        started = time.monotonic()
        mcp.call(tool, **arguments)
        assert time.monotonic() - started < 10, tool
    stubs.release_gate("hold")


def test_a_tool_that_would_take_too_long_answers_in_budget(start_app, stubs) -> None:
    client = start_app(mcp_tool_budget_seconds=1, preview_timeout_seconds=30)
    stubs.script(preview_delay=5)
    mcp = McpCaller(str(client.base_url), LXC_TOKEN)

    started = time.monotonic()
    error = mcp.error("preview_hosts", playbook="servers.yml")

    assert time.monotonic() - started < 3
    assert "did not answer within 1s" in error

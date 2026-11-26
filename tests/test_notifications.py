"""Notifications: one ntfy message per notable Run, sent straight to the configured ntfy server.

The fake HTTP server stands in for ntfy; what it captured is what the user's phone would get.
"""

import json
import socket

import pytest

from conftest import LXC_TOKEN, PUBLIC_HOSTNAME, McpCaller, preview_for, wait_for_run, wait_until
from test_run_grid import changed, result, stats, task
from test_schedules import BLOCKED, create, entries, local, tick

TOKEN = "tk_not-a-real-ntfy-token"
TOPIC = "locomotive-alerts"
PUBLIC_URL = "https://locomotive.example.invalid"

FAILING = {
    "events": [
        task("Gathering Facts", "t1"),
        result("runner_on_ok", "alpha", "t1"),
        result("runner_on_ok", "beta", "t1"),
        task("Install packages", "t2"),
        changed("alpha", "t2"),
        result("runner_on_failed", "beta", "t2", res={"msg": "No package matching 'nope' found"}),
        stats(alpha=(2, 1, 0, 0, 0), beta=(1, 0, 0, 1, 0)),
    ],
    "rc": 2,
}


@pytest.fixture
def notify_app(start_app, fake_http):
    def start(**overrides):
        values = {
            "ntfy_url": fake_http.url,
            "ntfy_topic": TOPIC,
            "ntfy_token": TOKEN,
            "public_url": PUBLIC_URL,
            "ntfy_retry_seconds": 0.01,
            **overrides,
        }
        return start_app(**values)

    return start


@pytest.fixture
def app(notify_app):
    return notify_app()


def sent(fake_http):
    """The ntfy messages received, as JSON bodies."""
    return [json.loads(r.body) for r in fake_http.requests if r.method == "POST"]


def launch(client, **body):
    resp = client.post("/api/runs", json={"playbook": "servers.yml", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_a_failed_hand_started_run_notifies_at_priority_4(app, stubs, fake_http, remote):
    stubs.script(runner=FAILING)

    run = wait_for_run(app, launch(app))
    assert run["status"] == "failed"
    wait_until(lambda: len(sent(fake_http)) == 1)

    [request] = [r for r in fake_http.requests if r.method == "POST"]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    [message] = sent(fake_http)
    assert message["topic"] == TOPIC
    assert message["priority"] == 4
    assert message["title"] == "servers.yml failed"
    assert message["click"] == f"{PUBLIC_URL}/runs/{run['id']}"


def test_a_run_that_fails_before_ansible_starts_notifies_too(app, stubs, fake_http):
    stubs.script(galaxy_rc=1, galaxy_output="ERROR! Galaxy is down")

    run = wait_for_run(app, launch(app))
    assert run["status"] == "failed"
    wait_until(lambda: len(sent(fake_http)) == 1)
    assert sent(fake_http)[0]["priority"] == 4


GATED = {
    "events": [
        task("Gathering Facts", "t1"),
        result("runner_on_ok", "alpha", "t1"),
        task("Install packages", "t2"),
        result("runner_on_start", "alpha", "t2"),
        changed("alpha", "t2"),
    ],
    "gate_after": 4,
}


def started(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()["status"] == "running" and bool(
        client.get(f"/api/runs/{run_id}/grid").json()["tasks"]
    )


def test_a_run_left_running_by_a_crash_notifies_as_interrupted_when_locomotive_starts(
    notify_app, stubs, fake_http
):
    stubs.script(runner=GATED)
    first = notify_app()
    run_id = launch(first)
    wait_until(lambda: started(first, run_id))

    second = notify_app()  # a second instance on the same data dir sees what a restart after a crash would

    assert second.get(f"/api/runs/{run_id}").json()["status"] == "interrupted"
    wait_until(lambda: len(sent(fake_http)) == 1)
    [message] = sent(fake_http)
    assert message["priority"] == 4
    assert message["title"] == "servers.yml interrupted"
    stubs.release_gate()


def test_a_run_cut_off_by_a_shutdown_notifies_as_interrupted(notify_app, stop_app, stubs, fake_http):
    stubs.script(runner=GATED)
    client = notify_app()
    run_id = launch(client)
    wait_until(lambda: started(client, run_id))

    stop_app(client)

    [message] = sent(fake_http)
    assert message["priority"] == 4
    assert message["title"] == "servers.yml interrupted"
    stubs.release_gate()


# --------------------------------------------------------------------------- silence


def nothing_else_sent(client, stubs, fake_http, before):
    """Nothing was sent for what came before: a failing sentinel Run's message is the next one ntfy gets."""
    previews = {**stubs.scenario.get("previews", {}), "sentinel": preview_for(["gamma"])}
    stubs.script(runner=FAILING, previews=previews)
    wait_for_run(client, launch(client, limit="sentinel"))
    wait_until(lambda: len(sent(fake_http)) > before)
    assert [m["title"] for m in sent(fake_http)[before:]] == ["servers.yml failed"]


def test_a_successful_hand_started_run_stays_silent(app, stubs, fake_http):
    stubs.script(runner={"events": [
        task("Install packages", "t1"), changed("alpha", "t1"), stats(alpha=(1, 1, 0, 0, 0)),
    ]})

    run = wait_for_run(app, launch(app))
    assert run["status"] == "succeeded"
    assert run["recap"]["alpha"]["changed"] == 1

    nothing_else_sent(app, stubs, fake_http, before=0)
    assert app.get(f"/api/runs/{run['id']}").json()["notification"] is None


# --------------------------------------------------------------------------- Schedules


@pytest.fixture
def sched_app(notify_app):
    def start(**overrides):
        return notify_app(time_zone="America/New_York", scheduler_interval_seconds=0.02, **overrides)

    return start


CHANGES = {
    "events": [
        task("Install packages", "t1"),
        changed("alpha", "t1"),
        changed("beta", "t1"),
        stats(alpha=(1, 1, 0, 0, 0), beta=(1, 1, 0, 0, 0)),
    ],
}


def test_a_missed_schedule_firing_notifies_at_priority_4(sched_app, stop_app, clock, fake_http):
    clock.set(local(2026, 11, 25, 2, 0))
    first = sched_app()
    create(first, name="nightly drift", cron="0 3 * * *")
    tick(first, clock)
    stop_app(first)

    clock.set(local(2026, 11, 25, 4, 1))
    second = sched_app()
    tick(second, clock)

    [entry] = entries(second)
    assert entry["status"] == "missed"
    wait_until(lambda: len(sent(fake_http)) == 1)
    [message] = sent(fake_http)
    assert message["priority"] == 4
    assert message["title"] == "nightly drift missed"
    assert message["click"] == f"{PUBLIC_URL}/runs/{entry['id']}"


def test_a_scheduled_run_with_changes_notifies_at_priority_3(sched_app, clock, stubs, fake_http):
    stubs.script(runner=CHANGES)
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="0 3 * * *")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    assert wait_for_run(app, entry["id"])["status"] == "succeeded"
    wait_until(lambda: len(sent(fake_http)) == 1)
    [message] = sent(fake_http)
    assert message["priority"] == 3
    assert message["title"] == "servers.yml: changes on 2 hosts"


def test_a_scheduled_run_with_changes_stays_silent_when_its_schedule_has_notify_on_changes_off(
    sched_app, clock, stubs, fake_http
):
    stubs.script(runner=CHANGES)
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="0 3 * * *", notify_on_changes=False)
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    assert wait_for_run(app, entry["id"])["status"] == "succeeded"
    nothing_else_sent(app, stubs, fake_http, before=0)


def test_a_quiet_scheduled_run_stays_silent(sched_app, clock, stubs, fake_http):
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="0 3 * * *")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    assert wait_for_run(app, entry["id"])["status"] == "succeeded"
    nothing_else_sent(app, stubs, fake_http, before=0)


def test_a_failed_scheduled_run_notifies_as_failed_even_with_notify_on_changes_off(
    sched_app, clock, stubs, fake_http
):
    stubs.script(runner=FAILING)
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="0 3 * * *", notify_on_changes=False)
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    assert wait_for_run(app, entry["id"])["status"] == "failed"
    wait_until(lambda: len(sent(fake_http)) == 1)
    assert sent(fake_http)[0]["title"] == "servers.yml failed"
    assert sent(fake_http)[0]["priority"] == 4


def test_a_skipped_firing_stays_silent(sched_app, clock, stubs, fake_http):
    stubs.script(previews={"alpha": preview_for(["alpha"])}, runner_by_limit={"alpha": BLOCKED})
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="*/5 * * * *", limit="alpha")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)
    [first] = entries(app)
    wait_until(lambda: app.get(f"/api/runs/{first['id']}").json()["status"] == "running")

    clock.set(local(2026, 11, 25, 3, 5))
    tick(app, clock)
    assert entries(app)[1]["status"] == "skipped"

    nothing_else_sent(app, stubs, fake_http, before=0)
    stubs.release_gate("slow")
    wait_for_run(app, first["id"])


# --------------------------------------------------------------------------- content


def test_the_message_body_has_what_is_needed_to_triage_and_no_log_lines(app, stubs, fake_http, remote, clock):
    sha = remote.sha()
    stubs.script(runner={
        "events": [
            task("Gathering Facts", "t1"),
            result("runner_on_ok", "alpha", "t1"),
            result("runner_on_unreachable", "gamma", "t1"),
            task("Install packages", "t2"),
            changed("alpha", "t2"),
            result("runner_on_failed", "beta", "t2", res={"msg": "No package matching 'nope' found"}),
            task("Configure", "t3"),
            result("runner_on_failed", "alpha", "t3"),
            stats(alpha=(2, 1, 0, 1, 0), beta=(0, 0, 0, 1, 0), gamma=(0, 0, 0, 0, 1)),
        ],
        "rc": 2,
    })

    run = wait_for_run(app, launch(app, check=False))
    wait_until(lambda: len(sent(fake_http)) == 1)

    [message] = sent(fake_http)
    assert message["message"] == "\n".join([
        "Source: ui",
        "Check mode: no",
        f"Commit: {sha[:7]} (main)",
        "Duration: 0s",  # the injected clock stood still
        "Failed (2): alpha (1), beta (1)",
        "Unreachable (1): gamma (1)",
        "Changed (1): alpha (1)",
        "First failed task: Install packages on beta",
    ])
    assert "nope" not in json.dumps(message)
    assert run["first_failed_task"] == {"name": "Install packages", "hosts": ["beta"]}


def test_the_duration_and_schedule_are_in_a_scheduled_runs_message(sched_app, clock, stubs, fake_http, remote):
    stubs.script(runner={**CHANGES, "delay": 0})
    app = sched_app()
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, name="nightly drift", cron="0 3 * * *")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)
    [entry] = entries(app)
    wait_for_run(app, entry["id"])

    wait_until(lambda: len(sent(fake_http)) == 1)
    body = sent(fake_http)[0]["message"].splitlines()
    assert body[:3] == ["Source: schedule (nightly drift)", "Check mode: yes", f"Commit: {remote.sha()[:7]} (main)"]
    assert "Changed (2): alpha (1), beta (1)" in body


def test_a_working_copy_run_says_working_copy_instead_of_a_sha(start_app, fake_http, stubs, working_copy):
    stubs.script(runner=FAILING)
    app = start_app(
        ntfy_url=fake_http.url, ntfy_topic=TOPIC, ntfy_token=TOKEN, working_copy_path=working_copy.path,
        public_hostname=None,
    )

    run = wait_for_run(app, launch(app, checkout="working_copy"))
    assert run["status"] == "failed"
    wait_until(lambda: len(sent(fake_http)) == 1)
    [message] = sent(fake_http)
    assert "Commit: working copy" in message["message"].splitlines()
    assert "click" not in message  # no public URL configured: nothing to click through to


def test_a_missed_entry_says_which_firings_were_missed(sched_app, stop_app, clock, fake_http):
    clock.set(local(2026, 11, 25, 2, 0))
    first = sched_app()
    create(first, name="nightly drift", cron="0 3 * * *")
    tick(first, clock)
    stop_app(first)
    clock.set(local(2026, 11, 25, 4, 1))
    second = sched_app()
    tick(second, clock)

    wait_until(lambda: len(sent(fake_http)) == 1)
    assert sent(fake_http)[0]["message"] == "\n".join([
        "Playbook: servers.yml",
        "locomotive was not running: 1 firing missed, the newest at 2026-11-25 03:00 EST (more than 1h ago)",
    ])


# --------------------------------------------------------------------------- delivery


def test_a_failed_delivery_is_retried_with_backoff_until_ntfy_takes_it(notify_app, stubs, fake_http):
    app = notify_app(ntfy_retry_seconds=0.1)
    fake_http.statuses = [500, 502, 503]
    stubs.script(runner=FAILING)

    run_id = launch(app)
    wait_until(lambda: (app.get(f"/api/runs/{run_id}").json()["notification"] or {}).get("status") == "sent")

    attempts = [r for r in fake_http.requests if r.method == "POST"]
    assert len(attempts) == 4  # the first try and 3 retries
    gaps = [b.at - a.at for a, b in zip(attempts, attempts[1:])]
    assert gaps[0] >= 0.1 and gaps[1] >= 0.2 and gaps[2] >= 0.4
    run = app.get(f"/api/runs/{run_id}").json()
    assert run["notification"] == {"status": "sent", "attempts": 4, "error": None}
    assert run["status"] == "failed"


def test_a_delivery_that_keeps_failing_is_recorded_on_the_run_and_never_changes_its_outcome(
    app, stubs, fake_http
):
    fake_http.status = 500
    stubs.script(runner=FAILING)

    run_id = launch(app)
    wait_until(lambda: (app.get(f"/api/runs/{run_id}").json()["notification"] or {}).get("status") == "failed")

    run = app.get(f"/api/runs/{run_id}").json()
    assert run["notification"] == {"status": "failed", "attempts": 4, "error": "ntfy answered HTTP 500"}
    assert run["status"] == "failed"
    assert run["failure_reason"] == "ansible-playbook failed (exit code 2)"
    assert len([r for r in fake_http.requests if r.method == "POST"]) == 4


def test_an_unreachable_ntfy_is_recorded_without_its_url_or_token(start_app, stubs):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_url = f"http://127.0.0.1:{s.getsockname()[1]}/secret-path"
    app = start_app(ntfy_url=dead_url, ntfy_topic=TOPIC, ntfy_token=TOKEN, ntfy_retry_seconds=0.01)
    stubs.script(runner={"events": [], "rc": 3})

    run_id = launch(app)
    wait_until(lambda: (app.get(f"/api/runs/{run_id}").json()["notification"] or {}).get("status") == "failed")

    run = app.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["notification"]["attempts"] == 4
    assert run["notification"]["error"].startswith("could not reach ntfy")
    shown = json.dumps(app.get("/api/runs").json())
    assert TOKEN not in shown and "secret-path" not in shown


def test_the_ntfy_token_never_appears_in_any_response(app, stubs, fake_http):
    stubs.script(runner=FAILING)
    run_id = launch(app)
    wait_until(lambda: len(sent(fake_http)) == 1)

    for path in ("/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/log", "/api/scheduler", "/api/schedules"):
        assert TOKEN not in app.get(path).text
    assert TOKEN not in json.dumps(sent(fake_http))


def test_without_ntfy_configured_nothing_is_sent_and_runs_are_unaffected(start_app, stubs, fake_http):
    app = start_app()
    stubs.script(runner=FAILING)

    run = wait_for_run(app, launch(app))

    assert run["status"] == "failed"
    assert app.get(f"/api/runs/{run['id']}").json()["notification"] is None
    assert fake_http.requests == []


def test_without_a_public_url_the_click_through_uses_the_public_hostname(start_app, fake_http, stubs):
    app = start_app(ntfy_url=fake_http.url, ntfy_topic=TOPIC, ntfy_token=TOKEN, ntfy_retry_seconds=0.01)
    stubs.script(runner=FAILING)

    run_id = launch(app)
    wait_until(lambda: len(sent(fake_http)) == 1)
    assert sent(fake_http)[0]["click"] == f"https://{PUBLIC_HOSTNAME}/runs/{run_id}"


# --------------------------------------------------------------------------- MCP Runs


def test_a_successful_mcp_run_stays_silent_and_a_failed_one_notifies(app, stubs, fake_http):
    mcp = McpCaller(str(app.base_url), LXC_TOKEN)
    stubs.script(runner={"events": [
        task("Install packages", "t1"), changed("alpha", "t1"), stats(alpha=(1, 1, 0, 0, 0)),
    ]})
    quiet = mcp.ok("start_run", playbook="servers.yml")
    assert wait_for_run(app, quiet["id"])["status"] == "succeeded"

    stubs.script(runner=FAILING)
    failing = mcp.ok("start_run", playbook="servers.yml")
    assert wait_for_run(app, failing["id"])["status"] == "failed"

    wait_until(lambda: len(sent(fake_http)) == 1)
    [message] = sent(fake_http)
    assert message["title"] == "servers.yml failed"
    assert message["priority"] == 4
    assert message["message"].splitlines()[0] == "Source: mcp:lxc"
    assert app.get(f"/api/runs/{quiet['id']}").json()["notification"] is None

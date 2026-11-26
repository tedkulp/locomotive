"""Schedules: saved Run settings with a cron expression; each firing produces one clean Run.

Time is driven by the injected clock. The scheduler loop ticks often in tests (every few ms of real
time) and always evaluates the clock's current time; ``tick`` waits until a tick has seen it.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import preview_for, wait_for_run, wait_until

NY = ZoneInfo("America/New_York")


def local(*args: int) -> datetime:
    """A New York wall-clock time as an aware datetime (fold=0: the first of a repeated time)."""
    return datetime(*args, tzinfo=NY)


def iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def sched_app(start_app):
    def start(**overrides):
        return start_app(time_zone="America/New_York", scheduler_interval_seconds=0.02, **overrides)

    return start


@pytest.fixture
def app(sched_app):
    return sched_app()


def tick(client, clock):
    """Wait until the scheduler has evaluated the clock's current time."""
    now = iso(clock.now())
    wait_until(lambda: client.get("/api/scheduler").json()["last_tick_at"] == now)


def create(client, **fields):
    body = {"name": "nightly drift", "playbook": "servers.yml", "cron": "0 3 * * *", **fields}
    resp = client.post("/api/schedules", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def entries(client):
    """History, oldest first."""
    return list(reversed(client.get("/api/runs").json()["runs"]))


# --------------------------------------------------------------------------- CRUD


def test_a_schedule_is_created_with_its_run_settings_and_toggles_default_on(app, clock):
    clock.set(local(2026, 11, 25, 12, 0))

    created = create(
        app, limit="alpha", tags=["base"], check=False, diff=True, extra_vars={"x": 1}, branch="dev",
    )

    assert created["name"] == "nightly drift"
    assert created["playbook"] == "servers.yml"
    assert created["cron"] == "0 3 * * *"
    assert created["limit"] == "alpha"
    assert created["tags"] == ["base"]
    assert created["check"] is False
    assert created["diff"] is True
    assert created["extra_vars"] == {"x": 1}
    assert created["branch"] == "dev"
    assert created["enabled"] is True
    assert created["notify_on_changes"] is True
    assert created["next_fire_at"] == iso(local(2026, 11, 26, 3, 0))
    listed = app.get("/api/schedules").json()
    assert [s["id"] for s in listed["schedules"]] == [created["id"]]
    assert listed["time_zone"] == "America/New_York"


def test_a_schedule_can_be_edited_toggled_and_deleted(app, clock):
    clock.set(local(2026, 11, 25, 12, 0))
    sid = create(app)["id"]

    resp = app.patch(f"/api/schedules/{sid}", json={"cron": "30 4 * * 1", "name": "weekly", "enabled": False})
    assert resp.status_code == 200, resp.text
    edited = resp.json()
    assert (edited["cron"], edited["name"], edited["enabled"]) == ("30 4 * * 1", "weekly", False)
    assert edited["next_fire_at"] is None  # disabled
    assert app.patch(f"/api/schedules/{sid}", json={"notify_on_changes": False}).json()["notify_on_changes"] is False
    assert app.get(f"/api/schedules/{sid}").json()["name"] == "weekly"

    assert app.delete(f"/api/schedules/{sid}").status_code == 204
    assert app.get(f"/api/schedules/{sid}").status_code == 404
    assert app.get("/api/schedules").json()["schedules"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {"cron": "not a cron"},
        {"cron": "0 3 * * * *"},  # no seconds field
        {"playbook": "mac.yml"},  # hidden on this Controller
        {"branch": "../evil"},
        {"name": ""},
        {"working_copy": True},  # unknown options are rejected, never ignored
    ],
)
def test_invalid_schedules_are_rejected(app, fields):
    body = {"name": "n", "playbook": "servers.yml", "cron": "0 3 * * *", **fields}
    assert app.post("/api/schedules", json=body).status_code == 422


# --------------------------------------------------------------------------- firing


def test_a_firing_produces_one_clean_run_of_the_saved_settings_labelled_with_its_schedule(
    app, clock, remote, stubs
):
    stubs.script(previews={"alpha": preview_for(["alpha"])})
    dev_sha = remote.commit({"README.md": "on dev\n"}, branch="dev")
    clock.set(local(2026, 11, 25, 2, 59))
    schedule = create(
        app, limit="alpha", tags=["base"], check=False, extra_vars={"x": 1}, branch="dev", cron="0 3 * * *",
    )
    tick(app, clock)
    assert entries(app) == []

    clock.set(local(2026, 11, 25, 3, 0, 10))
    tick(app, clock)

    [entry] = entries(app)
    run = wait_for_run(app, entry["id"])
    assert run["status"] == "succeeded", run["failure_reason"]
    assert run["source"] == "schedule"
    assert run["schedule"] == {"id": schedule["id"], "name": "nightly drift"}
    assert run["checkout"] == "clean"
    assert (run["branch"], run["sha"]) == ("dev", dev_sha)
    assert (run["limit"], run["tags"], run["check"], run["extra_vars"]) == ("alpha", ["base"], False, {"x": 1})
    [runner] = stubs.calls("ansible-runner")
    assert runner["cmdline"] == '--limit alpha --tags base --extra-vars \'{"x": 1}\''

    # It fires once per firing, not once per tick.
    clock.advance(seconds=40)
    tick(app, clock)
    assert len(entries(app)) == 1
    shown = app.get(f"/api/schedules/{schedule['id']}").json()
    assert shown["last_run"] == {"id": run["id"], "status": "succeeded"}
    assert shown["next_fire_at"] == iso(local(2026, 11, 26, 3, 0))


def test_a_schedule_without_a_branch_runs_the_default_branch(app, clock, remote):
    clock.set(local(2026, 11, 25, 2, 59))
    create(app)
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    run = wait_for_run(app, entry["id"])
    assert (run["branch"], run["sha"], run["checkout"]) == ("main", remote.sha("main"), "clean")


BLOCKED = {
    "events": [
        {"event": "playbook_on_task_start", "stdout": "TASK [ping] ***"},
        {"event": "runner_on_ok", "stdout": "ok: [alpha]", "event_data": {"host": "alpha"}},
    ],
    "gate_after": 1,
    "gate": "slow",
}


def test_a_firing_while_the_schedules_previous_run_is_running_is_recorded_as_skipped(app, clock, stubs):
    stubs.script(previews={"alpha": preview_for(["alpha"])}, runner_by_limit={"alpha": BLOCKED})
    clock.set(local(2026, 11, 25, 2, 59))
    create(app, cron="*/5 * * * *", limit="alpha")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)
    [first] = entries(app)
    wait_until(lambda: app.get(f"/api/runs/{first['id']}").json()["status"] == "running")

    clock.set(local(2026, 11, 25, 3, 5))
    tick(app, clock)

    first_run, skipped = entries(app)
    assert first_run["status"] == "running"
    assert skipped["status"] == "skipped"
    assert skipped["source"] == "schedule"
    assert skipped["schedule"]["name"] == "nightly drift"
    assert skipped["failure_reason"] == f"the Schedule's previous Run #{first['id']} is still running"
    assert skipped["started_at"] is None and skipped["sha"] is None
    log = [line["text"] for line in app.get(f"/api/runs/{skipped['id']}/log").json()["lines"]]
    assert any("Skipped" in text for text in log)
    assert len(stubs.calls("ansible-runner")) == 1

    stubs.release_gate("slow")
    assert wait_for_run(app, first["id"])["status"] == "succeeded"
    clock.set(local(2026, 11, 25, 3, 10))
    tick(app, clock)
    assert entries(app)[-1]["status"] in ("queued", "running", "succeeded")


def test_a_firing_while_the_schedules_previous_run_is_queued_is_skipped_but_other_conflicts_queue(
    app, clock, stubs
):
    stubs.script(previews={"alpha": preview_for(["alpha"])}, runner_by_limit={"alpha": BLOCKED})
    clock.set(local(2026, 11, 25, 2, 59))
    hand_started = app.post("/api/runs", json={"playbook": "servers.yml", "limit": "alpha"}).json()["id"]
    wait_until(lambda: app.get(f"/api/runs/{hand_started}").json()["status"] == "running")
    create(app, cron="*/5 * * * *", limit="alpha")

    # The Schedule's own Run conflicts with the hand-started one: it queues.
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)
    scheduled = entries(app)[-1]
    wait_until(lambda: app.get(f"/api/runs/{scheduled['id']}").json()["waiting_for"] is not None)

    # The next firing finds that Run still queued: skipped.
    clock.set(local(2026, 11, 25, 3, 5))
    tick(app, clock)
    skipped = entries(app)[-1]
    assert skipped["status"] == "skipped"
    assert skipped["failure_reason"] == f"the Schedule's previous Run #{scheduled['id']} is still queued"

    stubs.release_gate("slow")
    assert wait_for_run(app, scheduled["id"])["status"] == "succeeded"


# --------------------------------------------------------------------------- missed firings


def test_firings_missed_while_down_collapse_into_one_run_if_the_newest_is_within_an_hour(
    sched_app, stop_app, clock, stubs
):
    clock.set(local(2026, 11, 25, 1, 0))
    first_app = sched_app()
    create(first_app, cron="*/15 * * * *")
    tick(first_app, clock)
    stop_app(first_app)

    # Down from 01:00 to 03:50: eleven firings missed, the newest (03:45) five minutes ago.
    clock.set(local(2026, 11, 25, 3, 50))
    second_app = sched_app()
    tick(second_app, clock)

    [entry] = entries(second_app)
    run = wait_for_run(second_app, entry["id"])
    assert run["status"] == "succeeded"
    assert run["source"] == "schedule"
    log = [line["text"] for line in second_app.get(f"/api/runs/{run['id']}/log").json()["lines"]]
    assert "Schedule 'nightly drift' (*/15 * * * *) came due at 2026-11-25 03:45 EST; fired a clean Run of main" in log


def test_firings_missed_more_than_an_hour_ago_leave_one_missed_entry_and_no_run(
    sched_app, stop_app, clock, stubs
):
    clock.set(local(2026, 11, 25, 2, 0))
    first_app = sched_app()
    create(first_app, cron="0 3 * * *")
    tick(first_app, clock)
    stop_app(first_app)

    clock.set(local(2026, 11, 25, 4, 1))
    second_app = sched_app()
    tick(second_app, clock)

    [entry] = entries(second_app)
    assert entry["status"] == "missed"
    assert entry["source"] == "schedule"
    assert entry["schedule"]["name"] == "nightly drift"
    assert entry["failure_reason"] == (
        "locomotive was not running: 1 firing missed, the newest at 2026-11-25 03:00 EST (more than 1h ago)"
    )
    assert entry["finished_at"] == iso(local(2026, 11, 25, 4, 1))
    assert stubs.calls("ansible-runner") == []

    # The Schedule carries on from now: the next firing runs normally.
    clock.set(local(2026, 11, 26, 3, 0))
    tick(second_app, clock)
    assert [e["status"] for e in entries(second_app)][0] == "missed"
    assert wait_for_run(second_app, entries(second_app)[1]["id"])["status"] == "succeeded"


# --------------------------------------------------------------------------- DST (America/New_York, 2026)


def walk(client, clock, start: datetime, end: datetime, step=timedelta(minutes=10)):
    """Move the clock from ``start`` to ``end`` in steps, letting the scheduler see each one."""
    when = start
    while when <= end:
        clock.set(when)
        tick(client, clock)
        when += step


def test_a_time_skipped_by_the_spring_forward_does_not_fire(app, clock):
    clock.set(local(2026, 3, 7, 12, 0))
    create(app, cron="30 2 * * *")

    # 8 March: 02:00 EST jumps to 03:00 EDT, so 02:30 never happens.
    walk(app, clock, datetime(2026, 3, 8, 6, 0, tzinfo=UTC), datetime(2026, 3, 8, 9, 0, tzinfo=UTC))
    assert entries(app) == []

    clock.set(local(2026, 3, 9, 2, 30))
    tick(app, clock)
    [entry] = entries(app)
    assert entry["created_at"] == iso(local(2026, 3, 9, 2, 30))


def test_a_time_repeated_by_the_fall_back_fires_once(app, clock):
    clock.set(local(2026, 10, 31, 12, 0))
    create(app, cron="30 1 * * *")

    # 1 November: 01:00-02:00 happens twice (EDT, then EST). Walk 04:00Z-08:00Z through both.
    walk(app, clock, datetime(2026, 11, 1, 4, 0, tzinfo=UTC), datetime(2026, 11, 1, 8, 0, tzinfo=UTC))
    [entry] = entries(app)
    assert entry["status"] != "skipped"
    assert entry["created_at"] == "2026-11-01T05:30:00Z"  # 01:30 EDT, the first occurrence

    clock.set(local(2026, 11, 2, 1, 30))
    tick(app, clock)
    assert len(entries(app)) == 2


# --------------------------------------------------------------------------- pause and enable


def test_firings_during_a_global_pause_are_dropped_without_a_record(app, clock):
    clock.set(local(2026, 11, 25, 2, 0))
    create(app, cron="*/30 * * * *")

    resp = app.put("/api/scheduler", json={"paused": True})
    assert resp.status_code == 200
    assert resp.json()["paused"] is True
    assert app.get("/api/schedules").json()["paused"] is True

    walk(app, clock, local(2026, 11, 25, 2, 10), local(2026, 11, 25, 3, 10))
    assert entries(app) == []

    # Resuming doesn't catch up on what came due during the pause.
    assert app.put("/api/scheduler", json={"paused": False}).json()["paused"] is False
    tick(app, clock)
    assert entries(app) == []

    clock.set(local(2026, 11, 25, 3, 30))
    tick(app, clock)
    [entry] = entries(app)
    assert entry["created_at"] == iso(local(2026, 11, 25, 3, 30))


def test_the_pause_survives_a_restart_and_drops_firings_missed_meanwhile(sched_app, stop_app, clock):
    clock.set(local(2026, 11, 25, 2, 0))
    first_app = sched_app()
    create(first_app, cron="0 3 * * *")
    first_app.put("/api/scheduler", json={"paused": True})
    stop_app(first_app)

    clock.set(local(2026, 11, 25, 3, 10))
    second_app = sched_app()
    tick(second_app, clock)
    assert second_app.get("/api/scheduler").json()["paused"] is True
    assert entries(second_app) == []


def test_a_disabled_schedule_does_not_fire_and_re_enabling_does_not_backfill(app, clock):
    clock.set(local(2026, 11, 25, 2, 0))
    sid = create(app, cron="0 3 * * *", enabled=False)["id"]

    walk(app, clock, local(2026, 11, 25, 2, 50), local(2026, 11, 25, 3, 20))
    assert entries(app) == []
    app.patch(f"/api/schedules/{sid}", json={"enabled": True})
    tick(app, clock)
    assert entries(app) == []

    clock.set(local(2026, 11, 26, 3, 0))
    tick(app, clock)
    assert len(entries(app)) == 1


# --------------------------------------------------------------------------- Scheduler Ping


def test_the_scheduler_loop_pings_healthchecks_every_five_minutes(sched_app, clock, fake_http):
    clock.set(local(2026, 11, 25, 12, 0))
    app = sched_app(healthchecks_url=f"{fake_http.url}/ping/check-uuid")

    tick(app, clock)
    wait_until(lambda: len(fake_http.requests) == 1)
    assert fake_http.requests[0].path == "/ping/check-uuid"

    walk(app, clock, local(2026, 11, 25, 12, 1), local(2026, 11, 25, 12, 4), step=timedelta(minutes=1))
    assert len(fake_http.requests) == 1

    clock.set(local(2026, 11, 25, 12, 5))
    tick(app, clock)
    assert len(fake_http.requests) == 2
    status = app.get("/api/scheduler").json()
    assert status["last_ping_at"] == iso(local(2026, 11, 25, 12, 5))
    assert "check-uuid" not in str(status)  # the ping URL is a secret


def test_a_failed_ping_is_retried_on_the_next_tick_and_never_stops_the_scheduler(sched_app, clock, fake_http):
    fake_http.status = 503
    clock.set(local(2026, 11, 25, 2, 59))
    app = sched_app(healthchecks_url=f"{fake_http.url}/ping/check-uuid")
    create(app)
    tick(app, clock)
    assert "503" in app.get("/api/scheduler").json()["last_ping_error"]

    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)  # the loop runs one tick at a time: from here on every tick reads 03:00
    assert len(entries(app)) == 1  # the failing ping didn't stop the Schedule firing
    assert "503" in app.get("/api/scheduler").json()["last_ping_error"]

    fake_http.status = 200
    wait_until(lambda: app.get("/api/scheduler").json()["last_ping_error"] is None)
    status = app.get("/api/scheduler").json()
    assert status["last_ping_at"] == iso(local(2026, 11, 25, 3, 0))


def test_a_schedule_whose_playbook_became_hidden_fails_its_firing_without_running(
    sched_app, stop_app, clock, stubs
):
    clock.set(local(2026, 11, 25, 2, 0))
    first_app = sched_app()
    create(first_app, playbook="docker.yml")
    stop_app(first_app)

    clock.set(local(2026, 11, 25, 3, 0))
    second_app = sched_app(hidden_playbooks=["docker.yml"])
    tick(second_app, clock)

    [entry] = entries(second_app)
    assert entry["status"] == "failed"
    assert entry["failure_reason"] == "Playbook docker.yml is hidden on this Controller"
    assert stubs.calls("ansible-runner") == []


def test_scheduled_runs_are_clean_even_when_a_dirty_working_copy_is_configured(
    sched_app, clock, remote, stubs, working_copy
):
    working_copy.write({"playbooks/servers.yml": "- hosts: servers\n  tasks: [{debug: {msg: wip}}]\n"})
    clock.set(local(2026, 11, 25, 2, 59))
    app = sched_app(working_copy_path=working_copy.path)
    create(app)
    clock.set(local(2026, 11, 25, 3, 0))
    tick(app, clock)

    [entry] = entries(app)
    run = wait_for_run(app, entry["id"])
    assert (run["checkout"], run["sha"], run["dirty"]) == ("clean", remote.sha("main"), None)
    [runner] = stubs.calls("ansible-runner")
    assert str(working_copy.path) not in runner["cwd"]

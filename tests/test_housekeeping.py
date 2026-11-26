"""Log retention: the per-Run log cap, gzip of finished logs, Keep, age and budget pruning by the daily
housekeeping pass (04:00 local and at startup), and what a pruned Run still shows."""

import pytest

from conftest import wait_for_run, wait_until
from test_run_grid import banner_of, changed, result, stats, task
from test_schedules import create, iso, local, tick

GIB = 1024**3


@pytest.fixture
def hk_app(start_app):
    """The app with New York local time and a housekeeping loop that looks at the clock often."""

    def start(**overrides):
        values = {
            "time_zone": "America/New_York",
            "scheduler_interval_seconds": 0.02,
            "housekeeping_interval_seconds": 0.02,
            **overrides,
        }
        return start_app(**values)

    return start


def passes(client):
    """Housekeeping passes, newest first."""
    return client.get("/api/housekeeping").json()["passes"]


def await_pass(client, count):
    """Wait until ``count`` passes are recorded (across restarts: they share the database) and the newest
    has finished; the newest."""
    wait_until(lambda: len(passes(client)) >= count)
    wait_until(lambda: passes(client)[0]["finished_at"] is not None)
    return passes(client)[0]


def checked(client, clock):
    """Wait until housekeeping has looked at the clock's current time."""
    now = iso(clock.now())
    wait_until(lambda: client.get("/api/housekeeping").json()["last_check_at"] == now)


def chatty(lines: int, width: int = 200) -> dict:
    """A Run whose one task prints ``lines`` long lines on alpha, then changes it."""
    noise = [
        result("runner_on_ok", "alpha", "t1", res={}) | {"stdout": f"ok: [alpha] {i:06d} " + "x" * width}
        for i in range(lines)
    ]
    return {
        "events": [
            task("Chatter", "t1"),
            *noise,
            task("Change it", "t2"),
            changed("alpha", "t2"),
            stats(alpha=(lines + 1, 1, 0, 0, 0)),
        ],
    }


def launch(client, **body):
    resp = client.post("/api/runs", json={"playbook": "servers.yml", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def log_texts(client, run_id):
    return [line["text"] for line in client.get(f"/api/runs/{run_id}/log").json()["lines"]]


# --------------------------------------------------------------------------- log cap


def test_a_log_stops_at_the_cap_while_the_run_carries_on_with_a_correct_grid_and_recap(start_app, stubs):
    client = start_app(log_cap_bytes=20_000)
    stubs.script(runner=chatty(lines=400))

    run = wait_for_run(client, launch(client))

    assert run["status"] == "succeeded"
    assert run["log_truncated"] is True
    assert run["log_state"] == "truncated"
    texts = log_texts(client, run["id"])
    assert sum(len(t) for t in texts) < 25_000
    assert any(t.startswith("Log truncated") for t in texts)
    assert not any("000399" in t for t in texts)  # the last chatter line came after the cap
    assert texts[-1] == "Run succeeded"  # locomotive's own lines still go in
    assert run["recap"]["alpha"] == {"ok": 401, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0}
    grid = client.get(f"/api/runs/{run['id']}/grid").json()
    assert [t["name"] for t in grid["tasks"]] == ["Chatter", "Change it"]


def test_tasks_whose_banner_came_after_the_cap_have_no_log_position(start_app, stubs):
    client = start_app(log_cap_bytes=20_000)
    stubs.script(runner=chatty(lines=400))

    run = wait_for_run(client, launch(client))

    tasks = client.get(f"/api/runs/{run['id']}/grid").json()["tasks"]
    lines = client.get(f"/api/runs/{run['id']}/log").json()["lines"]
    chatter, change_it = tasks
    assert banner_of(lines, chatter["log_seq"]) == "TASK [Chatter] *****"
    assert change_it["log_seq"] is None


def _disk_bytes(*dirs) -> int:
    return sum(p.stat().st_size for d in dirs if d.exists() for p in d.rglob("*") if p.is_file())


def test_the_log_cap_bounds_the_runs_disk_use_while_it_runs(start_app, stubs, data_dir):
    cap = 50_000
    client = start_app(log_cap_bytes=cap)
    scenario = chatty(lines=3000)  # about 4 MB of output from Ansible
    stubs.script(runner={**scenario, "gate_after": len(scenario["events"]) - 1})  # held before the stats
    run_id = launch(client)

    def all_folded():
        tasks = client.get(f"/api/runs/{run_id}/grid").json()["tasks"]
        return len(tasks) == 2 and tasks[1]["results"].get("alpha") == "changed"

    wait_until(all_folded, timeout=30)
    used = _disk_bytes(data_dir / "history" / str(run_id), data_dir / "runs" / str(run_id))
    assert used < 2 * cap, used  # the log (at the cap), the grid and a little bookkeeping
    stubs.release_gate()
    run = wait_for_run(client, run_id)
    assert run["status"] == "succeeded" and run["log_truncated"] is True
    assert run["recap"]["alpha"] == {"ok": 3001, "changed": 1, "skipped": 0, "failed": 0, "unreachable": 0}
    assert [t["name"] for t in client.get(f"/api/runs/{run_id}/grid").json()["tasks"]] == ["Chatter", "Change it"]
    assert run["log_bytes"] < 2 * cap


def test_a_log_under_the_cap_is_complete(client, stubs):
    stubs.script(runner=chatty(lines=50))

    run = wait_for_run(client, launch(client))

    assert run["log_truncated"] is False
    assert run["log_state"] == "complete"
    assert any("000049" in t for t in log_texts(client, run["id"]))


# --------------------------------------------------------------------------- gzip


def test_a_finished_log_is_gzip_compressed_and_still_reads_in_full(client, stubs):
    stubs.script(runner=chatty(lines=1000))

    run = wait_for_run(client, launch(client))

    texts = log_texts(client, run["id"])
    raw_size = sum(len(t) for t in texts)
    assert raw_size > 200_000
    assert any("000999" in t for t in texts)
    assert texts[-1] == "Run succeeded"
    # Its log, events and grid together take a fraction of the plain log's size.
    assert 0 < run["log_bytes"] < raw_size / 3


def test_the_stream_of_a_finished_compressed_run_replays_its_log(client, stubs):
    import json

    from conftest import sse_events

    run_id = wait_for_run(client, launch(client))["id"]

    with client.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        events = list(sse_events(resp.iter_lines()))

    texts = [json.loads(d)["text"] for e, _, d in events if e == "log"]
    assert "ok: [alpha]" in texts
    assert texts[-1] == "Run succeeded"


# --------------------------------------------------------------------------- Keep


def test_keep_is_set_and_cleared_on_a_run(client):
    run_id = launch(client)
    assert client.get(f"/api/runs/{run_id}").json()["keep"] is False

    resp = client.put(f"/api/runs/{run_id}/keep", json={"keep": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["keep"] is True
    assert client.get(f"/api/runs/{run_id}").json()["keep"] is True

    assert client.put(f"/api/runs/{run_id}/keep", json={"keep": False}).json()["keep"] is False
    assert client.put("/api/runs/999/keep", json={"keep": True}).status_code == 404


# --------------------------------------------------------------------------- passes


def test_a_pass_runs_at_startup_and_is_recorded(hk_app, clock):
    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()

    first = await_pass(client, 1)

    assert first["trigger"] == "startup"
    assert first["started_at"] == iso(clock.now())
    assert first["pruned"] == []
    assert first["budget_bytes"] == 10 * GIB
    status = client.get("/api/housekeeping").json()
    assert status["next_pass_at"] == iso(local(2026, 11, 26, 4, 0))
    assert status["budget_bytes"] == 10 * GIB


def test_a_pass_runs_daily_at_four_local_time(hk_app, clock):
    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()
    await_pass(client, 1)

    clock.set(local(2026, 11, 26, 3, 59))
    checked(client, clock)
    assert len(passes(client)) == 1

    clock.set(local(2026, 11, 26, 4, 0))
    daily = await_pass(client, 2)
    assert daily["trigger"] == "daily"
    assert daily["started_at"] == iso(local(2026, 11, 26, 4, 0))

    clock.set(local(2026, 11, 29, 9, 0))  # days without a check collapse into one pass
    await_pass(client, 3)
    clock.set(local(2026, 11, 29, 10, 0))
    checked(client, clock)
    assert len(passes(client)) == 3


# --------------------------------------------------------------------------- age


CHANGES_ALPHA = {"events": [task("Change it", "t1"), changed("alpha", "t1"), stats(alpha=(1, 1, 0, 0, 0))]}


def scheduled_runs(client, clock, stubs):
    """Fire two Schedules at 03:00: one quiet, one that changes alpha. Their Runs, by Schedule name."""
    from conftest import preview_for

    stubs.script(previews={"alpha": preview_for(["alpha"])}, runner_by_limit={"alpha": CHANGES_ALPHA})
    clock.set(local(2026, 11, 25, 2, 59))
    quiet = create(client, name="quiet")
    noisy = create(client, name="noisy", limit="alpha")
    clock.set(local(2026, 11, 25, 3, 0))
    tick(client, clock)
    for schedule in (quiet, noisy):  # no more firings while the clock jumps ahead
        client.patch(f"/api/schedules/{schedule['id']}", json={"enabled": False}).raise_for_status()
    runs = {}
    for entry in client.get("/api/runs").json()["runs"]:
        runs[entry["schedule"]["name"]] = wait_for_run(client, entry["id"])
    return runs


def test_quiet_scheduled_runs_keep_logs_14_days_and_all_others_90(hk_app, clock, stubs):
    clock.set(local(2026, 11, 25, 2, 0))
    client = hk_app()
    await_pass(client, 1)
    runs = scheduled_runs(client, clock, stubs)
    assert runs["noisy"]["recap"]["alpha"]["changed"] == 1
    hand = wait_for_run(client, launch(client))  # finished at 03:00 too
    kept = wait_for_run(client, launch(client))
    client.put(f"/api/runs/{kept['id']}/keep", json={"keep": True}).raise_for_status()
    await_pass(client, 1)

    clock.set(local(2026, 12, 9, 4, 0))  # 14 days and 1 hour
    first = await_pass(client, 2)
    assert [p["run_id"] for p in first["pruned"]] == [runs["quiet"]["id"]]
    assert first["pruned"][0]["reason"] == "age"
    assert client.get(f"/api/runs/{runs['quiet']['id']}").json()["log_pruned"] is True
    for run in (runs["noisy"], hand, kept):
        assert client.get(f"/api/runs/{run['id']}").json()["log_pruned"] is False

    clock.set(local(2027, 2, 22, 4, 0))  # 89 days
    assert await_pass(client, 3)["pruned"] == []

    clock.set(local(2027, 2, 24, 4, 0))  # 91 days
    last = await_pass(client, 4)
    assert sorted(p["run_id"] for p in last["pruned"]) == sorted([runs["noisy"]["id"], hand["id"]])
    assert client.get(f"/api/runs/{kept['id']}").json()["log_pruned"] is False  # Keep exempts
    assert log_texts(client, kept["id"])[-1] == "Run succeeded"


def test_a_pruned_run_keeps_its_summary_and_says_when_its_log_was_pruned(hk_app, clock):
    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()
    run = wait_for_run(client, launch(client))
    await_pass(client, 1)

    clock.set(local(2027, 3, 1, 4, 0))
    await_pass(client, 2)

    pruned = client.get(f"/api/runs/{run['id']}").json()
    assert pruned["log_state"] == "pruned"
    assert pruned["log_pruned_at"] == iso(local(2027, 3, 1, 4, 0))
    for key in ("status", "playbook", "hosts", "sha", "recap", "started_at", "finished_at", "first_failed_task"):
        assert pruned[key] == run[key]
    for path in ("log", "grid"):
        resp = client.get(f"/api/runs/{run['id']}/{path}")
        assert resp.status_code == 410
        assert resp.json()["detail"] == f"Run {run['id']}'s log was pruned on 2027-03-01"
    assert run["id"] in [r["id"] for r in client.get("/api/runs").json()["runs"]]  # still in history


def test_mcp_tail_run_output_on_a_pruned_run_is_a_log_pruned_error(hk_app, clock):
    from conftest import LXC_TOKEN, McpCaller

    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()
    mcp = McpCaller(str(client.base_url), LXC_TOKEN)
    run = wait_for_run(client, launch(client))
    assert mcp.ok("tail_run_output", run_id=run["id"])["lines"]
    await_pass(client, 1)

    clock.set(local(2027, 3, 1, 4, 0))
    await_pass(client, 2)

    error = mcp.error("tail_run_output", run_id=run["id"])
    assert f"log_pruned: Run {run['id']}'s log was pruned on {iso(local(2027, 3, 1, 4, 0))}" in error
    assert mcp.ok("get_run", run_id=run["id"])["log_pruned_at"] == iso(local(2027, 3, 1, 4, 0))


# --------------------------------------------------------------------------- budget


def finished_runs(client, count):
    return [wait_for_run(client, launch(client)) for _ in range(count)]


def test_over_the_budget_the_oldest_unkept_logs_are_pruned_first(hk_app, stop_app, clock):
    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()
    kept, oldest, middle, newest = finished_runs(client, 4)
    client.put(f"/api/runs/{kept['id']}/keep", json={"keep": True}).raise_for_status()
    sizes = {r["id"]: r["log_bytes"] for r in (kept, oldest, middle, newest)}
    await_pass(client, 1)
    stop_app(client)

    # Room for all but the oldest un-Kept Run (and less than the middle one to spare).
    budget = sizes[kept["id"]] + sizes[middle["id"]] + sizes[newest["id"]] + 10
    client = hk_app(log_budget_bytes=budget)
    startup = await_pass(client, 2)  # passes are counted across restarts

    assert startup["pruned"] == [
        {"run_id": oldest["id"], "playbook": "servers.yml", "reason": "budget", "bytes": sizes[oldest["id"]]}
    ]
    assert startup["freed_bytes"] == sizes[oldest["id"]]
    assert startup["total_bytes"] == budget - 10
    assert startup["kept_bytes"] == sizes[kept["id"]]
    assert startup["warning"] is None
    for run in (kept, middle, newest):
        assert client.get(f"/api/runs/{run['id']}").json()["log_pruned"] is False


def test_when_kept_logs_alone_exceed_the_budget_nothing_is_deleted_and_one_warning_is_sent(
    hk_app, stop_app, clock, fake_http
):
    import json

    clock.set(local(2026, 11, 25, 12, 0))
    client = hk_app()
    first, second, unkept = finished_runs(client, 3)
    for run in (first, second):
        client.put(f"/api/runs/{run['id']}/keep", json={"keep": True}).raise_for_status()
    await_pass(client, 1)
    stop_app(client)

    client = hk_app(
        log_budget_bytes=first["log_bytes"], ntfy_url=fake_http.url, ntfy_topic="alerts", ntfy_retry_seconds=0.01
    )
    startup = await_pass(client, 2)

    assert startup["pruned"] == []
    assert startup["warning"].startswith("Kept Runs' logs take")
    assert startup["warning_sent"] is True
    for run in (first, second, unkept):
        assert client.get(f"/api/runs/{run['id']}").json()["log_pruned"] is False
    wait_until(lambda: len(fake_http.requests) == 1)
    message = json.loads(fake_http.requests[0].body)
    assert message["title"] == "Kept logs exceed budget"
    assert message["priority"] == 3
    # New Runs are never refused for disk.
    assert wait_for_run(client, launch(client))["status"] == "succeeded"

    clock.set(local(2026, 11, 26, 4, 0))
    daily = await_pass(client, 3)
    assert daily["warning"] is not None  # still over, still shown on the status page
    assert daily["warning_sent"] is False  # but warned only once
    assert len(fake_http.requests) == 1


def test_the_startup_pass_compresses_a_run_cut_off_by_a_shutdown(hk_app, stop_app, stubs, clock):
    from test_run_log import GATED

    clock.set(local(2026, 11, 25, 12, 0))
    stubs.script(runner=GATED)
    client = hk_app()
    run_id = launch(client)
    wait_until(lambda: "ok: [alpha]" in log_texts(client, run_id))
    await_pass(client, 1)
    stop_app(client)

    client = hk_app()
    startup = await_pass(client, 2)

    assert startup["compressed"] == 1
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "interrupted"
    assert run["log_bytes"] > 0
    assert "ok: [alpha]" in log_texts(client, run_id)

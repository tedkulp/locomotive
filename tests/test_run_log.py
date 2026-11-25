"""The log streams live over SSE; the Run lands in the recent-Runs list and history."""

import json

from conftest import sse_events, wait_for_run, wait_until

GATED = {
    "events": [
        {"event": "playbook_on_play_start", "stdout": "PLAY [servers] ***"},
        {"event": "runner_on_ok", "stdout": "ok: [alpha]", "event_data": {"host": "alpha"}},
        {"event": "runner_on_ok", "stdout": "ok: [beta]", "event_data": {"host": "beta"}},
        {"event": "playbook_on_stats", "stdout": "PLAY RECAP ***"},
    ],
    "gate_after": 2,
}


def test_the_log_streams_live_while_the_run_is_running(client, stubs):
    stubs.script(runner=GATED)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]

    with client.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = sse_events(resp.iter_lines())
        seen = []
        for event, ident, data in events:
            if event == "grid":
                continue
            assert event == "log"
            seen.append(json.loads(data)["text"])
            if "ok: [alpha]" in seen:
                break
        # The Run is blocked mid-Playbook, yet its output already reached us.
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
        assert "PLAY [servers] ***" in seen
        assert "ok: [beta]" not in seen

        stubs.release_gate()
        rest = list(events)

    texts = [json.loads(data)["text"] for event, _, data in rest if event == "log"]
    assert texts.index("ok: [beta]") < texts.index("PLAY RECAP ***")
    end_event, _, end_data = rest[-1]
    assert end_event == "end"
    assert json.loads(end_data)["status"] == "succeeded"


def test_the_stream_of_a_finished_run_replays_its_log_and_ends(client):
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_for_run(client, run_id)

    with client.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        events = list(sse_events(resp.iter_lines()))

    texts = [json.loads(d)["text"] for e, _, d in events if e == "log"]
    assert "Fetching main from the remote" in texts
    assert "ok: [alpha]" in texts
    assert texts[-1] == "Run succeeded"
    assert events[-1][0] == "end"
    ids = [int(i) for e, i, _ in events if e == "log"]
    assert ids == sorted(ids) == list(range(1, len(ids) + 1))


def test_the_stream_resumes_after_last_event_id(client):
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_for_run(client, run_id)

    with client.stream("GET", f"/api/runs/{run_id}/log/stream", headers={"Last-Event-ID": "3"}) as resp:
        ids = [int(i) for e, i, _ in sse_events(resp.iter_lines()) if e == "log"]

    assert ids[0] == 4


def test_a_failure_reason_is_in_the_log(client, stubs):
    stubs.script(galaxy_rc=1, galaxy_output="ERROR! Galaxy is down")
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_for_run(client, run_id)

    texts = [line["text"] for line in client.get(f"/api/runs/{run_id}/log").json()["lines"]]

    assert "ERROR! Galaxy is down" in texts
    assert texts[-1] == "Run failed: Collections install failed: ansible-galaxy exited 1: ERROR! Galaxy is down"


def test_runs_appear_in_the_recent_runs_list_newest_first_while_running(client, stubs):
    first = wait_for_run(client, client.post("/api/runs", json={"playbook": "docker.yml"}).json()["id"])
    stubs.script(runner=GATED)
    second_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_until(lambda: len(stubs.calls("ansible-runner")) == 2)

    runs = client.get("/api/runs").json()["runs"]

    assert [r["id"] for r in runs] == [second_id, first["id"]]
    assert runs[0]["status"] == "running"
    assert runs[0]["playbook"] == "servers.yml"
    assert runs[1]["status"] == "succeeded"
    stubs.release_gate()
    wait_for_run(client, second_id)
    assert client.get("/api/runs", params={"limit": 1}).json()["runs"][0]["status"] == "succeeded"


def test_timings_come_from_the_clock(client, stubs, clock):
    stubs.script(runner=GATED)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    wait_until(lambda: len(stubs.calls("ansible-runner")) == 1)
    clock.advance(minutes=4, seconds=12)
    stubs.release_gate()

    run = wait_for_run(client, run_id)

    assert run["started_at"] == "2026-11-25T12:00:00Z"
    assert run["finished_at"] == "2026-11-25T12:04:12Z"
    assert run["duration_seconds"] == 252


def test_a_run_lasting_hours_keeps_streaming_with_keep_alives_while_quiet(start_app, stubs, clock):
    client = start_app(sse_heartbeat_seconds=0.3)
    stubs.script(runner=GATED)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]

    with client.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        lines = resp.iter_lines()
        for line in lines:
            if "ok: [alpha]" in line:
                break
        clock.advance(hours=6)  # a long, silent task: nothing times the Run out
        keep_alives = 0
        for line in lines:
            if line.startswith(":"):
                keep_alives += 1
                if keep_alives == 2:
                    break
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "running"
        stubs.release_gate()
        rest = list(sse_events(lines))

    assert "ok: [beta]" in [json.loads(d)["text"] for e, _, d in rest if e == "log"]
    assert rest[-1][0] == "end"
    assert json.loads(rest[-1][2])["duration_seconds"] == 6 * 3600

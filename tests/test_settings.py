"""No homelab-specific value lives in the app code: everything comes from settings or env."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import _free_port, wait_for_run, wait_until

APP_SOURCES = [
    *Path(__file__).parent.parent.joinpath("src").rglob("*.py"),
    *Path(__file__).parent.parent.joinpath("frontend", "src").rglob("*.ts*"),
]
HOMELAB = re.compile(
    r"tedkulp|n3rd|int\.tedkulp|192\.168\.|100\.\d+\.\d+\.\d+|ntfy\.sh|hc-ping|"
    r"Tailscale-User|/home/ted|mac\.yml|locomotive\.yml|\bpve\d*\b",
    re.IGNORECASE,
)


def test_app_code_holds_no_homelab_values():
    offenders = [
        f"{path}:{n}: {line.strip()}"
        for path in APP_SOURCES
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if HOMELAB.search(line)
    ]
    assert offenders == []


def _main(env: dict[str, str], port: int) -> subprocess.Popen:
    """``python -m locomotive`` (the real entry point) with only ``env`` as its LOCOMOTIVE_* settings."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("LOCOMOTIVE_")}
    return subprocess.Popen(
        [sys.executable, "-m", "locomotive"],
        env={**clean, **env, "LOCOMOTIVE_HOST": "127.0.0.1", "LOCOMOTIVE_PORT": str(port)},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


@pytest.fixture
def main_app():
    procs: list[subprocess.Popen] = []

    def start(env: dict[str, str]) -> httpx.Client:
        port = _free_port()
        proc = _main(env, port)
        procs.append(proc)
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        deadline = time.monotonic() + 20
        while True:
            try:
                client.get("/api/runs")
                return client
            except httpx.TransportError:
                assert proc.poll() is None, proc.communicate()[0]
                assert time.monotonic() < deadline, "locomotive did not start"
                time.sleep(0.1)

    yield start
    for proc in procs:
        proc.terminate()
        proc.communicate(timeout=10)


def test_every_deployment_value_is_read_from_env(main_app, tmp_path, fake_http, working_copy, stubs):
    token = "mcp-token-0123456789"
    data_dir = tmp_path / "data"
    client = main_app({
        "LOCOMOTIVE_DATA_DIR": str(data_dir),
        "LOCOMOTIVE_REPO_URL": f"{fake_http.url}/acme/infra.git",
        "LOCOMOTIVE_REPO_USERNAME": "bot",
        "LOCOMOTIVE_REPO_TOKEN": "s3cret",
        "LOCOMOTIVE_WORKING_COPY_PATH": str(working_copy.path),
        "LOCOMOTIVE_TRUSTED_PROXY_IPS": "10.0.0.1, 127.0.0.1",
        "LOCOMOTIVE_IDENTITY_HEADER": "X-Who",
        "LOCOMOTIVE_ALLOWLIST": "alice@example.invalid",
        "LOCOMOTIVE_NTFY_URL": f"{fake_http.url}/ntfy",
        "LOCOMOTIVE_NTFY_TOPIC": "ops",
        "LOCOMOTIVE_NTFY_TOKEN": "tk",
        "LOCOMOTIVE_HEALTHCHECKS_URL": f"{fake_http.url}/ping/x",
        "LOCOMOTIVE_HIDDEN_PLAYBOOKS": "docker.yml,mac.yml",
        "LOCOMOTIVE_MCP_TOKENS": f"lxc={hashlib.sha256(token.encode()).hexdigest()}, mac={'b' * 64}",
        "LOCOMOTIVE_PUBLIC_HOSTNAME": "controller.example.invalid",
        "LOCOMOTIVE_MISE_BIN": str(stubs.mise),
    })
    alice = {"X-Who": "alice@example.invalid"}

    # Access guard: trusted peer, identity header, allowlist.
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs", headers={"X-Who": "bob@example.invalid"}).status_code == 403
    assert client.get("/api/runs", headers=alice).status_code == 200
    # Data dir: the database lives there.
    assert (data_dir / "locomotive.db").exists()
    # Working Copy and hidden Playbooks.
    assert client.get("/api/working-copy", headers=alice).json()["path"] == str(working_copy.path)
    listed = client.get("/api/playbooks", params={"checkout": "working_copy"}, headers=alice).json()
    assert [p["name"] for p in listed["playbooks"]] == ["locomotive.yml", "servers.yml"]
    # Scheduler Ping.
    wait_until(lambda: any(r.path == "/ping/x" for r in fake_http.requests))
    # Repo URL and its credentials: a Run fetches from there (the fake remote fails it), then ntfy is told.
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}, headers=alice).json()["id"]
    wait_until(lambda: client.get(f"/api/runs/{run_id}", headers=alice).json()["status"] == "failed")
    fetches = [r for r in fake_http.requests if r.path.startswith("/acme/infra.git/")]
    assert fetches and any(
        r.headers.get("Authorization") == "Basic " + base64.b64encode(b"bot:s3cret").decode() for r in fetches
    )
    wait_until(lambda: any(r.path == "/ntfy/" for r in fake_http.requests))
    [ntfy] = [r for r in fake_http.requests if r.path == "/ntfy/"]
    assert ntfy.headers["Authorization"] == "Bearer tk"
    assert json.loads(ntfy.body)["topic"] == "ops"
    # MCP tokens and the public hostname.
    mcp = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/mcp", json=body, headers=mcp | {"Host": "controller.example.invalid"}).status_code not in (401, 421)
    assert client.post("/mcp", json=body, headers=mcp | {"Host": "elsewhere.example.invalid"}).status_code == 421
    assert client.post("/mcp", json=body, headers={"Host": "controller.example.invalid"}).status_code == 401


@pytest.mark.parametrize("missing", ["LOCOMOTIVE_DATA_DIR", "LOCOMOTIVE_REPO_URL"])
def test_the_app_refuses_to_start_without_its_repo_url_or_data_dir(tmp_path, remote, missing):
    env = {"LOCOMOTIVE_DATA_DIR": str(tmp_path / "data"), "LOCOMOTIVE_REPO_URL": remote.url}
    del env[missing]

    proc = _main(env, _free_port())
    output, _ = proc.communicate(timeout=20)

    assert proc.returncode != 0
    assert missing.removeprefix("LOCOMOTIVE_").lower() in output
    assert not (tmp_path / "data").exists()


def test_hidden_playbooks_come_from_settings(start_app):
    client = start_app(hidden_playbooks=["docker.yml"])

    names = [p["name"] for p in client.get("/api/playbooks").json()["playbooks"]]

    assert names == ["locomotive.yml", "mac.yml", "servers.yml"]


def test_the_repo_token_never_appears_in_responses(start_app, tmp_path):
    client = start_app(repo_url=(tmp_path / "missing.git").as_uri(), repo_token="tok-123-secret")

    listing = client.get("/api/playbooks")
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    run = wait_for_run(client, run_id)
    log = client.get(f"/api/runs/{run_id}/log")

    for resp_text in (listing.text, str(run), log.text):
        assert "tok-123-secret" not in resp_text


def test_the_repo_token_authenticates_fetches_over_http(start_app, fake_http):
    client = start_app(repo_url=f"{fake_http.url}/acme/infra.git", repo_token="tok-123", repo_username="bot")

    client.get("/api/playbooks")

    assert fake_http.requests, "git never contacted the remote"
    auth = fake_http.requests[0].headers.get("Authorization")
    assert auth == "Basic " + base64.b64encode(b"bot:tok-123").decode()
    assert "tok-123" not in fake_http.requests[0].path

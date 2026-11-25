"""No homelab-specific value lives in the app code: everything comes from settings or env."""

import base64
import re
from pathlib import Path

import pytest
from conftest import wait_for_run

from locomotive.settings import Settings

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


def test_every_deployment_value_is_read_from_env(monkeypatch, tmp_path):
    env = {
        "LOCOMOTIVE_DATA_DIR": str(tmp_path),
        "LOCOMOTIVE_REPO_URL": "https://git.example.invalid/acme/infra.git",
        "LOCOMOTIVE_REPO_TOKEN": "s3cret",
        "LOCOMOTIVE_WORKING_COPY_PATH": "/srv/infra",
        "LOCOMOTIVE_TRUSTED_PROXY_IPS": "10.0.0.1, 10.0.0.2",
        "LOCOMOTIVE_IDENTITY_HEADER": "X-Who",
        "LOCOMOTIVE_ALLOWLIST": "alice@example.invalid",
        "LOCOMOTIVE_NTFY_URL": "https://ntfy.example.invalid",
        "LOCOMOTIVE_NTFY_TOPIC": "ops",
        "LOCOMOTIVE_NTFY_TOKEN": "tk",
        "LOCOMOTIVE_HEALTHCHECKS_URL": "https://hc.example.invalid/ping/x",
        "LOCOMOTIVE_HIDDEN_PLAYBOOKS": "a.yml,b.yml",
        "LOCOMOTIVE_MCP_TOKENS": f"lxc={'a' * 64}, mac={'b' * 64}",
        "LOCOMOTIVE_PUBLIC_HOSTNAME": "controller.example.invalid",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    s = Settings()

    assert s.data_dir == tmp_path
    assert s.repo_url == "https://git.example.invalid/acme/infra.git"
    assert s.repo_token.get_secret_value() == "s3cret"
    assert s.working_copy_path == Path("/srv/infra")
    assert s.trusted_proxy_ips == ["10.0.0.1", "10.0.0.2"]
    assert s.identity_header == "X-Who"
    assert s.allowlist == ["alice@example.invalid"]
    assert (s.ntfy_url, s.ntfy_topic, s.ntfy_token.get_secret_value()) == (
        "https://ntfy.example.invalid", "ops", "tk",
    )
    assert s.healthchecks_url == "https://hc.example.invalid/ping/x"
    assert s.hidden_playbooks == ["a.yml", "b.yml"]
    assert s.mcp_tokens == [f"lxc={'a' * 64}", f"mac={'b' * 64}"]
    assert s.public_hostname == "controller.example.invalid"


def test_repo_url_and_data_dir_are_required(monkeypatch):
    monkeypatch.delenv("LOCOMOTIVE_DATA_DIR", raising=False)
    monkeypatch.delenv("LOCOMOTIVE_REPO_URL", raising=False)
    with pytest.raises(ValueError):
        Settings()


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

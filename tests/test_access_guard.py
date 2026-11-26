"""The access guard: only the trusted proxy may connect, and only allowlisted identities get in."""

import pytest
from conftest import IDENTITY, IDENTITY_HEADER, LXC_TOKEN, McpCaller

from locomotive.app import create_app


def test_no_identity_header_is_a_plain_401_without_redirect(client, anonymous):
    resp = anonymous(client).get("/api/runs", follow_redirects=False)

    assert resp.status_code == 401
    assert "location" not in resp.headers


def test_an_identity_not_on_the_allowlist_is_403(client, anonymous):
    stranger = anonymous(client, {IDENTITY_HEADER: "mallory@example.invalid"})

    assert stranger.get("/api/runs").status_code == 403
    assert stranger.post("/api/runs", json={"playbook": "servers.yml"}).status_code == 403
    assert client.get("/api/runs").json()["runs"] == []


def test_a_peer_outside_the_trusted_proxy_list_is_rejected_even_with_an_allowlisted_identity(
    start_app,
):
    # The test client connects from 127.0.0.1, which this app does not trust.
    client = start_app(trusted_proxy_ips=["192.0.2.10"])

    for path in ("/api/runs", "/api/playbooks", "/mcp", "/"):
        assert client.get(path).status_code == 403, path
    assert client.post("/api/runs", json={"playbook": "servers.yml"}).status_code == 403


def test_trusted_proxies_can_be_given_as_networks(start_app):
    client = start_app(trusted_proxy_ips=["192.0.2.10", "127.0.0.0/8"])

    assert client.get("/api/runs").status_code == 200


def test_with_no_trusted_proxies_configured_every_peer_is_rejected(start_app):
    client = start_app(trusted_proxy_ips=[])

    assert client.get("/api/runs").status_code == 403


def test_mcp_is_not_guarded_by_identity_headers(client, anonymous):
    tools = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # /mcp answers with its own bearer-token 401, whatever identity is (or isn't) sent...
    for headers in (None, {IDENTITY_HEADER: IDENTITY}, {IDENTITY_HEADER: "mallory@example.invalid"}):
        resp = anonymous(client, headers).post("/mcp", json=tools)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")
    # ...and a valid token gets in with no identity at all, or a stranger's.
    stranger = {IDENTITY_HEADER: "mallory@example.invalid"}
    assert McpCaller(str(client.base_url), LXC_TOKEN).tool_names()
    assert McpCaller(str(client.base_url), LXC_TOKEN, headers=stranger).tool_names()
    # Only the exact /mcp prefix is exempt.
    assert anonymous(client).get("/mcpx").status_code == 401
    assert "www-authenticate" not in anonymous(client).get("/mcpx").headers


def test_mcp_with_a_valid_token_still_needs_a_trusted_peer(start_app, anonymous):
    client = start_app(trusted_proxy_ips=["192.0.2.10"])
    resp = anonymous(client, {"Authorization": f"Bearer {LXC_TOKEN}"}).post("/mcp", json={})
    assert resp.status_code == 403


def test_an_allowlisted_identity_reaches_the_ui_api_and_sse_streams(start_app, anonymous, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    client = start_app(static_dir=dist)
    # Header names are case-insensitive, and so is the login.
    ted = anonymous(client, {IDENTITY_HEADER.lower(): IDENTITY.upper()})

    assert ted.get("/").text == "<div id=root></div>"
    assert ted.get("/runs/1").text == "<div id=root></div>"
    run_id = ted.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    assert ted.get(f"/api/runs/{run_id}").status_code == 200
    with ted.stream("GET", f"/api/runs/{run_id}/log/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert any(line.startswith("event: end") for line in resp.iter_lines())


def test_the_ui_and_sse_streams_are_guarded_too(start_app, anonymous, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div id=root></div>")
    client = start_app(static_dir=dist)
    run_id = client.post("/api/runs", json={"playbook": "servers.yml"}).json()["id"]
    stranger = anonymous(client, {IDENTITY_HEADER: "mallory@example.invalid"})

    assert anonymous(client).get("/").status_code == 401
    assert stranger.get("/").status_code == 403
    assert anonymous(client).get(f"/api/runs/{run_id}/log/stream").status_code == 401
    assert stranger.get(f"/api/runs/{run_id}/log/stream").status_code == 403


def test_a_malformed_trusted_proxy_entry_stops_startup(make_settings):
    with pytest.raises(ValueError):
        create_app(make_settings(trusted_proxy_ips=["core.lan"]))

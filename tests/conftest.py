"""Seam 1 harness: locomotive in-process behind real HTTP, with fakes only at the true edges.

- a temp data dir (real SQLite in WAL mode lives in it);
- a local bare git repo standing in for the remote (``remote``);
- stub ``mise`` / ``ansible-runner`` / ``ansible-galaxy`` / ``ansible`` executables driven by a
  scripted scenario (``stubs``);
- an injected clock (``clock``);
- a fake HTTP server capturing outbound calls (``fake_http``).

Tests talk to the app only through ``client`` (an httpx client against a real uvicorn server
running the app in a background thread).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from locomotive.app import create_app
from locomotive.settings import Settings

STUBS_SRC = Path(__file__).parent / "stubs"


# --------------------------------------------------------------------------- clock


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, **kwargs: float) -> None:
        with self._lock:
            self._now += timedelta(**kwargs)

    def set(self, when: datetime) -> None:
        with self._lock:
            self._now = when


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --------------------------------------------------------------------------- remote git repo


def _git(*args: str, cwd: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


DEFAULT_TREE = {
    "ansible.cfg": "[defaults]\ninventory=hosts/inventory\n",
    "hosts/inventory": "[servers]\nalpha\nbeta\n",
    "mise.toml": '[tools]\n"pipx:ansible-core" = "2.22.0"\n',
    "collections/requirements.yml": "---\ncollections:\n  - name: community.general\n",
    "playbooks/servers.yml": "- hosts: servers\n  tasks: []\n",
    "playbooks/docker.yml": "- hosts: docker\n  tasks: []\n",
    "playbooks/mac.yml": "- hosts: mac\n  tasks: []\n",
    "playbooks/locomotive.yml": "- hosts: locomotive\n  tasks: []\n",
    "README.md": "fixture\n",
}


@dataclass
class Remote:
    """A bare repo used as the remote, plus a scratch clone to commit into it."""

    bare: Path
    work: Path

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def commit(
        self,
        files: dict[str, str | None],
        message: str = "change",
        branch: str = "main",
    ) -> str:
        """Write (or delete, with None) files on ``branch`` and push. Returns the new SHA."""
        _git("checkout", "-q", "-B", branch, f"origin/{branch}" if self.has_branch(branch) else "HEAD", cwd=self.work)
        for path, content in files.items():
            target = self.work / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        _git("add", "-A", cwd=self.work)
        _git("commit", "-q", "-m", message, cwd=self.work)
        _git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=self.work)
        _git("fetch", "-q", "origin", cwd=self.work)
        return _git("rev-parse", "HEAD", cwd=self.work)

    def has_branch(self, branch: str) -> bool:
        return bool(_git("ls-remote", "--heads", "origin", branch, cwd=self.work))

    def sha(self, branch: str = "main") -> str:
        return _git("rev-parse", f"refs/heads/{branch}", cwd=self.bare)


@pytest.fixture
def remote(tmp_path: Path) -> Remote:
    bare = tmp_path / "remote.git"
    work = tmp_path / "remote-work"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(work), cwd=tmp_path)
    _git("remote", "add", "origin", bare.as_uri(), cwd=work)
    for path, content in DEFAULT_TREE.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "initial", cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)
    return Remote(bare=bare, work=work)


# --------------------------------------------------------------------------- stub executables


@dataclass
class Stubs:
    """Stub toolchain. ``scenario`` is read by the stubs at the moment they run."""

    root: Path
    scenario: dict[str, Any] = field(default_factory=dict)

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def mise(self) -> Path:
        return self.bin / "mise"

    def script(self, **scenario: Any) -> None:
        self.scenario.update(scenario)
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines() if line]
        return [c for c in calls if tool is None or c["tool"] == tool]

    def release_gate(self) -> None:
        (self.root / "gate").touch()


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    root = tmp_path / "stubs"
    (root / "bin").mkdir(parents=True)
    for src in STUBS_SRC.glob("*.py"):
        if src.name.startswith("_"):
            continue
        dst = root / "bin" / src.stem.replace("_", "-")
        dst.write_text(f"#!{sys.executable}\n" + src.read_text())
        dst.chmod(0o755)
    s = Stubs(root=root)
    s.script()
    return s


# --------------------------------------------------------------------------- fake HTTP server


@dataclass
class CapturedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeHTTP:
    def __init__(self) -> None:
        self.requests: list[CapturedRequest] = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append(
                    CapturedRequest(self.command, self.path, dict(self.headers), body)
                )
                self.send_response(fake.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_HEAD = _handle

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def fake_http() -> Iterator[FakeHTTP]:
    server = FakeHTTP()
    yield server
    server.close()


# --------------------------------------------------------------------------- the app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppServer:
    def __init__(self, settings: Settings, clock: FakeClock) -> None:
        self.settings = settings
        self.clock = clock
        self.port = _free_port()
        config = uvicorn.Config(
            create_app(settings, clock=clock),
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            lifespan="on",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> httpx.Client:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("locomotive did not start")
            time.sleep(0.01)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10)
        return self.client

    def __exit__(self, *exc: object) -> None:
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def make_settings(
    data_dir: Path, remote: Remote, stubs: Stubs
) -> Callable[..., Settings]:
    def make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "data_dir": data_dir,
            "repo_url": remote.url,
            "hidden_playbooks": ["mac.yml", "locomotive.yml"],
            "mise_bin": str(stubs.mise),
        }
        values.update(overrides)
        return Settings(**values)

    return make


@pytest.fixture
def app_servers() -> Iterator[list[AppServer]]:
    servers: list[AppServer] = []
    yield servers
    for server in reversed(servers):
        if server.thread.is_alive():
            server.__exit__(None, None, None)


@pytest.fixture
def start_app(
    make_settings: Callable[..., Settings], clock: FakeClock, app_servers: list[AppServer]
) -> Callable[..., httpx.Client]:
    def start(**overrides: Any) -> httpx.Client:
        server = AppServer(make_settings(**overrides), clock)
        app_servers.append(server)
        return server.__enter__()

    return start


@pytest.fixture
def stop_app(app_servers: list[AppServer]) -> Callable[[httpx.Client], None]:
    """Shut down (gracefully, as on ``docker stop``) the app instance behind ``client``."""

    def stop(client: httpx.Client) -> None:
        [server] = [s for s in app_servers if s.client is client]
        server.__exit__(None, None, None)

    return stop


@pytest.fixture
def client(start_app: Callable[..., httpx.Client]) -> httpx.Client:
    return start_app()


# --------------------------------------------------------------------------- helpers


TERMINAL = {"succeeded", "failed", "canceled", "interrupted"}


def wait_for_run(client: httpx.Client, run_id: int, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        run = client.get(f"/api/runs/{run_id}").raise_for_status().json()
        if run["status"] in TERMINAL:
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"Run {run_id} still {run['status']} after {timeout}s")
        time.sleep(0.05)


def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.05)


def sse_events(lines):
    """Parse an SSE line iterator into (event, id, data) tuples."""
    event, ident, data = "message", None, []
    for line in lines:
        if line == "":
            if data:
                yield event, ident, "\n".join(data)
            event, ident, data = "message", None, []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "id":
                ident = value
            elif field == "data":
                data.append(value)

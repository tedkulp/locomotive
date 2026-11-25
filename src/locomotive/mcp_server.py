"""The MCP server at ``/mcp``: Claude Code (and other MCP clients) read and drive locomotive.

Built on the official ``mcp`` SDK (``MCPServer``), served over streamable HTTP inside the FastAPI app.

- **Auth**: a static bearer token per client, configured as ``name=<sha256 of the token>``
  (``Settings.mcp_tokens``) and compared hashed. A missing or unknown token is a 401; identity headers
  mean nothing here (the access guard still requires a trusted peer first). A Run a token starts is
  labelled ``mcp:<name>`` in history.
- **One source of truth**: every tool is a thin adapter over locomotive's own HTTP API, called
  in-process (no network, no guard), so validation, loud failure of unsupported options and response
  shapes are exactly the UI's.
- **Budget**: every tool returns within ``mcp_tool_budget_seconds`` (under the 10s clients allow),
  except ``wait_for_run``, which returns within the ``max_seconds`` (at most 30) the caller asks for.
- **Long Runs** are never awaited in one call: ``start_run`` returns the Run's id and status at once
  (queued or running); ``get_run`` / ``tail_run_output(cursor)`` / ``wait_for_run`` follow it.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, ParamSpec, TypeVar

import httpx
from fastapi import APIRouter, FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from locomotive.api import RUN_SOURCE_KEY
from locomotive.models import TERMINAL_STATUSES
from locomotive.settings import Settings

MCP_PATH = "/mcp"
TOKEN_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

MAX_WAIT_SECONDS = 30
DEFAULT_WAIT_SECONDS = 8  # unasked, it too answers within the 10s budget
WAIT_POLL_SECONDS = 0.2
DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 1000
MAX_TAIL_BYTES = 256 * 1024  # well under the client's 1 MiB per-event cap

LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

INSTRUCTIONS = """\
locomotive runs this repo's Ansible Playbooks. Typical flow: list_playbooks, preview_hosts /
preview_tasks for the exact blast radius, then start_run. start_run defaults to check mode; a real
Run (check: false) needs confirm: true. It returns at once with the Run's id and status (queued until
no other Run holds its hosts, then running): follow it with wait_for_run, get_run and
tail_run_output (pass back the cursor you got). source: working_copy runs the uncommitted Working Copy
instead of a clean commit of a branch."""

Source = Literal["clean", "working_copy"]


# ---------------------------------------------------------------------- bearer tokens


def parse_tokens(entries: list[str]) -> dict[str, str]:
    """``name=<sha256 hex>`` entries -> {hash: "mcp:<name>"}. Raises ValueError on a bad entry."""
    tokens: dict[str, str] = {}
    for entry in entries:
        name, sep, digest = entry.partition("=")
        name, digest = name.strip(), digest.strip().lower()
        if not sep or not TOKEN_NAME.match(name) or not SHA256_HEX.match(digest):
            raise ValueError(
                "LOCOMOTIVE_MCP_TOKENS entries are name=<sha256 hex of the token> "
                f"(name: lowercase letters, digits, - or _); got {name or entry!r}"
            )
        tokens[digest] = f"mcp:{name}"
    return tokens


class BearerGuard:
    """401 unless ``Authorization: Bearer <token>`` hashes to a configured token. Fails closed."""

    def __init__(self, app: ASGIApp, tokens: dict[str, str]) -> None:
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        source = self._source(scope) if scope["type"] == "http" else None
        if source is None:
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "a valid bearer token is required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="locomotive", error="invalid_token"'},
            )
            await response(scope, receive, send)
            return
        await self.app({**scope, RUN_SOURCE_KEY: source}, receive, send)

    def _source(self, scope: Scope) -> str | None:
        header = next((v for k, v in scope["headers"] if k == b"authorization"), b"").decode("latin-1")
        scheme, _, token = header.strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        found = None
        for known, source in self.tokens.items():  # check every entry: no early exit to time
            if hmac.compare_digest(digest, known):
                found = source
        return found


# ---------------------------------------------------------------------- the HTTP API, in-process


class _Api:
    """locomotive's own HTTP API, called in-process; an error status becomes a ToolError."""

    def __init__(self, app: FastAPI, routers: list[APIRouter]) -> None:
        self._app = FastAPI()
        for router in routers:
            self._app.include_router(router, prefix="/api")
        self._app.state = app.state  # the same services as the real app (set up by its lifespan)

    async def call(
        self, method: str, path: str, *, source: str | None = None, **kwargs: Any
    ) -> Any:
        inner = self._app

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if source is not None:
                scope = {**scope, RUN_SOURCE_KEY: source}
            await inner(scope, receive, send)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://locomotive") as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ToolError(_detail(response))
        return None if response.status_code == 204 else response.json()


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (json.JSONDecodeError, AttributeError):
        return f"HTTP {response.status_code}"
    if isinstance(detail, list):  # request validation errors
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:]) or 'request'}: {e.get('msg')}" for e in detail
        )
    return str(detail) if detail else f"HTTP {response.status_code}"


# ---------------------------------------------------------------------- the server


P = ParamSpec("P")
R = TypeVar("R")


def _within(budget: float, what: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                async with asyncio.timeout(budget):
                    return await fn(*args, **kwargs)
            except TimeoutError:
                raise ToolError(f"{what} did not answer within {budget:g}s; try again shortly") from None

        return wrapper

    return decorate


def _limit(hosts: list[str] | None, limit: str | None) -> str | None:
    if hosts and limit:
        raise ToolError("pass hosts or limit, not both")
    if hosts:
        return ",".join(h.strip() for h in hosts if h.strip()) or None
    return limit


def _checkout(body: dict[str, Any], branch: str | None, source: Source) -> dict[str, Any]:
    """The checkout choice in the API's terms; left out when it's the default (a clean default branch)."""
    if source != "clean":
        body["checkout"] = source
    if branch is not None:
        body["branch"] = branch
    return body


def create_mcp(app: FastAPI, settings: Settings, routers: list[APIRouter]) -> tuple[MCPServer, ASGIApp]:
    """The MCP server and the ASGI app to route ``/mcp`` to (bearer guard included). ``routers`` are
    the app's ``/api`` routers, which the tools call in-process."""
    tokens = parse_tokens(settings.mcp_tokens)
    budget = settings.mcp_tool_budget_seconds
    http = _Api(app, routers)
    server = MCPServer(name="locomotive", instructions=INSTRUCTIONS)

    def tool(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        server.tool()(_within(budget, fn.__name__)(fn))
        return fn

    async def _preview(
        playbook: str, hosts: list[str] | None, limit: str | None, tags: list[str], extra_vars: dict[str, Any],
        branch: str | None, source: Source, expand_includes: bool = False,
    ) -> dict[str, Any]:
        body = _checkout(
            {
                "playbook": playbook, "limit": _limit(hosts, limit), "tags": tags, "extra_vars": extra_vars,
                "expand_includes": expand_includes,
            },
            branch, source,
        )
        return await http.call("POST", "/api/preview", json=body)

    # -------------------------------------------------------------- read

    @tool
    async def list_playbooks(branch: str | None = None, source: Source = "clean") -> dict[str, Any]:
        """The Playbooks a Run can use, read now from the chosen checkout: a clean ``branch`` (default:
        the default branch) or the Working Copy (``source: working_copy``)."""
        params: dict[str, str] = {}
        if branch is not None:
            params["branch"] = branch
        if source != "clean":
            params["checkout"] = source
        return await http.call("GET", "/api/playbooks", params=params)

    @tool
    async def list_branches() -> dict[str, Any]:
        """The remote's branches (fetched now) and which one is the default."""
        return await http.call("GET", "/api/branches")

    @tool
    async def working_copy_status() -> dict[str, Any]:
        """The Working Copy's branch, HEAD, dirty and untracked files, and ahead/behind its upstream."""
        return await http.call("GET", "/api/working-copy")

    @tool
    async def list_schedules() -> dict[str, Any]:
        """Schedules: cron, Run settings, enabled and notify-on-changes toggles, plus whether all are paused."""
        return await http.call("GET", "/api/schedules")

    @tool
    async def get_inventory(
        playbook: str, branch: str | None = None, source: Source = "clean"
    ) -> dict[str, Any]:
        """The inventory graph (groups, their child groups and hosts; never host vars) as the given
        Playbook's checkout sees it."""
        reply = await _preview(playbook, None, None, [], {}, branch, source)
        return {"branch": reply.get("branch"), "sha": reply.get("sha"), **reply["inventory"]}

    @tool
    async def list_tags(playbook: str, branch: str | None = None, source: Source = "clean") -> dict[str, Any]:
        """The tags a Run of this Playbook can select (none selected means run everything)."""
        reply = await _preview(playbook, None, None, [], {}, branch, source)
        return {"branch": reply.get("branch"), "sha": reply.get("sha"), "tags": reply["tags"]}

    @tool
    async def preview_hosts(
        playbook: str,
        hosts: list[str] | None = None,
        limit: str | None = None,
        tags: list[str] | None = None,
        extra_vars: dict[str, Any] | None = None,
        branch: str | None = None,
        source: Source = "clean",
    ) -> dict[str, Any]:
        """Exactly which hosts a Run would touch (``--limit`` semantics, patterns and &/! included), per
        play, plus any limit patterns that match nothing. ``hosts`` is a list of hosts, groups or
        patterns; ``limit`` the same as one string."""
        reply = await _preview(playbook, hosts, limit, tags or [], extra_vars or {}, branch, source)
        return {k: reply.get(k) for k in ("branch", "sha", "command", "hosts")}

    @tool
    async def preview_tasks(
        playbook: str,
        hosts: list[str] | None = None,
        limit: str | None = None,
        tags: list[str] | None = None,
        extra_vars: dict[str, Any] | None = None,
        branch: str | None = None,
        source: Source = "clean",
        expand_includes: bool = False,
    ) -> dict[str, Any]:
        """Tasks that may run, per play (approximate: includes expand at run time; ``expand_includes``
        peeks one level in), with whether each is selected by the tags."""
        reply = await _preview(playbook, hosts, limit, tags or [], extra_vars or {}, branch, source, expand_includes)
        return {k: reply.get(k) for k in ("branch", "sha", "command", "tasks")}

    @tool
    async def list_runs(limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, Any]:
        """Recent Runs, newest first, with status, who started them (``source``) and their settings."""
        return await http.call("GET", "/api/runs", params={"limit": limit})

    @tool
    async def get_run(run_id: int) -> dict[str, Any]:
        """One Run: status, settings, commit, timings, failure reason and per-host recap."""
        return await http.call("GET", f"/api/runs/{run_id}")

    # -------------------------------------------------------------- act

    @tool
    async def start_run(
        ctx: Context,
        playbook: str,
        hosts: list[str] | None = None,
        limit: str | None = None,
        tags: list[str] | None = None,
        check: bool = True,
        diff: bool = False,
        extra_vars: dict[str, Any] | None = None,
        verbosity: Annotated[int, Field(ge=0, le=4)] = 0,
        branch: str | None = None,
        source: Source = "clean",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Start a Run and return at once with its id and status (queued until no other Run holds its
        hosts, then running). Check mode by default; a real Run (``check: false``) changes hosts and
        needs ``confirm: true``. ``source: clean`` runs a fresh commit of ``branch`` (default: the
        default branch); ``working_copy`` runs the Working Copy as it is now, uncommitted edits included.
        Options locomotive can't honour fail the call; nothing is silently ignored."""
        if not check and not confirm:
            raise ToolError(
                "This is a real Run (check: false): it changes hosts. Preview it, then call start_run again "
                "with confirm: true."
            )
        body = _checkout(
            {
                "playbook": playbook, "limit": _limit(hosts, limit), "tags": tags or [], "check": check,
                "diff": diff, "extra_vars": extra_vars or {}, "verbosity": verbosity,
            },
            branch, source,
        )
        run_source = ctx.request_context.request.scope[RUN_SOURCE_KEY]
        return await http.call("POST", "/api/runs", json=body, source=run_source)

    @tool
    async def tail_run_output(
        run_id: int,
        cursor: Annotated[int, Field(ge=0)] = 0,
        max_lines: Annotated[int, Field(ge=1, le=MAX_TAIL_LINES)] = DEFAULT_TAIL_LINES,
    ) -> dict[str, Any]:
        """The Run's log after ``cursor`` (0: from the start), up to ``max_lines`` lines. Pass the
        returned ``cursor`` to the next call; ``more`` says there is already more to read, and
        ``finished`` that the Run is over (so once ``more`` is false, the log is complete)."""
        run = await http.call("GET", f"/api/runs/{run_id}")  # status first: a finished Run's log is whole
        if run.get("log_pruned"):  # set once housekeeping has pruned this Run's log
            raise ToolError(
                f"log_pruned: Run {run_id}'s log was pruned on {run.get('log_pruned_at')}; its record and "
                "recap remain (get_run)"
            )
        reply = await http.call("GET", f"/api/runs/{run_id}/log", params={"after": cursor})
        lines = reply["lines"]
        page: list[dict[str, Any]] = []
        size = 0
        for line in lines[:max_lines]:
            size += len(line["text"]) + 32
            if page and size > MAX_TAIL_BYTES:
                break
            page.append({"seq": line["seq"], "text": line["text"], "host": line["host"]})
        return {
            "run_id": run_id,
            "status": run["status"],
            "finished": run["status"] in TERMINAL_STATUSES,
            "lines": page,
            "cursor": page[-1]["seq"] if page else cursor,
            "more": len(page) < len(lines),
        }

    async def wait_for_run(
        run_id: int, max_seconds: Annotated[float, Field(ge=0, le=MAX_WAIT_SECONDS)] = DEFAULT_WAIT_SECONDS
    ) -> dict[str, Any]:
        """Wait up to ``max_seconds`` (default 8, at most 30) for the Run to end; returns as soon as it does, with
        the Run and ``finished``. If it is still going, call again (or tail its output meanwhile)."""
        deadline = time.monotonic() + max_seconds
        while True:
            run = await http.call("GET", f"/api/runs/{run_id}")
            finished = run["status"] in TERMINAL_STATUSES
            if finished or time.monotonic() >= deadline:
                return {"finished": finished, "run": run}
            await asyncio.sleep(min(WAIT_POLL_SECONDS, max(deadline - time.monotonic(), 0)))

    server.tool()(_within(MAX_WAIT_SECONDS + budget, "wait_for_run")(wait_for_run))

    mcp_app: Starlette = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        transport_security=_transport_security(settings.public_hostname),
    )
    return server, BearerGuard(mcp_app, tokens)


def _transport_security(public_hostname: str | None) -> TransportSecuritySettings:
    """DNS-rebinding protection: Host/Origin must be localhost or the configured public hostname."""
    hosts, origins = list(LOCAL_HOSTS), list(LOCAL_ORIGINS)
    if public_hostname:
        hosts += [public_hostname, f"{public_hostname}:*"]
        origins += [f"https://{public_hostname}", f"https://{public_hostname}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )

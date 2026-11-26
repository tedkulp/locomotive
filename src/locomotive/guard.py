"""The access guard: the one gate every request passes before reaching the app.

locomotive has no login of its own. It trusts an identity header only because the reverse proxy is
the only way in, so the guard enforces that too:

1. the TCP peer must be in ``trusted_proxy_ips`` (addresses or networks), else 403 for every path;
2. the ``identity_header`` must be present (else a plain 401, no redirect) and on the
   ``allowlist`` (else 403). ``/mcp`` skips this step: it ignores identity headers and
   authenticates with bearer tokens of its own.

Misconfiguration fails closed: no trusted proxies rejects every peer, no identity header (or an
empty allowlist) lets no one in.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from locomotive.settings import Settings

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MCP_PATH = "/mcp"


def parse_networks(entries: Iterable[str]) -> list[Network]:
    """``10.0.0.1`` or ``10.0.0.0/24`` (IPv4 or IPv6). Raises ValueError on a bad entry."""
    return [ipaddress.ip_network(entry, strict=False) for entry in entries]


class AccessGuard:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.trusted = parse_networks(settings.trusted_proxy_ips)
        self.header = (settings.identity_header or "").lower().encode("latin-1")
        self.allowlist = {identity.casefold() for identity in settings.allowlist}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket routes: refuse rather than guard them
            await send({"type": "websocket.close", "code": 1008})
            return
        if not self._peer_is_trusted(scope):
            await _reject(403, "Forbidden", scope, receive, send)
            return
        if not _is_mcp(scope["path"]):
            identity = self._identity(scope)
            if not identity:
                await _reject(401, "Unauthorized", scope, receive, send)
                return
            if identity.casefold() not in self.allowlist:
                await _reject(403, "Forbidden", scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _peer_is_trusted(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            peer = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in network for network in self.trusted)

    def _identity(self, scope: Scope) -> str:
        if not self.header:
            return ""
        for name, value in scope["headers"]:
            if name == self.header:
                return value.decode("latin-1").strip()
        return ""


def _is_mcp(path: str) -> bool:
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


async def _reject(status: int, text: str, scope: Scope, receive: Receive, send: Send) -> None:
    await PlainTextResponse(text, status_code=status)(scope, receive, send)

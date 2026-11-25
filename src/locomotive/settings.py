"""All deployment-specific values come from here (env vars prefixed ``LOCOMOTIVE_``).

Nothing homelab-specific lives in the app code: the deploy role supplies these.
"""

from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CommaList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOCOMOTIVE_", extra="ignore")

    # Where locomotive keeps its database, bare mirror, Run dirs, logs and caches.
    data_dir: Path
    database_url: str | None = None

    # The one repo locomotive runs Playbooks from.
    repo_url: str
    repo_username: str = "git"
    repo_token: SecretStr | None = None
    default_branch: str = "main"
    working_copy_path: Path | None = None
    hidden_playbooks: CommaList = []

    # Access guard (see guard.py). Fails closed: unset means no peer / no identity gets in.
    # Trusted proxies are addresses or networks (10.0.0.1, 10.0.0.0/24).
    trusted_proxy_ips: CommaList = []
    identity_header: str | None = None
    allowlist: CommaList = []

    # MCP at /mcp: static bearer tokens, stored as ``name=<sha256 hex of the token>`` entries. A Run a
    # token starts is labelled ``mcp:<name>`` in history (e.g. ``lxc=…`` -> ``mcp:lxc``). None: no MCP.
    mcp_tokens: CommaList = []
    # The hostname clients reach locomotive by; MCP's DNS-rebinding
    # protection accepts it in Host/Origin besides localhost.
    public_hostname: str | None = None
    # Every MCP tool answers within this (clients give up at 10s); wait_for_run within what it's asked.
    mcp_tool_budget_seconds: float = 9.0

    # Outbound notifications and the Scheduler Ping.
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: SecretStr | None = None
    healthchecks_url: str | None = None

    # Schedules: cron times are read in this IANA time zone (unset: TZ, then /etc/localtime, then UTC).
    time_zone: str | None = None
    # The scheduler loop looks for due firings this often; the Scheduler Ping goes out every 5 minutes.
    scheduler_interval_seconds: float = 30.0

    # Toolchain used for every Run (the checkout's own mise.toml decides versions).
    mise_bin: str = "mise"

    # Kill sends SIGINT to a Run's process group, then SIGKILL if it is still alive after this long.
    kill_grace_seconds: float = 10.0

    # A quiet Run's live stream sends a keep-alive this often so proxies don't drop it.
    sse_heartbeat_seconds: float = 15.0

    # Previews must answer within the 10s MCP budget; the helper itself is killed after this long.
    preview_timeout_seconds: float = 8.0

    # Built frontend served at /; unset means API only.
    static_dir: Path | None = None

    @field_validator("hidden_playbooks", "trusted_proxy_ips", "allowlist", "mcp_tokens", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'locomotive.db'}"

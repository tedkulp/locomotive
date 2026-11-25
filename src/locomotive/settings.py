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

    # Access guard (enforced by a later ticket; listed so nothing is hardcoded).
    trusted_proxy_ips: CommaList = []
    identity_header: str | None = None
    allowlist: CommaList = []

    # Outbound notifications and the Scheduler Ping.
    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: SecretStr | None = None
    healthchecks_url: str | None = None

    # Toolchain used for every Run (the checkout's own mise.toml decides versions).
    mise_bin: str = "mise"

    # Kill sends SIGINT to a Run's process group, then SIGKILL if it is still alive after this long.
    kill_grace_seconds: float = 10.0

    # A quiet Run's live stream sends a keep-alive this often so proxies don't drop it.
    sse_heartbeat_seconds: float = 15.0

    # Built frontend served at /; unset means API only.
    static_dir: Path | None = None

    @field_validator("hidden_playbooks", "trusted_proxy_ips", "allowlist", mode="before")
    @classmethod
    def _split_commas(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'locomotive.db'}"

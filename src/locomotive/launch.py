"""Launch options: what a user (or a client) may ask of a Run, validated loudly, and the command they mean.

Every option here is one locomotive can honour. Anything else is rejected at launch (``extra="forbid"``),
so nothing is ever silently ignored.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

TAG = re.compile(r"^[^\s,]+$")
MAX_EXTRA_VARS_BYTES = 64 * 1024


class LaunchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    playbook: str = Field(min_length=1, max_length=255)
    limit: str | None = Field(default=None, max_length=4096)
    tags: list[str] = Field(default_factory=list, max_length=200)
    check: bool = True
    diff: bool = False
    verbosity: int = Field(default=0, ge=0, le=4)
    extra_vars: dict[str, Any] = Field(default_factory=dict)

    @field_validator("limit")
    @classmethod
    def _limit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if any(ch in value for ch in "\r\n\0"):
            raise ValueError("the limit must be a single line")
        patterns = [p.strip() for p in re.split(r"[,:]", value) if p.strip()]
        if any(p.lstrip("&!").startswith("@") for p in patterns):
            raise ValueError("limit files (@file) are not supported")
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not TAG.match(tag):
                raise ValueError(f"invalid tag {tag!r} (no spaces or commas)")
        return list(dict.fromkeys(value))

    @field_validator("extra_vars")
    @classmethod
    def _extra_vars(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_EXTRA_VARS_BYTES:
            raise ValueError("extra vars are too large")
        return value

    def cmdline(self) -> list[str]:
        """The ``ansible-playbook`` arguments after the Playbook path."""
        args: list[str] = []
        if self.check:
            args.append("--check")
        if self.diff:
            args.append("--diff")
        if self.limit:
            args += ["--limit", self.limit]
        if self.tags:
            args += ["--tags", ",".join(self.tags)]
        if self.verbosity:
            args.append("-" + "v" * self.verbosity)
        if self.extra_vars:
            args += ["--extra-vars", json.dumps(self.extra_vars)]
        return args

    def command(self) -> str:
        return shlex.join(["ansible-playbook", f"playbooks/{self.playbook}", *self.cmdline()])

    def helper_request(self, expand_includes: bool = False) -> dict[str, Any]:
        return {
            "playbook": f"playbooks/{self.playbook}",
            "limit": self.limit,
            "tags": self.tags,
            "extra_vars": self.extra_vars,
            "expand_includes": expand_includes,
        }

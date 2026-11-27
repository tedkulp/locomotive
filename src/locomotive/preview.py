"""Preview module: the exact hosts a Run will touch and the tasks that may run, before it starts.

A preview resolves a commit, exports it once into a cached per-commit dir, prepares that commit's own
runtime (mise + collections), and asks the preview helper (run in that runtime) for JSON. The reply is
re-built field by field from what the helper is allowed to say (names, patterns, tags), so nothing else,
host vars in particular, can ever reach a client.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from locomotive.checkout import Checkout, CheckoutError
from locomotive.launch import LaunchOptions
from locomotive.runtime import PreviewTimeout, Runtime, Toolchain, ToolchainError
from locomotive.settings import Settings
from locomotive.working_copy import WorkingCopy

KEEP_PREPARED = 4
KEEP_REPLIES = 16  # remembered clean preview replies per commit, most recently used kept
TOOLCHAIN_FILES = ("mise.toml", "collections/requirements.yml")
TASKS_LABEL = "tasks that may run"


class PreviewError(Exception):
    """The preview can't be given; ``status`` is the HTTP status that says why."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# Seconds a client should wait before asking again while a runtime is still being prepared.
PREPARING_RETRY_SECONDS = 2


@dataclass
class _Prepared:
    task: asyncio.Task[Runtime]
    used: float
    cleanup: bool = True  # whether evicting it deletes its dir under previews/


class Previewer:
    def __init__(self, settings: Settings, checkout: Checkout, toolchain: Toolchain) -> None:
        self._settings = settings
        self._checkout = checkout
        self._toolchain = toolchain
        self._prepared: dict[str, _Prepared] = {}
        # Sanitized helper replies for clean previews, by commit and then by helper request. A commit's
        # replies go when its prepared runtime is evicted. Never the Working Copy: it changes on disk.
        self._replies: dict[str, dict[str, dict[str, Any]]] = {}

    @property
    def previews_dir(self) -> Path:
        return self._settings.data_dir / "previews"

    def sweep(self) -> None:
        """At startup no preview is prepared, so every leftover dir goes."""
        shutil.rmtree(self.previews_dir, ignore_errors=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)

    async def shutdown(self) -> None:
        for prepared in self._prepared.values():
            prepared.task.cancel()
        for prepared in self._prepared.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prepared.task

    # ------------------------------------------------------------------ public

    async def preview(
        self, options: LaunchOptions, branch: str, sha: str | None, expand_includes: bool
    ) -> dict[str, Any]:
        """Preview a clean Run of ``branch`` (or of commit ``sha``, if the mirror already has it)."""
        deadline = time.monotonic() + self._settings.preview_timeout_seconds
        if sha is None or not await self._checkout.has_commit(sha):
            sha = await self._checkout.fetch(branch)
        runtime = await self._runtime(sha, lambda: self._prepare(sha), options.playbook, f"at {sha[:12]}", deadline)
        request = options.helper_request(expand_includes)
        request_key = json.dumps(request, sort_keys=True)  # canonical: the request holds a list and a dict
        replies = self._replies.setdefault(sha, {})
        reply = replies.pop(request_key, None)  # re-inserted below, so dict order is least recently used first
        if reply is None:  # only a successful reply is remembered; a failure raises before this
            reply = await self._ask(runtime, request, deadline)
        replies[request_key] = reply
        if len(replies) > KEEP_REPLIES:
            del replies[next(iter(replies))]
        return {"checkout": "clean", "branch": branch, "sha": sha, "command": options.command(), **reply}

    async def preview_working_copy(
        self, options: LaunchOptions, working_copy: WorkingCopy, expand_includes: bool
    ) -> dict[str, Any]:
        """Preview a Run of the Working Copy as it is on disk now, in its own runtime.

        The runtime is reused only while the Working Copy's ``mise.toml`` and collections requirements are
        unchanged, so an uncommitted toolchain bump shows up in the next preview.
        """
        deadline = time.monotonic() + self._settings.preview_timeout_seconds
        status = await working_copy.status()
        project = status.path
        key = "wc-" + hashlib.sha256(
            b"\0".join(_read(project / name) for name in TOOLCHAIN_FILES)
        ).hexdigest()
        runtime = await self._runtime(
            key, lambda: self._toolchain.prepare(project), options.playbook, "in the Working Copy", deadline,
            cleanup=False,
        )
        reply = await self._ask(runtime, options.helper_request(expand_includes), deadline)
        return {
            "checkout": "working_copy",
            "branch": status.branch or "HEAD",
            "sha": status.head_sha,
            "command": options.command(),
            **reply,
        }

    async def _ask(
        self, runtime: Runtime, request: dict[str, Any], deadline: float
    ) -> dict[str, Any]:
        try:
            reply = await self._toolchain.preview(runtime, request, timeout=deadline - time.monotonic())
        except PreviewTimeout as exc:
            raise PreviewError(504, str(exc)) from exc
        except ToolchainError as exc:
            raise PreviewError(502, str(exc)) from exc
        if not reply.get("ok"):
            raise PreviewError(422, _text(reply.get("error")) or "the preview helper reported an error")
        return sanitize(reply)

    # ------------------------------------------------------------------ prepared runtimes

    async def _runtime(
        self,
        key: str,
        prepare: Callable[[], Awaitable[Runtime]],
        playbook: str,
        where: str,
        deadline: float,
        cleanup: bool = True,
    ) -> Runtime:
        prepared = self._prepared.get(key)
        if prepared is None or (prepared.task.done() and prepared.task.exception() is not None):
            prepared = _Prepared(asyncio.create_task(prepare(), name=f"preview-{key[:15]}"), 0, cleanup)
            self._prepared[key] = prepared
            self._evict()
        prepared.used = time.monotonic()
        try:
            # Shielded: a request that runs out of time leaves the preparation running for the next one.
            runtime = await asyncio.wait_for(asyncio.shield(prepared.task), max(deadline - time.monotonic(), 0.1))
        except TimeoutError:
            # Not a failure: the preparation carries on, and the client asks again after Retry-After.
            raise PreviewError(
                503,
                f"Still preparing the Ansible runtime {where} (first use installs it); try again shortly",
                retry_after=PREPARING_RETRY_SECONDS,
            ) from None
        except (ToolchainError, CheckoutError) as exc:
            raise PreviewError(502, str(exc)) from exc
        if not (runtime.project / "playbooks" / playbook).is_file():
            raise PreviewError(404, f"Playbook playbooks/{playbook} does not exist {where}")
        return runtime

    async def _prepare(self, sha: str) -> Runtime:
        root = self.previews_dir / sha
        project = root / "project"
        if not project.is_dir():
            shutil.rmtree(root, ignore_errors=True)
            staging = self.previews_dir / f".staging-{sha}"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            await self._checkout.export(sha, staging / "project")
            staging.rename(root)
        return await self._toolchain.prepare(project)

    def _evict(self) -> None:
        if len(self._prepared) <= KEEP_PREPARED:
            return
        for key, prepared in sorted(self._prepared.items(), key=lambda item: item[1].used)[: -KEEP_PREPARED]:
            if not prepared.task.done():
                continue
            del self._prepared[key]
            self._replies.pop(key, None)
            if prepared.cleanup:  # an export of a commit; never the Working Copy
                shutil.rmtree(self.previews_dir / key, ignore_errors=True)


# ---------------------------------------------------------------------- the reply, field by field


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _names(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _task(task: object, depth: int = 0) -> dict[str, Any] | None:
    if not isinstance(task, dict):
        return None
    include = task.get("include")
    clean_include = None
    if isinstance(include, dict):
        children = include.get("tasks")
        count = include.get("task_count")
        clean_include = {
            "kind": _text(include.get("kind")),
            "target": _text(include.get("target")),
            "expandable": bool(include.get("expandable")),
            "reason": _text(include.get("reason")),
            "task_count": count if isinstance(count, int) else None,
            "tasks": (
                [t for t in (_task(c, depth + 1) for c in children) if t is not None]
                if isinstance(children, list) and depth == 0
                else None
            ),
        }
    return {
        "name": _text(task.get("name")) or "",
        "role": _text(task.get("role")),
        "action": _text(task.get("action")),
        "tags": _names(task.get("tags")),
        "selected": bool(task.get("selected", True)),
        "include": clean_include,
    }


def sanitize(reply: dict[str, Any]) -> dict[str, Any]:
    plays = [p for p in reply.get("plays") or [] if isinstance(p, dict)]
    return {
        "hosts": {
            "hosts": _names(reply.get("hosts")),
            "all_hosts": _names(reply.get("all_hosts")),
            "unmatched": _names(reply.get("unmatched")),
            "plays": [
                {
                    "name": _text(p.get("name")) or "",
                    "pattern": _text(p.get("pattern")) or "",
                    "hosts": _names(p.get("hosts")),
                    "all_hosts": _names(p.get("all_hosts")),
                }
                for p in plays
            ],
        },
        "tasks": {
            "label": TASKS_LABEL,
            "plays": [
                {
                    "name": _text(p.get("name")) or "",
                    "pattern": _text(p.get("pattern")) or "",
                    "tags": _names(p.get("tags")),
                    "tasks": [t for t in (_task(t) for t in p.get("tasks") or []) if t is not None],
                }
                for p in plays
            ],
        },
        "tags": _names(reply.get("tags")),
        "inventory": {
            "groups": [
                {
                    "name": _text(g.get("name")) or "",
                    "children": _names(g.get("children")),
                    "hosts": _names(g.get("hosts")),
                }
                for g in reply.get("groups") or []
                if isinstance(g, dict)
            ]
        },
    }

"""Descriptions: what a Playbook or Tag is for, read from the raw text of the Ansible repo.

A Playbook's Description is the first block of ``#`` lines at the top of the Playbook file (YAML loaders
drop comments, so the raw text is read here, never through the preview helper). Tag Descriptions are
repo-wide, from the top-level ``tags:`` map in ``locomotive.yml``. Both are plain text, capped, and a
broken ``locomotive.yml`` only ever costs its bad entries and a one-line warning.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yaml

DESCRIPTION_CAP = 2000
SUMMARY_CAP = 200
LOCOMOTIVE_YML = "locomotive.yml"
MAX_READ_BYTES = 1_000_000  # more than any header or tag map needs; a bigger file is read only this far

DIRECTIVES = ("yaml-language-server:", "vim:")


class PlaybookDescription(NamedTuple):
    """A Playbook's Description (``text``) and its first paragraph (``summary``); both None without one."""

    text: str | None
    summary: str | None


def _cap(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _is_directive(content: str) -> bool:
    return content.startswith(DIRECTIVES) or "-*-" in content


def _comment(line: str) -> str | None:
    """The text of a ``#`` comment line, without its prefix; None for any other line."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    return stripped[1:].strip()


def playbook_description(text: str | None) -> PlaybookDescription:
    """The Description in a Playbook's raw text (None when the file can't be read)."""
    if text is None:
        return PlaybookDescription(None, None)
    lines = text.splitlines()
    i = 0
    seen_marker = False
    # Above the block: blank lines, one ``---`` and tool directives.
    while i < len(lines):
        stripped = lines[i].strip()
        content = _comment(lines[i])
        if not stripped or (stripped == "---" and not seen_marker) or (content is not None and _is_directive(content)):
            seen_marker = seen_marker or stripped == "---"
            i += 1
            continue
        break
    paragraphs: list[list[str]] = [[]]
    while i < len(lines) and (content := _comment(lines[i])) is not None:
        i += 1
        if _is_directive(content):
            continue
        if content:
            paragraphs[-1].append(content)
        elif paragraphs[-1]:
            paragraphs.append([])
    joined = [" ".join(p) for p in paragraphs if p]
    if not joined:
        return PlaybookDescription(None, None)
    return PlaybookDescription(_cap("\n\n".join(joined), DESCRIPTION_CAP), _cap(joined[0], SUMMARY_CAP))


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tag_descriptions(text: str | None) -> tuple[dict[str, str], str | None]:
    """Tag name -> Description from ``locomotive.yml``'s text (None when there is no file), plus a
    one-line warning when anything in it had to be skipped."""
    if text is None:
        return {}, None
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - not only YAMLError: a bad date is a ValueError, deep nesting a RecursionError
        return {}, _one_line(f"{LOCOMOTIVE_YML} is not valid YAML: {exc}")
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, f"{LOCOMOTIVE_YML} must be a mapping at the top level"
    tags = data.get("tags")
    if tags is None:
        return {}, None
    if not isinstance(tags, dict):
        return {}, f"{LOCOMOTIVE_YML}: tags must be a mapping of tag name to text"
    found: dict[str, str] = {}
    skipped: list[str] = []
    for name, value in tags.items():
        if not isinstance(name, str) or not isinstance(value, str):
            skipped.append(str(name))
            continue
        if value.strip():
            found[name] = _cap(value.strip(), DESCRIPTION_CAP)
    warning = None
    if skipped:
        warning = _one_line(
            f"{LOCOMOTIVE_YML}: skipped tags without a text Description: {', '.join(skipped)}"
        )
    return found, warning


def read_text(path: Path) -> str | None:
    """A file's text (up to ``MAX_READ_BYTES``), or None if it can't be read."""
    try:
        with path.open("rb") as f:
            return f.read(MAX_READ_BYTES).decode(errors="replace")
    except OSError:
        return None

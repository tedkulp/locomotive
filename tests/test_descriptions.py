"""Descriptions: a Playbook's header comment block, and repo-wide Tag Descriptions from ``locomotive.yml``."""

import pytest

from locomotive.descriptions import (
    DESCRIPTION_CAP,
    SUMMARY_CAP,
    playbook_description,
    tag_descriptions,
)

# ---------------------------------------------------------------------- Playbook header


def test_header_block_gives_description_and_first_paragraph_as_summary():
    text = (
        "# Base setup for every server.\n"
        "# Installs packages.\n"
        "#\n"
        "# Run it after provisioning.\n"
        "- hosts: all\n"
        "  # not part of it\n"
    )

    desc = playbook_description(text)

    assert desc.summary == "Base setup for every server. Installs packages."
    assert desc.text == "Base setup for every server. Installs packages.\n\nRun it after provisioning."


def test_an_unreadable_file_means_no_description():
    assert playbook_description(None) == (None, None)


def test_no_header_means_no_description():
    assert playbook_description("- hosts: all\n  tasks: []\n") == (None, None)
    assert playbook_description("") == (None, None)


def test_comment_after_yaml_content_is_not_a_header():
    assert playbook_description("- hosts: all\n# trailing note\n") == (None, None)


def test_a_leading_document_marker_is_allowed():
    desc = playbook_description("---\n# Docker hosts.\n- hosts: docker\n")
    assert desc.summary == "Docker hosts."


def test_blank_lines_and_marker_before_the_block_are_skipped_but_the_block_ends_at_a_blank_line():
    desc = playbook_description("\n---\n\n# First.\n\n# Not in the block.\n- hosts: all\n")
    assert desc.text == "First."


def test_prefix_without_a_space_is_stripped_too():
    assert playbook_description("#Tight.\n").summary == "Tight."


@pytest.mark.parametrize(
    "directive",
    [
        "# yaml-language-server: $schema=https://example.com/schema.json",
        "# vim: set ft=yaml.ansible:",
        "# -*- mode: yaml -*-",
    ],
)
def test_directives_above_or_inside_the_block_are_skipped(directive):
    above = playbook_description(f"{directive}\n# Real text.\n- hosts: all\n")
    above_with_gap = playbook_description(f"---\n{directive}\n\n# Real text.\n- hosts: all\n")
    inside = playbook_description(f"# Real text.\n{directive}\n# More.\n- hosts: all\n")

    assert above.text == "Real text."
    assert above_with_gap.text == "Real text."
    assert inside.text == "Real text. More."


def test_a_block_of_only_directives_is_no_description():
    assert playbook_description("# vim: ft=yaml\n- hosts: all\n") == (None, None)


def test_long_description_and_summary_are_capped():
    long = "word " * 1000
    desc = playbook_description(f"# {long}\n#\n# second\n")

    assert len(desc.summary) == SUMMARY_CAP
    assert desc.summary.endswith("…")
    assert len(desc.text) == DESCRIPTION_CAP
    assert desc.text.endswith("…")


def test_windows_line_endings():
    assert playbook_description("# Hello.\r\n#\r\n# World.\r\n").text == "Hello.\n\nWorld."


# ---------------------------------------------------------------------- locomotive.yml


def test_tags_map_is_read():
    text = "tags:\n  docker: Install and configure Docker\n  hardening: SSH, firewall and sysctl lockdown\n"

    assert tag_descriptions(text) == (
        {"docker": "Install and configure Docker", "hardening": "SSH, firewall and sysctl lockdown"},
        None,
    )


def test_missing_file_or_tags_key_is_empty_without_a_warning():
    assert tag_descriptions(None) == ({}, None)
    assert tag_descriptions("") == ({}, None)
    assert tag_descriptions("other: 1\n") == ({}, None)


def test_bad_entries_are_skipped_one_by_one_and_good_ones_kept():
    text = "tags:\n  good: Fine\n  number: 3\n  list: [a, b]\n  empty: ''\n  7: seven\n"

    found, warning = tag_descriptions(text)

    assert found == {"good": "Fine"}
    assert warning is not None
    assert "number" in warning and "list" in warning
    assert "\n" not in warning


def test_invalid_yaml_is_a_one_line_warning():
    found, warning = tag_descriptions("tags:\n  docker: [unclosed\n")

    assert found == {}
    assert warning is not None and warning.startswith("locomotive.yml")
    assert "\n" not in warning


@pytest.mark.parametrize("text", ["tags:\n  a: 2001-13-45\n", "[" * 10000 + "]" * 10000])
def test_yaml_that_fails_beyond_a_yaml_error_is_still_only_a_warning(text):
    found, warning = tag_descriptions(text)

    assert found == {}
    assert warning is not None and "\n" not in warning


@pytest.mark.parametrize("text", ["tags: [a, b]\n", "tags: just text\n", "- a list\n"])
def test_non_mapping_tags_or_root_is_a_warning(text):
    found, warning = tag_descriptions(text)

    assert found == {}
    assert warning is not None


def test_long_tag_description_is_capped():
    found, _ = tag_descriptions(f"tags:\n  big: {'x' * 5000}\n")
    assert len(found["big"]) == DESCRIPTION_CAP

"""Tests for ``lithos_loom.subscriptions._project_settings`` — the project
resolution the watcher's dispatchers share, including the cheap origin read
both use to refuse a mis-mapped checkout before spending anything (PR #362
re-review 2)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.subscriptions.merge_gate_dispatch import (
    OriginRead,
    origin_read,
    origin_repo,
    parse_origin,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:agent-lore/lithos-lens.git", "agent-lore/lithos-lens"),
        ("git@github.com:agent-lore/lithos-lens", "agent-lore/lithos-lens"),
        ("ssh://git@github.com/agent-lore/lithos-lens.git", "agent-lore/lithos-lens"),
        ("https://github.com/agent-lore/lithos-lens.git", "agent-lore/lithos-lens"),
        ("https://github.com/agent-lore/lithos-lens", "agent-lore/lithos-lens"),
        ("https://github.com/agent-lore/lithos-lens/", "agent-lore/lithos-lens"),
        # real-world shapes gh resolves that a strict parser rejected (self-review)
        (
            "https://dave@github.com/agent-lore/lithos-lens.git",
            "agent-lore/lithos-lens",
        ),
        (
            "https://x-access-token:TOKEN@github.com/agent-lore/lithos-lens.git",
            "agent-lore/lithos-lens",
        ),
        (
            "ssh://git@github.com:22/agent-lore/lithos-lens.git",
            "agent-lore/lithos-lens",
        ),
        ("https://www.github.com/agent-lore/lithos-lens", "agent-lore/lithos-lens"),
        ("https://gitlab.com/x/y.git", None),
        ("", None),
    ],
)
def test_parse_origin(url: str, expected: str | None) -> None:
    assert parse_origin(url) == expected


async def test_origin_repo_reads_the_checkout(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:agent-lore/lithos-lens.git",
        ],
        check=True,
    )
    assert await origin_repo(tmp_path) == "agent-lore/lithos-lens"


async def test_origin_repo_is_none_when_it_cannot_answer(tmp_path: Path) -> None:
    assert await origin_repo(tmp_path / "missing") is None
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert await origin_repo(tmp_path) is None  # no origin remote


async def test_origin_read_names_why_it_cannot_answer(tmp_path: Path) -> None:
    # PR #362 re-review 3 F1: "cannot resolve" is a refusal with a reason the
    # operator can act on, never permission to dispatch
    assert await origin_read(tmp_path / "missing") == OriginRead(None, "missing")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert await origin_read(tmp_path) == OriginRead(None, "no_origin")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://gitlab.com/x/y",
        ],
        check=True,
    )
    assert await origin_read(tmp_path) == OriginRead(None, "unparseable")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "set-url",
            "origin",
            "git@github.com:agent-lore/lithos-lens.git",
        ],
        check=True,
    )
    assert await origin_read(tmp_path) == OriginRead("agent-lore/lithos-lens", "ok")

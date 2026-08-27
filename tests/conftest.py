"""Shared pytest fixtures for lithos-loom."""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo on branch ``main`` with one commit. Returns its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "spike@example.com")
    _git(repo, "config", "user.name", "Spike")
    (repo / "README.md").write_text("# fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture(autouse=True)
def clean_loom_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clear ``LITHOS_*`` env vars so a developer's shell cannot leak into tests.

    Tests that need a specific env should set vars explicitly via
    ``monkeypatch`` inside the test body.

    Also pins the story-develop idempotency store (US-18) to a per-test temp
    dir so a daemon-mode run's completion record never lands in the developer's
    real ``~/.local/state`` (a test that exercises the daemon happy path now
    records a completion); a test that wants to drive the store explicitly just
    re-points the same var.
    """
    for var in (
        "LITHOS_URL",
        "LITHOS_LOOM_CONFIG",
        "LITHOS_LOOM_ENVIRONMENT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(
        "LITHOS_LOOM_IDEMPOTENCY_DIR", str(tmp_path / "idempotency-store")
    )


_FIXTURE_ROUTE_CMD = (
    "uv run python -m lithos_loom.plugins.prd_decompose "
    "--task-json {{task_json}} "
    "--work-dir {{work_dir}} "
    "--result-file {{result_file}}"
)


@pytest.fixture
def loom_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a minimal ``config.toml`` and point ``LITHOS_LOOM_CONFIG`` at it."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"
            work_dir = "{tmp_path / "work"}"
            max_concurrency = 2
            log_level = "info"

            [projects.lithos-lens]
            repo = "{repo}"

            [[routes]]
            name = "prd-decompose"
            command = "{_FIXTURE_ROUTE_CMD}"
            [routes.match]
            tags = ["trigger:prd-decompose"]
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(config_path))
    return config_path


@pytest.fixture(autouse=True)
def no_sandbox_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the sandbox capability probe (SC-1) off docker in unit tests.

    ``develop()`` and ``review_head()`` prime the probe at run start, which shells
    out to ``docker inspect``. The gate must stay hermetic — CI may have no
    docker at all, and a unit test has no business starting a container — so the
    single I/O entry point is stubbed to "cannot tell". That is the fail-soft
    path: prompts then carry no environment section, never a fabricated absence.

    A test that wants the real resolution overrides ``resolve_image_id`` (and
    ``probe_image``) at function scope, which wins over this fixture — see
    ``tests/test_story_develop_sandbox_facts.py``.
    """
    from lithos_loom.plugins.story_develop import sandbox_facts

    sandbox_facts.reset_cache()
    monkeypatch.setattr(sandbox_facts, "resolve_image_id", lambda image: None)
